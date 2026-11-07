"""Resumable chunked uploads (FR-XFER-01, NFR-REL-02/03/04, references/storage.md).

Storage first, metadata second: a version's blob is fully written and verified before the
transaction that makes it visible commits. A blob whose commit fails is left for the GC job
(roadmap F3); staged chunks nothing pending owns are removed by `expire_uploads`.
"""

import hashlib
import hmac
import logging
import uuid
from collections.abc import Callable, Iterable
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
from dropshare_server.scanner.base import Scanner, ScannerUnavailableError, ScanResult
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import changes as change_service
from dropshare_server.services import nodes as node_service
from dropshare_server.services.audit import AuditDetails, AuditTarget
from dropshare_server.services.context import CallContext
from dropshare_server.services.file_policy import FileTypePolicy
from dropshare_server.services.file_types import executable_kind
from dropshare_server.storage.base import CommitResult, StorageBackend, new_blob_key
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.error_details import (
    FileTooLargeDetails,
    MalwareDetectedDetails,
    UploadIncompleteDetails,
)
from dropshare_shared.schemas.nodes import NodeKind, NodeOut
from dropshare_shared.schemas.sync import ChangeType
from dropshare_shared.schemas.uploads import (
    CHUNK_SHA256_HEADER,
    SHA256_PATTERN,
    UploadCreateIn,
    UploadOut,
    UploadStatus,
)

logger = logging.getLogger(__name__)

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
    session: Session,
    settings: Settings,
    file_types: FileTypePolicy,
    ctx: CallContext,
    user_id: uuid.UUID,
    body: UploadCreateIn,
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
        # A file stored before its type was banned keeps its content, but gets no new one
        # (OQ-57). Checked before anything is locked or created, so no content is ever sent.
        file_types.check_name(session, ctx, node.name or "", audit_service.node_target(node))
        namespace_id, parent_id, name = node.namespace_id, None, None
    elif body.parent_id is not None and body.name is not None:
        name, normalized = node_service.valid_name(body.name, ["body", "name"])
        parent = access.require_node(
            session, user_id, body.parent_id, write=True, follow_mount=True
        )
        node_service.require_folder(parent)
        file_types.check_name(session, ctx, name, _new_file_target(parent, name))
        existing = node_repository.get_live_child(
            session, parent.namespace_id, parent.id, normalized
        )
        if existing is not None:
            raise node_service.name_conflict(existing)
        namespace_id, parent_id = parent.namespace_id, parent.id
    else:  # UploadCreateIn guarantees one of the two targets
        raise invalid("Send either parent_id and name, or node_id and base_revision.", "target")

    now = datetime.now(UTC)
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
    # Inserted before the owner is locked: the row's foreign keys lock its folder (or file), and
    # completing an upload locks that node and then the owner, so this order can't deadlock
    # with it (M10, parallel uploads). A refused upload rolls back with the request.
    upload_repository.add(session, upload)
    # The whole size is reserved, new versions included: that's what gets staged on disk until
    # the upload completes or expires (audit decision D1). Locking the owner serialises
    # reservations, so two uploads can't both take the last space.
    owner = _owner(session, namespace_id, lock=True)
    others = upload_repository.reserved_bytes(session, owner.id, now) - upload.size_bytes
    _check_quota(owner, body.size_bytes, others)
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
    return _status(storage, upload)


def _all_chunks(upload: UploadSession) -> set[int]:
    """Assembled already (scanning, or refused after it): every chunk counts as received, so
    no client sends any again, older ones included (FR-SAFE-01)."""
    return set(range(upload.chunk_count))


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
    file_types: FileTypePolicy,
    ctx: CallContext,
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
    if index == 0:
        # A program is refused as soon as its start arrives, so the rest is never sent. The
        # check at completion still decides: it also sees a PE header beyond this chunk.
        kind = executable_kind(lambda offset, length: data[offset : offset + length])
        if kind is not None:
            target = AuditTarget(type="upload", id=str(upload.id), name=upload.name)
            locked = _require_upload(session, user_id, upload_id, lock=True)
            _require_pending(locked, datetime.now(UTC))  # not completed or cancelled meanwhile
            _cancel(session, storage, locked, None)
            raise file_types.refuse_executable(session, ctx, target, kind)
    storage.write_chunk(upload.id, index, data)


def complete(
    session: Session,
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    file_types: FileTypePolicy,
    scanner: Scanner,
    settings: Settings,
    ctx: CallContext,
    user_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> NodeOut | UploadOut:
    """Verify the whole file, scan it, and make it visible. Completing again returns the same
    result. Returns the upload instead of the file (202) while the malware scan is still to
    come: the client asks again (FR-SAFE-01, OQ-57 (5), OQ-58).

    Order (audit S2): check the target without locks, assemble the blob, then lock the target,
    check again and record the version. Only the upload row is locked while assembling, so a
    large file doesn't block other writes in its folder. The staged chunks are discarded only
    after the database commit: any failure before that leaves the upload retryable.
    """
    found = _require_upload(session, user_id, upload_id)
    # Never wait for another `complete` assembling it or for the scan worker scanning it, which
    # can take minutes: answer with where it stands (202), and the client asks again. A retry
    # that finds it handed to the worker meanwhile gets the same (NFR-REL-04).
    upload = upload_repository.get_for_update(session, upload_id, skip_locked=True)
    if upload is None:
        return _status(storage, found)
    if upload.status == UploadStatus.SCANNING:
        return _status(storage, upload)
    if upload.status == UploadStatus.COMPLETED:
        return _replay(session, user_id, upload)
    if upload.status == UploadStatus.REJECTED:
        raise _rejection(session, user_id, upload)
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
    target = _upload_target(upload, node, parent)
    extension = file_types.banned_extension(target.name or "")
    if extension is not None:
        # The list may have changed since the upload started: no retry can succeed.
        _cancel(session, storage, upload, None)
        raise file_types.refuse_extension(session, ctx, target, extension)
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
    kind = executable_kind(_reader(storage, blob_key, stored.size_bytes))
    if kind is not None:  # a program, whatever its name (FR-SAFE-02): the old version stays
        _cancel(session, storage, upload, blob_key)
        raise file_types.refuse_executable(session, ctx, target, kind)
    if upload.size_bytes > settings.scan_inline_max_bytes:
        return _defer(session, storage, ctx, upload, blob_key, now)
    try:
        scan = scanner.scan(blob_chunks(storage, blob_key, stored.size_bytes))
    except ScannerUnavailableError:
        logger.warning("Upload %s waits for the malware scanner", upload.id, exc_info=True)
        return _defer(session, storage, ctx, upload, blob_key, now)
    if scan.detection is not None:
        target, details = malware_audit(session, upload, scan.detection)
        reject(upload, ErrorCode.MALWARE_DETECTED, scan.detection)
        # In the same transaction as the refusal, as the scan worker does: no row of the users
        # table is locked here, so the audit's reference to the actor can't wait on one.
        audit_service.record(
            session,
            ctx,
            AuditAction.FILE_MALWARE_DETECTED,
            AuditOutcome.DENIED,
            target=target,
            details=details,
        )
        session.commit()
        _remove_data(storage, upload, blob_key)
        raise malware_detected(scan.detection, audited=True)
    try:
        result = record_version(session, ctx, user_id, upload, blob_key, stored, scan, now)
    except AppError:
        _remove_blob(storage, blob_key)  # the staged chunks remain, so the client can retry
        raise
    session.commit()  # the version is durable in storage and now in the database
    storage.discard_upload(upload.id)
    return result


def _defer(
    session: Session,
    storage: StorageBackend,
    ctx: CallContext,
    upload: UploadSession,
    blob_key: str,
    now: datetime,
) -> UploadOut:
    """Leave the scan to the scan worker: too large to scan within the request, or the scanner
    is down (OQ-55: the upload waits, nothing becomes visible unscanned). The blob holds every
    byte now, so the staged chunks go; gc-blobs keeps the blob while the upload scans."""
    upload.status = UploadStatus.SCANNING
    upload.blob_key = blob_key
    upload.device_id = ctx.actor.device_id
    upload.source_ip = ctx.source_ip
    upload.request_id = ctx.request_id
    upload.next_scan_at = now
    session.commit()
    _remove_data(storage, upload, None)
    return _to_out(upload, _all_chunks(upload))


def blob_chunks(storage: StorageBackend, blob_key: str, size: int) -> Iterable[bytes]:
    """A stored blob as a stream, for the scanner (never all of it in memory, §11)."""
    return storage.open_range(blob_key, 0, size - 1) if size > 0 else ()


def reject(upload: UploadSession, code: ErrorCode, detection: str | None = None) -> None:
    """Refused after it was assembled: `complete` answers with `code` from now on."""
    upload.status = UploadStatus.REJECTED
    upload.rejection_code = code.value
    upload.detection = detection
    upload.next_scan_at = None


def malware_detected(detection: str, *, audited: bool) -> AppError:
    return AppError(
        422,
        ErrorCode.MALWARE_DETECTED,
        f"DropShare's virus scan found a threat in this file ({detection}), so it wasn't "
        "uploaded. Your copy on this computer hasn't been changed.",
        MalwareDetectedDetails(detection=detection).model_dump(),
        audited=audited,
    )


def malware_audit(
    session: Session, upload: UploadSession, detection: str
) -> tuple[AuditTarget, AuditDetails]:
    """Target and details of `file.malware_detected` (FR-SAFE-01): the file's name and folder
    and the detection, never content. Read before anything is committed or rolled back."""
    node = node_repository.get(session, upload.node_id) if upload.node_id else None
    folder_id = node.parent_id if node is not None else upload.parent_id
    target = audit_target(session, upload)
    details: AuditDetails = {
        "detection": detection,
        "folder_id": str(folder_id) if folder_id else None,
        "size_bytes": upload.size_bytes,
        "new_version": upload.base_revision is not None,
        "upload_id": str(upload.id),
    }
    return target, details


def audit_target(session: Session, upload: UploadSession) -> AuditTarget:
    """The file an upload is for: the file itself for a new version, else its folder and name."""
    node = node_repository.get(session, upload.node_id) if upload.node_id else None
    if node is not None:
        return audit_service.node_target(node)
    return AuditTarget(type="folder", id=str(upload.parent_id), name=upload.name)


def _rejection(session: Session, user_id: uuid.UUID, upload: UploadSession) -> AppError:
    """What a repeated `complete` answers for an upload refused after it was assembled: the
    same code, status and details `complete` itself would have answered with. Anything else
    becomes 404 "start again", which every client treats as the end of this upload."""
    code = upload.rejection_code
    if code == ErrorCode.MALWARE_DETECTED:
        return malware_detected(upload.detection or "unknown", audited=True)
    if code == ErrorCode.REVISION_CONFLICT:
        return node_service.revision_conflict(_current_revision(session, user_id, upload))
    if code == ErrorCode.NAME_CONFLICT and upload.parent_id is not None and upload.name:
        existing = node_repository.get_live_child(
            session, upload.namespace_id, upload.parent_id, normalize_name(upload.name)
        )
        if existing is not None:
            return node_service.name_conflict(existing)
    if code == ErrorCode.QUOTA_EXCEEDED:
        return _quota_exceeded()
    if code == ErrorCode.FORBIDDEN:
        return access.view_only()
    return AppError(
        404,
        ErrorCode.NOT_FOUND,
        "Where this file was going changed while it was scanned. Start the upload again.",
    )


def _current_revision(session: Session, user_id: uuid.UUID, upload: UploadSession) -> int | None:
    """The file's revision now, only for someone who may still see it."""
    if upload.node_id is None:
        return None
    try:
        node = access.require_node(session, user_id, upload.node_id, file_share=True)
    except AppError:
        return None
    return node.revision


def _status(storage: StorageBackend, upload: UploadSession) -> UploadOut:
    """Where an upload stands, as GET /uploads/{id} answers."""
    if upload.status == UploadStatus.PENDING:
        return _to_out(upload, storage.chunk_indexes(upload.id))
    if upload.status in (UploadStatus.SCANNING, UploadStatus.REJECTED):
        return _to_out(upload, _all_chunks(upload))
    return _to_out(upload, set())


def _remove_blob(storage: StorageBackend, blob_key: str) -> None:
    try:
        storage.delete_blob(blob_key)
    except OSError:
        logger.warning("Couldn't remove blob %s; gc-blobs will", blob_key, exc_info=True)


def _remove_data(storage: StorageBackend, upload: UploadSession, blob_key: str | None) -> None:
    """Best effort: what's left behind is removed by gc-blobs and expire-uploads."""
    try:
        if blob_key is not None:
            storage.delete_blob(blob_key)
        storage.discard_upload(upload.id)
    except OSError:
        logger.warning("Couldn't remove the data of upload %s", upload.id, exc_info=True)


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
    _cancel(session, storage, upload, blob_key)
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


def _cancel(
    session: Session, storage: StorageBackend, upload: UploadSession, blob_key: str | None
) -> None:
    """Cancel an upload no retry can complete, and remove what it stored.

    Committed here: the error raised next rolls back the request transaction, and a separate
    transaction (the audit row) would wait forever on the locks this one holds. Removing the
    bytes is best effort: if it fails, gc-blobs and expire-uploads remove them later, and the
    caller still reports why the upload was refused.
    """
    upload.status = UploadStatus.CANCELLED
    session.commit()
    _remove_data(storage, upload, blob_key)


def _new_file_target(parent: Node, name: str) -> AuditTarget:
    """A file that doesn't exist yet is recorded by its folder and name (FR-AUDIT-02)."""
    return AuditTarget(type="folder", id=str(parent.id), name=name)


def _upload_target(upload: UploadSession, node: Node | None, parent: Node | None) -> AuditTarget:
    if node is not None:
        return audit_service.node_target(node)
    assert parent is not None and upload.name is not None  # noqa: S101 - set by _target
    return _new_file_target(parent, upload.name)


def _reader(storage: StorageBackend, blob_key: str, size: int) -> Callable[[int, int], bytes]:
    """`read(offset, length)` over a stored blob, for `executable_kind`."""

    def read(offset: int, length: int) -> bytes:
        end = min(offset + length, size) - 1
        if offset >= size or end < offset:
            return b""
        return b"".join(storage.open_range(blob_key, offset, end))

    return read


def record_version(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    upload: UploadSession,
    blob_key: str,
    stored: CommitResult,
    scan: ScanResult,
    now: datetime,
) -> NodeOut:
    """Lock the target and the owner, check again, and record the version, change and audit.
    Only ever for content the malware scanner passed (FR-SAFE-01)."""
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
            scanned_at=now,
            scan_signature=scan.signature,
        ),
    )
    owner.used_bytes += delta
    upload.status = UploadStatus.COMPLETED
    upload.completed_at = now
    upload.node_id = node.id
    upload.result_revision = node.revision
    upload.blob_key = upload.next_scan_at = None
    change_service.record(session, node, change_type, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.FILE_UPLOADED,
        target=audit_service.node_target(node),
        details={
            "size_bytes": stored.size_bytes,
            "new_version": change_type == ChangeType.MODIFIED,
        },
    )
    return node_service.to_out(node)


def cancel(
    session: Session, storage: StorageBackend, user_id: uuid.UUID, upload_id: uuid.UUID
) -> None:
    _require_upload(session, user_id, upload_id)
    # Never wait for the scan worker, which holds the row for minutes while it scans: say at
    # once that the upload may still complete (FR-SAFE-01, audit 2026-11-07).
    upload = upload_repository.get_for_update(session, upload_id, skip_locked=True)
    if upload is None:
        raise AppError(
            409,
            ErrorCode.UPLOAD_SCANNING,
            "This upload is being scanned right now and may still complete. Ask again shortly.",
        )
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
