"""Resumable chunked uploads (FR-XFER-01, NFR-REL-02/03/04, references/storage.md).

Storage first, metadata second: a version's blob is fully written and verified before the
transaction that makes it visible commits. A blob whose commit fails is left for the GC job
(roadmap F3); staged chunks nothing pending owns are removed by `expire_uploads`.
"""

import hashlib
import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NoReturn

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError, invalid
from dropshare_server.models import FileVersion, Node, UploadSession, User
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import uploads as upload_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import changes as change_service
from dropshare_server.services import nodes as node_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.context import CallContext
from dropshare_server.storage.base import CommitResult, StorageBackend, new_blob_key
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.error_details import FileTooLargeDetails, UploadIncompleteDetails
from dropshare_shared.schemas.nodes import NodeKind, NodeOut
from dropshare_shared.schemas.sync import ChangeType
from dropshare_shared.schemas.uploads import (
    CHUNK_SHA256_HEADER,
    SHA256_PATTERN,
    UploadCreateIn,
    UploadOut,
    UploadStatus,
)

_MISSING_CHUNKS_SHOWN = 1000
_EXPIRE_BATCH = 500
_SWEEP_BATCH = 1000  # Oracle IN-list limit


def _to_out(upload: UploadSession, received: set[int]) -> UploadOut:
    return UploadOut(
        id=upload.id,
        status=upload.status,
        size_bytes=upload.size_bytes,
        chunk_size=upload.chunk_size,
        chunk_count=upload.chunk_count,
        received_chunks=sorted(received),
        expires_at=upload.expires_at,
        node_id=upload.node_id,
    )


def _quota_exceeded() -> AppError:
    return AppError(409, ErrorCode.QUOTA_EXCEEDED, "There isn't enough space left in your quota.")


def _owner(session: Session, namespace_id: uuid.UUID, *, lock: bool) -> User:
    namespace = namespace_repository.get(session, namespace_id)
    owner = (
        None
        if namespace is None
        else (user_repository.get_for_update if lock else user_repository.get)(
            session, namespace.owner_user_id
        )
    )
    if owner is None:
        raise RuntimeError(f"Namespace {namespace_id} has no owner")
    return owner


def _check_quota(owner: User, needed: int, reserved: int) -> None:
    # Shrinking a file is always allowed, even for a user already over a lowered quota.
    if needed > 0 and owner.used_bytes + reserved + needed > owner.quota_bytes:
        raise _quota_exceeded()


def start(
    session: Session, settings: Settings, user_id: uuid.UUID, body: UploadCreateIn
) -> UploadOut:
    if body.size_bytes > settings.max_file_size_bytes:
        raise AppError(
            413,
            ErrorCode.FILE_TOO_LARGE,
            "This file is larger than the maximum allowed size.",
            FileTooLargeDetails(max_file_size_bytes=settings.max_file_size_bytes).model_dump(),
        )
    if body.node_id is not None and body.base_revision is not None:
        # An editor of a single shared file may upload a new version of it (OQ-32).
        node = access.require_node(session, user_id, body.node_id, write=True, file_share=True)
        if node.kind != NodeKind.FILE:
            raise invalid("Only files can get new content.", "not_a_file", ["body", "node_id"])
        if node.revision != body.base_revision:
            raise node_service.revision_conflict(node.revision)
        namespace_id, parent_id, name = node.namespace_id, None, None
    elif body.parent_id is not None and body.name is not None:
        name, normalized = node_service.valid_name(body.name, ["body", "name"])
        parent = access.require_node(
            session, user_id, body.parent_id, write=True, follow_mount=True
        )
        node_service.require_folder(parent)
        existing = node_repository.get_live_child(
            session, parent.namespace_id, parent.id, normalized
        )
        if existing is not None:
            raise node_service.name_conflict(existing)
        namespace_id, parent_id = parent.namespace_id, parent.id
    else:  # UploadCreateIn guarantees one of the two targets
        raise invalid("Send either parent_id and name, or node_id and base_revision.", "target")

    now = datetime.now(UTC)
    # The whole size is reserved, new versions included: that's what gets staged on disk until
    # the upload completes or expires (audit decision D1). Locking the owner serialises
    # reservations, so two uploads can't both take the last space.
    owner = _owner(session, namespace_id, lock=True)
    _check_quota(owner, body.size_bytes, upload_repository.reserved_bytes(session, owner.id, now))
    upload = UploadSession(
        user_id=user_id,
        namespace_id=namespace_id,
        parent_id=parent_id,
        name=name,
        node_id=body.node_id,
        base_revision=body.base_revision,
        size_bytes=body.size_bytes,
        chunk_size=settings.upload_chunk_size_bytes,
        sha256=body.sha256,
        status=UploadStatus.PENDING,
        created_at=now,
        expires_at=now + timedelta(hours=settings.upload_expiry_hours),
    )
    upload_repository.add(session, upload)
    return _to_out(upload, set())


def _require_upload(
    session: Session, user_id: uuid.UUID, upload_id: uuid.UUID, *, lock: bool = False
) -> UploadSession:
    upload = (upload_repository.get_for_update if lock else upload_repository.get)(
        session, upload_id
    )
    if upload is None:
        raise access.not_found("Upload")
    if upload.user_id != user_id:
        raise access.not_found("Upload", denied=True, target=("upload", str(upload_id)))
    return upload


def _require_pending(upload: UploadSession, now: datetime) -> None:
    if upload.status != UploadStatus.PENDING or upload.expires_at <= now:
        raise AppError(
            404, ErrorCode.NOT_FOUND, "This upload has expired or was cancelled. Start it again."
        )


def get(
    session: Session, storage: StorageBackend, user_id: uuid.UUID, upload_id: uuid.UUID
) -> UploadOut:
    upload = _require_upload(session, user_id, upload_id)
    if upload.status == UploadStatus.PENDING:
        _require_pending(upload, datetime.now(UTC))
        return _to_out(upload, storage.chunk_indexes(upload.id))
    return _to_out(upload, set())


def expected_chunk_size(upload: UploadSession, index: int) -> int:
    if not 0 <= index < upload.chunk_count:
        message = (
            "This upload has no chunks: complete it directly."
            if upload.chunk_count == 0
            else f"Chunk index must be between 0 and {upload.chunk_count - 1}."
        )
        raise invalid(message, "chunk_index", ["path", "index"])
    return min(upload.chunk_size, upload.size_bytes - index * upload.chunk_size)


def wrong_chunk_size(index: int, expected: int) -> AppError:
    return invalid(f"Chunk {index} must be exactly {expected} bytes.", "chunk_size", ["body"])


def chunk_limit(session: Session, user_id: uuid.UUID, upload_id: uuid.UUID, index: int) -> int:
    """The exact byte length chunk `index` must have, checked before its body is read.

    Ends the transaction, so no pooled connection is held while the body arrives: a slow client
    sending 8 MiB would otherwise pin one for minutes (audit S1). put_chunk checks again.
    """
    upload = _require_upload(session, user_id, upload_id)
    _require_pending(upload, datetime.now(UTC))
    expected = expected_chunk_size(upload, index)
    session.commit()
    return expected


def put_chunk(
    session: Session,
    storage: StorageBackend,
    user_id: uuid.UUID,
    upload_id: uuid.UUID,
    index: int,
    data: bytes,
    declared_sha256: str | None,
) -> None:
    """Store one verified chunk. Sending a chunk again is safe (NFR-REL-04)."""
    upload = _require_upload(session, user_id, upload_id)
    _require_pending(upload, datetime.now(UTC))
    expected = expected_chunk_size(upload, index)
    if len(data) != expected:
        raise wrong_chunk_size(index, expected)
    declared = (declared_sha256 or "").strip().lower()
    if not SHA256_PATTERN.fullmatch(declared):
        raise invalid(
            "The X-Chunk-SHA256 header is missing or not a hex SHA-256.",
            "chunk_sha256",
            ["header", CHUNK_SHA256_HEADER],
        )
    if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), declared):
        raise AppError(
            400, ErrorCode.CHUNK_HASH_MISMATCH, "The chunk arrived corrupted; send it again."
        )
    storage.write_chunk(upload.id, index, data)


def complete(
    session: Session,
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    ctx: CallContext,
    user_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> NodeOut:
    """Verify the whole file and make it visible. Completing again returns the same result.

    Order (audit S2): check the target without locks, assemble the blob, then lock the target,
    check again and record the version. Only the upload row is locked while assembling, so a
    large file doesn't block other writes in its folder. The staged chunks are discarded only
    after the database commit: any failure before that leaves the upload retryable.
    """
    upload = _require_upload(session, user_id, upload_id, lock=True)
    if upload.status == UploadStatus.COMPLETED:
        return _replay(session, user_id, upload)
    now = datetime.now(UTC)
    _require_pending(upload, now)
    missing = sorted(set(range(upload.chunk_count)) - storage.chunk_indexes(upload.id))
    if missing:
        raise AppError(
            409,
            ErrorCode.UPLOAD_INCOMPLETE,
            "Some chunks haven't arrived yet.",
            UploadIncompleteDetails(missing_chunks=missing[:_MISSING_CHUNKS_SHOWN]).model_dump(),
        )
    node, parent = _target(session, user_id, upload, lock=False)  # fail early, before assembling
    previous_size = node.size_bytes if node is not None else 0
    _check_quota(
        _owner(session, _namespace_of(node, parent), lock=False),
        upload.size_bytes - previous_size,
        0,
    )

    blob_key = new_blob_key()
    stored = storage.commit(upload.id, upload.chunk_count, blob_key)
    if stored.sha256 != upload.sha256 or stored.size_bytes != upload.size_bytes:
        _reject_mismatch(session, session_factory, storage, ctx, upload, blob_key, stored)
    try:
        result = _record_version(session, ctx, user_id, upload, blob_key, stored, now)
    except AppError:
        storage.delete_blob(blob_key)  # the staged chunks remain, so the client can retry
        raise
    session.commit()  # the version is durable in storage and now in the database
    storage.discard_upload(upload.id)
    return result


def _replay(session: Session, user_id: uuid.UUID, upload: UploadSession) -> NodeOut:
    """The file's content state as this upload left it, not as later edits made it: a client
    retrying after a lost response must not take another device's revision as its own (B2).

    If the file has gone to the trash since, the answer is 409 REVISION_CONFLICT (current
    revision None), as for an upload to a trashed file: the client must not record a file
    that is only in the trash as synced, and uploads its copy again as a new file."""
    if upload.node_id is None or upload.result_revision is None or upload.completed_at is None:
        raise RuntimeError(f"Completed upload {upload.id} has no result")
    node = access.require_node(
        session, user_id, upload.node_id, allow_trashed=True, file_share=True
    )
    if node.trashed_at is not None:
        raise node_service.revision_conflict(None)
    return node_service.to_out(node).model_copy(
        update={
            "revision": upload.result_revision,
            "content_hash": upload.sha256,
            "size_bytes": upload.size_bytes,
            "modified_at": upload.completed_at,
            "modified_by_user_id": upload.user_id,
        }
    )


def _target(
    session: Session, user_id: uuid.UUID, upload: UploadSession, *, lock: bool
) -> tuple[Node | None, Node | None]:
    """(edited file, None) or (None, parent folder), still valid for this upload."""
    if upload.node_id is not None:
        node = access.require_node(
            session,
            user_id,
            upload.node_id,
            write=True,
            lock=lock,
            allow_trashed=True,
            file_share=True,
        )
        if node.trashed_at is not None:
            raise node_service.revision_conflict(None)  # the file went to the trash
        if node.revision != upload.base_revision:
            raise node_service.revision_conflict(node.revision)
        return node, None

    if upload.parent_id is None or upload.name is None:
        raise RuntimeError(f"Upload {upload.id} has no target")
    parent = access.require_node(
        session,
        user_id,
        upload.parent_id,
        write=True,
        lock=lock,
        resource="Folder",
        follow_mount=True,
    )
    normalized = node_service.valid_name(upload.name, ["name"])[1]
    existing = node_repository.get_live_child(session, parent.namespace_id, parent.id, normalized)
    if existing is not None:
        raise node_service.name_conflict(existing)
    return None, parent


def _namespace_of(node: Node | None, parent: Node | None) -> uuid.UUID:
    """Where the file lands now: its folder may have moved to a shared folder's namespace (or
    out of one) since the upload started, and the quota charged is that namespace owner's."""
    target = node if node is not None else parent
    if target is None:
        raise RuntimeError("An upload target has a file or a folder")
    return target.namespace_id


def _reject_mismatch(
    session: Session,
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    ctx: CallContext,
    upload: UploadSession,
    blob_key: str,
    stored: CommitResult,
) -> NoReturn:
    """Every chunk was verified, so the declared whole-file hash is wrong (the file changed
    while uploading): no retry can succeed. Cancel for good, then clean up."""
    upload.status = UploadStatus.CANCELLED
    # Committed here: the error raised below rolls back the request transaction, and a separate
    # transaction would wait forever on the upload row lock this one holds.
    session.commit()
    storage.delete_blob(blob_key)
    storage.discard_upload(upload.id)
    audit_service.record_now(
        session_factory,
        ctx,
        AuditAction.FILE_UPLOADED,
        AuditOutcome.FAILURE,
        target=AuditTarget(type="upload", id=str(upload.id), name=upload.name),
        details={"reason": "file_hash_mismatch", "size_bytes": stored.size_bytes},
    )
    raise AppError(
        400,
        ErrorCode.FILE_HASH_MISMATCH,
        "The uploaded file doesn't match its checksum. It may have changed while uploading; "
        "start the upload again.",
        audited=True,
    )


def _record_version(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    upload: UploadSession,
    blob_key: str,
    stored: CommitResult,
    now: datetime,
) -> NodeOut:
    """Lock the target and the owner, check again, and record the version, change and audit."""
    node, parent = _target(session, user_id, upload, lock=True)
    previous_size = node.size_bytes if node is not None else 0
    owner = _owner(session, _namespace_of(node, parent), lock=True)
    delta = stored.size_bytes - previous_size
    _check_quota(owner, delta, 0)  # again under the lock: another upload may have finished

    version_id = uuid.uuid4()
    if node is None:
        assert parent is not None and upload.name is not None  # noqa: S101 - set by _target
        node = Node(
            namespace_id=parent.namespace_id,
            parent_id=parent.id,
            name=upload.name,
            name_normalized=node_service.valid_name(upload.name, ["name"])[1],
            kind=NodeKind.FILE,
            revision=1,
            created_at=now,
            created_by_user_id=user_id,
        )
        change_type = ChangeType.CREATED
    else:
        node.revision += 1
        change_type = ChangeType.MODIFIED
    node.size_bytes = stored.size_bytes
    node.content_hash = stored.sha256
    node.current_version_id = version_id
    node.modified_at = now
    node.modified_by_user_id = user_id
    if change_type == ChangeType.CREATED:
        node_service.add_live(session, node)
    node_repository.add_version(
        session,
        FileVersion(
            id=version_id,
            node_id=node.id,
            blob_key=blob_key,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            created_at=now,
            created_by_user_id=user_id,
        ),
    )
    owner.used_bytes += delta
    upload.status = UploadStatus.COMPLETED
    upload.completed_at = now
    upload.node_id = node.id
    upload.result_revision = node.revision
    change_service.record(session, node, change_type, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.FILE_UPLOADED,
        target=node_service.node_target(node),
        details={
            "size_bytes": stored.size_bytes,
            "new_version": change_type == ChangeType.MODIFIED,
        },
    )
    return node_service.to_out(node)


def cancel(
    session: Session, storage: StorageBackend, user_id: uuid.UUID, upload_id: uuid.UUID
) -> None:
    upload = _require_upload(session, user_id, upload_id, lock=True)
    if upload.status == UploadStatus.COMPLETED:
        raise invalid("This upload has already completed.", "already_completed")
    upload.status = UploadStatus.CANCELLED
    session.flush()
    storage.discard_upload(upload.id)


@dataclass(frozen=True)
class ExpiryResult:
    expired: int  # pending uploads past their expiry, now marked expired
    swept: int  # staged chunk folders removed because no pending upload owns them


def expire_uploads(
    session_factory: sessionmaker[Session], storage: StorageBackend, now: datetime
) -> ExpiryResult:
    """The expire-uploads job (FR-XFER-01, NFR-REL-05): end uploads older than their expiry and
    remove staged chunks nothing pending owns any more (expired, cancelled, completed, or left
    behind by a race). Idempotent and safe to run while the API is serving."""
    expired = 0
    while True:
        with session_factory.begin() as session:
            batch = upload_repository.lock_expired(session, now, _EXPIRE_BATCH)
            for upload in batch:
                upload.status = UploadStatus.EXPIRED
        expired += len(batch)
        if len(batch) < _EXPIRE_BATCH:
            break

    swept = 0
    staged = list(storage.staged_upload_ids())
    for start_at in range(0, len(staged), _SWEEP_BATCH):
        candidates = staged[start_at : start_at + _SWEEP_BATCH]
        with session_factory() as session:
            pending = upload_repository.pending_ids(session, candidates)
        for upload_id in candidates:
            if upload_id not in pending:
                storage.discard_upload(upload_id)
                swept += 1
    return ExpiryResult(expired=expired, swept=swept)
