"""Namespaces, folders, delete to trash and download (FR-FILE-04, FR-TRASH-01, FR-XFER-02)."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError, invalid
from dropshare_server.models import Node
from dropshare_server.repositories import changes as change_repository
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories.nodes import LiveNameTakenError
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import changes as change_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.context import CallContext
from dropshare_server.storage.base import StorageBackend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import InvalidNameError, normalize_name, validate_name
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.envelope import Meta, PageQuery
from dropshare_shared.schemas.error_details import (
    NameConflictDetails,
    RangeNotSatisfiableDetails,
    RevisionConflictDetails,
)
from dropshare_shared.schemas.nodes import (
    AccessRole,
    FolderCreateIn,
    NamespaceOut,
    NodeKind,
    NodeOut,
)
from dropshare_shared.schemas.sync import ChangeType


def to_out(node: Node) -> NodeOut:
    return NodeOut.model_validate(node, from_attributes=True)


def node_target(node: Node) -> AuditTarget:
    return AuditTarget(type="node", id=str(node.id), name=node.name)


def valid_name(name: str, loc: list[str | int]) -> tuple[str, str]:
    """(NFC name, normalized name), or 400 with the NameProblem code as the issue `type`."""
    try:
        nfc = validate_name(name)
    except InvalidNameError as exc:
        raise invalid(str(exc), exc.problem.value, loc) from None
    return nfc, normalize_name(nfc)


def name_conflict(existing: Node) -> AppError:
    return AppError(
        409,
        ErrorCode.NAME_CONFLICT,
        "An item with this name already exists in this folder.",
        NameConflictDetails(node_id=existing.id, kind=existing.kind).model_dump(mode="json"),
    )


def add_live(session: Session, node: Node) -> None:
    """Insert a live node; if another request took the name first, 409 with that item."""
    try:
        node_repository.add(session, node)
    except LiveNameTakenError:
        assert node.parent_id is not None and node.name_normalized is not None  # noqa: S101
        existing = node_repository.get_live_child(
            session, node.namespace_id, node.parent_id, node.name_normalized
        )
        if existing is None:  # the other item is already gone again; the client may retry
            raise AppError(
                409, ErrorCode.NAME_CONFLICT, "An item with this name was just created here."
            ) from None
        raise name_conflict(existing) from None


def revision_conflict(current_revision: int | None) -> AppError:
    return AppError(
        409,
        ErrorCode.REVISION_CONFLICT,
        "This item changed on the server.",
        RevisionConflictDetails(current_revision=current_revision).model_dump(mode="json"),
    )


def require_folder(node: Node) -> None:
    if node.kind != NodeKind.FOLDER:
        raise invalid("The parent must be a folder.", "not_a_folder", ["body", "parent_id"])


def list_namespaces(session: Session, user_id: uuid.UUID) -> list[NamespaceOut]:
    result = []
    for namespace in namespace_repository.list_owned(session, user_id):
        root = node_repository.get_root(session, namespace.id)
        if root is None:  # every namespace gets a root when created (users.create_user)
            raise RuntimeError(f"Namespace {namespace.id} has no root folder")
        result.append(
            NamespaceOut(
                id=namespace.id, kind=namespace.kind, root_node_id=root.id, role=AccessRole.OWNER
            )
        )
    return result


def get_node(session: Session, user_id: uuid.UUID, node_id: uuid.UUID) -> NodeOut:
    return to_out(access.require_node(session, user_id, node_id))


def list_children(
    session: Session, user_id: uuid.UUID, node_id: uuid.UUID, page: PageQuery
) -> tuple[list[NodeOut], Meta]:
    folder = access.require_node(session, user_id, node_id)
    if folder.kind != NodeKind.FOLDER:
        raise invalid("Only folders have children.", "not_a_folder", ["path", "node_id"])
    children = node_repository.list_live_children(
        session, folder.id, offset=(page.page - 1) * page.limit, limit=page.limit
    )
    total = node_repository.count_live_children(session, folder.id)
    return [to_out(child) for child in children], Meta(
        page=page.page, limit=page.limit, total=total
    )


def create_folder(
    session: Session, ctx: CallContext, user_id: uuid.UUID, body: FolderCreateIn
) -> NodeOut:
    name, normalized = valid_name(body.name, ["body", "name"])
    # Locked so the parent can't go to the trash while the folder is added (see trash_node).
    parent = access.require_node(session, user_id, body.parent_id, write=True, lock=True)
    require_folder(parent)
    existing = node_repository.get_live_child(session, parent.namespace_id, parent.id, normalized)
    if existing is not None:
        raise name_conflict(existing)

    now = datetime.now(UTC)
    folder = Node(
        namespace_id=parent.namespace_id,
        parent_id=parent.id,
        name=name,
        name_normalized=normalized,
        kind=NodeKind.FOLDER,
        size_bytes=0,
        revision=1,
        created_at=now,
        created_by_user_id=user_id,
        modified_at=now,
        modified_by_user_id=user_id,
    )
    add_live(session, folder)
    change_service.record(session, folder, ChangeType.CREATED, user_id, now)
    audit_service.record(session, ctx, AuditAction.FOLDER_CREATED, target=node_target(folder))
    return to_out(folder)


def trash_node(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    if_revision: int,
    if_cursor: int | None = None,
) -> None:
    """Move an item and everything inside it to the owner's trash (FR-TRASH-01).

    Repeating a delete that already succeeded (a retry after a timeout) succeeds again without
    changing anything, so the client can confirm its own delete (NFR-REL-04). With
    `if_cursor`, a change to anything inside after that cursor refuses the delete: the client
    hasn't seen it, and it wins over the delete (FR-CONF-03).
    """
    node = access.require_node(session, user_id, node_id, write=True, lock=True, allow_trashed=True)
    if node.trashed_at is not None:
        if node.trash_root_id == node.id and node.revision == if_revision + 1:
            return
        raise access.not_found()
    if node.parent_id is None:
        raise invalid(
            "The root folder can't be deleted.", "root_not_deletable", ["path", "node_id"]
        )
    if node.revision != if_revision:
        raise revision_conflict(node.revision)

    descendant_ids = node_repository.lock_live_descendant_ids(session, node.id)
    if if_cursor is not None and change_repository.any_after(
        session, node.namespace_id, if_cursor, descendant_ids
    ):
        raise revision_conflict(node.revision)
    now = datetime.now(UTC)
    node.revision += 1
    node_repository.trash(session, node, descendant_ids, user_id, now)
    # One journal row for the whole subtree: clients remove everything below the node.
    change_service.record(session, node, ChangeType.TRASHED, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.NODE_TRASHED,
        target=node_target(node),
        details={"kind": node.kind.value, "descendants": len(descendant_ids)},
    )


@dataclass(frozen=True)
class Content:
    """A file's bytes start..end (inclusive), ready to stream."""

    name: str
    size_bytes: int
    sha256: str
    start: int
    end: int
    partial: bool
    chunks: Iterator[bytes]


def open_content(
    session: Session,
    storage: StorageBackend,
    ctx: CallContext,
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    byte_range: tuple[int | None, int | None] | None,
) -> Content:
    """`byte_range` is a parsed `Range: bytes=first-last` (first None = the last `last` bytes)."""
    node = access.require_node(session, user_id, node_id)
    if node.kind != NodeKind.FILE or node.current_version_id is None:
        raise invalid("Only files can be downloaded.", "not_a_file", ["path", "node_id"])
    version = node_repository.get_version(session, node.current_version_id)
    if version is None:
        raise RuntimeError(f"Node {node.id} points to a missing version")

    size = version.size_bytes
    start, end = (0, size - 1) if byte_range is None else _resolve_range(byte_range, size)
    chunks = storage.open_range(version.blob_key, start, end) if size else iter(())
    audit_service.record(
        session,
        ctx,
        AuditAction.FILE_DOWNLOADED,
        target=node_target(node),
        details={"start": start, "end": end} if byte_range is not None else None,
    )
    return Content(
        name=node.name or "download",
        size_bytes=size,
        sha256=version.sha256,
        start=start,
        end=end,
        partial=byte_range is not None,
        chunks=chunks,
    )


def _resolve_range(byte_range: tuple[int | None, int | None], size: int) -> tuple[int, int]:
    first, last = byte_range
    if first is None:  # suffix: the last `last` bytes
        if not last or size == 0:
            raise _unsatisfiable(size)
        return max(0, size - last), size - 1
    if first >= size:
        raise _unsatisfiable(size)
    return first, size - 1 if last is None else min(last, size - 1)


def _unsatisfiable(size: int) -> AppError:
    return AppError(
        416,
        ErrorCode.RANGE_NOT_SATISFIABLE,
        "The requested range is outside the file.",
        RangeNotSatisfiableDetails(size_bytes=size).model_dump(mode="json"),
        headers={"Content-Range": f"bytes */{size}"},
    )
