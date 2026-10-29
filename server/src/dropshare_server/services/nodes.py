"""Namespaces, folders, delete to trash and download (FR-FILE-04, FR-TRASH-01, FR-XFER-02)."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError, invalid
from dropshare_server.models import Namespace, Node
from dropshare_server.repositories import changes as change_repository
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import sharing as sharing_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.repositories.nodes import LiveNameTakenError
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import changes as change_service
from dropshare_server.services.audit import AuditDetails, AuditTarget
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
    NamespaceKind,
    NamespaceOut,
    NodeKind,
    NodeOut,
    NodeUpdateIn,
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


def _namespace_out(session: Session, namespace: Namespace, role: AccessRole) -> NamespaceOut:
    root = node_repository.get_root(session, namespace.id)
    if root is None:  # every namespace gets a root when created
        raise RuntimeError(f"Namespace {namespace.id} has no root folder")
    return NamespaceOut(id=namespace.id, kind=namespace.kind, root_node_id=root.id, role=role)


def list_namespaces(session: Session, user_id: uuid.UUID) -> list[NamespaceOut]:
    """What the user syncs: their own namespace, then each shared folder they have a mount of
    and still have access to (OQ-31). A shared folder they haven't added isn't synced."""
    personal = namespace_repository.get_personal(session, user_id)
    if personal is None:  # created with the user (users.create_user)
        raise RuntimeError(f"User {user_id} has no personal namespace")
    result = [_namespace_out(session, personal, AccessRole.OWNER)]
    for mount in node_repository.live_mounts(session, personal.id):
        assert mount.mount_namespace_id is not None  # noqa: S101 - live_mounts' filter
        namespace = namespace_repository.get(session, mount.mount_namespace_id)
        role = None if namespace is None else access.role_in(session, namespace, user_id)
        if namespace is not None and role is not None:  # else the mount goes at the next sync
            result.append(_namespace_out(session, namespace, role))
    return result


def get_node(session: Session, user_id: uuid.UUID, node_id: uuid.UUID) -> NodeOut:
    return to_out(access.require_node(session, user_id, node_id, file_share=True))


def list_children(
    session: Session, user_id: uuid.UUID, node_id: uuid.UUID, page: PageQuery
) -> tuple[list[NodeOut], Meta]:
    folder = access.require_node(session, user_id, node_id, follow_mount=True)
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
    parent = access.require_node(
        session, user_id, body.parent_id, write=True, lock=True, follow_mount=True
    )
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


def move_node(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    body: NodeUpdateIn,
) -> NodeOut:
    """Rename and/or move an item (FR-FILE-04, FR-SYNC-10), also into or out of a shared
    folder (`_move_between_namespaces`).

    Everything inside a folder moves with it and keeps its ID; one journal row covers the
    subtree. Repeating a move that already succeeded (a retry after a timeout) succeeds again
    without changing anything (NFR-REL-04).
    """
    node = access.require_node(session, user_id, node_id, write=True)
    if node.parent_id is None:
        raise invalid("The root folder can't be moved.", "root_not_movable", ["path", "node_id"])
    new_name = None if body.name is None else valid_name(body.name, ["body", "name"])
    if body.parent_id is not None and body.parent_id != node.parent_id:
        # Moves between folders run one at a time per namespace: the check below that a
        # folder isn't moved into itself reads the target's ancestors, which two moves at
        # once could each change (A into B while B into A). Nothing else locks the root
        # after another node, so taking it first adds no lock cycle. A move to another
        # namespace locks both roots, always in the same order.
        target = access.require_node(
            session, user_id, body.parent_id, write=True, resource="Folder", follow_mount=True
        )
        for namespace_id in sorted({node.namespace_id, target.namespace_id}, key=str):
            node_repository.lock_root(session, namespace_id)
    # The target folder is locked before the item, as creating a folder locks its parent first.
    parent = access.require_node(
        session,
        user_id,
        body.parent_id or node.parent_id,
        write=True,
        lock=True,
        resource="Folder",
        follow_mount=body.parent_id is not None,
    )
    node = access.require_node(session, user_id, node_id, write=True, lock=True)
    assert node.parent_id is not None and node.name is not None  # noqa: S101 - not the root
    name, normalized = new_name or (node.name, node.name_normalized or normalize_name(node.name))
    if node.parent_id == parent.id and node.name == name:
        if node.revision in (body.if_revision, body.if_revision + 1):
            return to_out(node)  # nothing to change, or our own earlier success
        raise revision_conflict(node.revision)
    if node.revision != body.if_revision:
        raise revision_conflict(node.revision)

    if parent.id != node.parent_id:
        require_folder(parent)
        if parent.id == node.id or node.id in node_repository.ancestor_ids(session, parent.id):
            raise invalid(
                "A folder can't be moved into itself.", "into_itself", ["body", "parent_id"]
            )
    existing = node_repository.get_live_child(session, parent.namespace_id, parent.id, normalized)
    if existing is not None and existing.id != node.id:
        raise name_conflict(existing)

    if parent.namespace_id != node.namespace_id:
        return _move_between_namespaces(session, ctx, user_id, node, parent, name, normalized)
    old_parent_id, old_name = node.parent_id, node.name
    try:
        node_repository.move(session, node, parent.id, name, normalized)
    except LiveNameTakenError:
        taken = node_repository.get_live_child(session, parent.namespace_id, parent.id, normalized)
        if taken is None:
            raise AppError(
                409, ErrorCode.NAME_CONFLICT, "An item with this name was just created here."
            ) from None
        raise name_conflict(taken) from None
    now = datetime.now(UTC)
    node.revision += 1
    change_service.record(session, node, ChangeType.MOVED, user_id, now)
    moved = old_parent_id != parent.id
    details: AuditDetails = {"old_name": old_name}
    if moved:
        details |= {"from_parent_id": str(old_parent_id), "to_parent_id": str(parent.id)}
    audit_service.record(
        session,
        ctx,
        AuditAction.NODE_MOVED if moved else AuditAction.NODE_RENAMED,
        target=node_target(node),
        details=details,
    )
    return to_out(node)


def _move_between_namespaces(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    node: Node,
    parent: Node,
    name: str,
    normalized: str,
) -> NodeOut:
    """Into or out of a shared folder: the item and everything in it (trashed items too, so a
    restore finds its folder) change namespace with their IDs. The source journal says it left
    (REMOVED); the destination lists every live item, parents first (CREATED). When the two
    namespaces have different owners the bytes move from one quota to the other, checked
    against the new owner's (FR-SHARE-06). Shared folders can't hold one another, so a mount,
    or a folder holding one, never goes into a shared folder."""
    source, destination = node.namespace_id, parent.namespace_id
    old = namespace_repository.get(session, source)
    if old is None:
        raise RuntimeError(f"Namespace {source} is missing")
    if old.kind == NamespaceKind.SHARED and old.owner_user_id != user_id:
        # OQ-35: what's in someone else's shared folder stays theirs; editors copy it instead.
        raise AppError(
            403,
            ErrorCode.FORBIDDEN,
            "Only the owner can move items out of a shared folder. Copy them instead.",
        )
    live_ids = set(node_repository.lock_live_descendant_ids(session, node.id))
    target = namespace_repository.get(session, destination)
    if target is None:
        raise RuntimeError(f"Namespace {destination} is missing")
    if target.kind == NamespaceKind.SHARED and node_repository.mounts_among(
        session, [node.id, *live_ids]
    ):
        raise AppError(
            409,
            ErrorCode.SHARE_NOT_ALLOWED,
            "A shared folder can't be moved into another shared folder.",
        )
    descendants = node_repository.subtree(session, node.id)
    if old.owner_user_id != target.owner_user_id:
        size = sum(n.size_bytes for n in (node, *descendants) if n.kind == NodeKind.FILE)
        _transfer_quota(session, old.owner_user_id, target.owner_user_id, size)
        # The new owner manages sharing now: grants the old owner made don't carry over.
        _end_file_shares(session, ctx, [node, *descendants])

    now = datetime.now(UTC)
    old_parent_id = node.parent_id
    node.revision += 1
    change_service.record(session, node, ChangeType.REMOVED, user_id, now)
    try:
        node_repository.move_to_namespace(
            session, node, descendants, destination, parent.id, name, normalized
        )
    except LiveNameTakenError:
        raise AppError(
            409, ErrorCode.NAME_CONFLICT, "An item with this name was just created here."
        ) from None
    live = [node, *(n for n in descendants if n.id in live_ids)]
    change_service.record_many(session, live, ChangeType.CREATED, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.NODE_MOVED,
        target=node_target(node),
        details={
            "from_parent_id": str(old_parent_id),
            "to_parent_id": str(parent.id),
            "from_namespace_id": str(source),
            "to_namespace_id": str(destination),
        },
    )
    return to_out(node)


def _end_file_shares(session: Session, ctx: CallContext, nodes: list[Node]) -> None:
    for node in nodes:
        if node.kind != NodeKind.FILE:
            continue
        for share in sharing_repository.file_members(session, node.id):
            grantee = (
                {"grantee_type": "user", "grantee_id": str(share.user_id)}
                if share.user_id is not None
                else {"grantee_type": "group", "grantee_id": str(share.group_id)}
            )
            sharing_repository.remove(session, share)
            audit_service.record(
                session,
                ctx,
                AuditAction.SHARE_REMOVED,
                target=node_target(node),
                details={**grantee, "reason": "moved_to_another_owner"},
            )


def _transfer_quota(
    session: Session, from_user_id: uuid.UUID, to_user_id: uuid.UUID, size: int
) -> None:
    """Both rows locked in ID order, so two moves in opposite directions can't deadlock."""
    users = {
        user_id: user_repository.get_for_update(session, user_id)
        for user_id in sorted((from_user_id, to_user_id), key=str)
    }
    giver, taker = users[from_user_id], users[to_user_id]
    if giver is None or taker is None:
        raise RuntimeError("A namespace owner is missing")
    if size > 0 and taker.used_bytes + size > taker.quota_bytes:
        raise AppError(
            409,
            ErrorCode.QUOTA_EXCEEDED,
            "There isn't enough space left in the shared folder owner's quota.",
        )
    giver.used_bytes -= size
    taker.used_bytes += size


def trash_node(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    if_revision: int,
    if_cursor: int | None = None,
    if_mount_cursor: int | None = None,
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
    mounts = node_repository.mounts_among(session, [node.id, *descendant_ids])
    own_shared = _own_shared_contents(session, user_id, node, mounts)
    if if_cursor is not None and change_repository.any_after(
        session, node.namespace_id, if_cursor, descendant_ids
    ):
        raise revision_conflict(node.revision)
    for mount, contents in own_shared:  # OQ-36: an edit the client hasn't seen wins here too
        assert mount.mount_namespace_id is not None  # noqa: S101 - a mount
        if if_mount_cursor is not None and change_repository.any_after(
            session, mount.mount_namespace_id, if_mount_cursor, contents
        ):
            raise revision_conflict(node.revision)
    now = datetime.now(UTC)
    node.revision += 1
    node_repository.trash(session, node, descendant_ids, user_id, now)
    for mount, contents in own_shared:  # restoring the mount brings them back with it (F3)
        node_repository.trash_below(session, mount, contents, user_id, now)
    # One journal row for the whole subtree: clients remove everything below the node.
    change_service.record(session, node, ChangeType.TRASHED, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.NODE_TRASHED,
        target=node_target(node),
        details={"kind": node.kind.value, "descendants": len(descendant_ids)},
    )
    own = {mount.id for mount, _ in own_shared}
    for mount in mounts:  # deleting someone else's shared folder from your tree is leaving it
        if mount.id in own:
            continue
        audit_service.record(
            session,
            ctx,
            AuditAction.SHARE_LEFT,
            target=node_target(mount),
            details={"namespace_id": str(mount.mount_namespace_id)},
        )


def _own_shared_contents(
    session: Session, user_id: uuid.UUID, node: Node, mounts: list[Node]
) -> list[tuple[Node, list[uuid.UUID]]]:
    """For each of the user's own shared folders among `mounts`: the live items inside it,
    which go to the trash with it. Refused while anyone else has access: the owner's mount is
    the shared folder itself for them, so in v1 the owner removes everyone's access first.
    Also refused inside a folder being deleted: the shared folder is deleted on its own, so
    its journal can be checked for edits the client hasn't seen (OQ-36)."""
    found = []
    for mount in mounts:
        assert mount.mount_namespace_id is not None  # noqa: S101 - mounts_among's filter
        namespace = namespace_repository.get(session, mount.mount_namespace_id)
        if namespace is None or namespace.owner_user_id != user_id:
            continue  # someone else's folder: deleting the mount is leaving it
        if mount.id != node.id:
            raise AppError(
                409,
                ErrorCode.SHARE_NOT_ALLOWED,
                f"This folder holds your shared folder '{mount.name}'. Delete that first.",
            )
        if sharing_repository.namespace_members(session, namespace.id):
            raise AppError(
                409,
                ErrorCode.SHARE_NOT_ALLOWED,
                f"'{mount.name}' is shared with others. Remove everyone's access before "
                "deleting it.",
            )
        root = node_repository.get_root(session, namespace.id)
        if root is not None:
            found.append((mount, node_repository.lock_live_descendant_ids(session, root.id)))
    return found


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
    node = access.require_node(session, user_id, node_id, file_share=True)
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
