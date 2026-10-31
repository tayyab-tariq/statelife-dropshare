"""The trash: list, restore, delete permanently, empty, and the purge job (FR-TRASH-01 … 04).

An item in the trash is the node a user deleted (`trash_root_id == id`); everything that went
with it carries its ID. The owner of a namespace sees its whole trash; an editor of a shared
folder sees what they deleted from it and may restore it; only the owner deletes permanently.
A user's mount of someone else's shared folder (left, or access ended) isn't shown: the folder
comes back from Shared with me, and the purge job removes the mount later (OQ-37).

Trashed items keep counting against the owner's quota until purged (FR-TRASH-04). Purging
removes the database rows; the router or job deletes the blobs after commit, and anything that
fails there is left for the gc-blobs job (storage.md).
"""

import logging
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.errors import AppError
from dropshare_server.models import Node
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import sharing as sharing_repository
from dropshare_server.repositories import uploads as upload_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.repositories.nodes import LiveNameTakenError, MountTakenError
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import blobs as blob_service
from dropshare_server.services import changes as change_service
from dropshare_server.services import nodes as node_service
from dropshare_server.services.context import CallContext
from dropshare_server.storage.base import StorageBackend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import InvalidNameError, normalize_name, validate_name
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.envelope import Meta, PageQuery
from dropshare_shared.schemas.error_details import RevisionConflictDetails
from dropshare_shared.schemas.nodes import AccessRole, NamespaceKind, NodeKind, NodeOut
from dropshare_shared.schemas.sharing import ShareRole
from dropshare_shared.schemas.sync import ChangeType
from dropshare_shared.schemas.trash import EmptyTrashOut, TrashItemOut

logger = logging.getLogger(__name__)

_NAME_MAX = 255  # NFR-COMPAT-02
_RESTORED_TRIES = 100
EMPTY_LIMIT = 200  # items one DELETE /trash purges; the client calls again for more
_PURGE_BATCH = 50


# --- who sees what -------------------------------------------------------------------------------


def _editing(session: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    """The shared folders (namespaces) where the user is an editor, directly or via a group."""
    return sorted(
        {
            row.namespace_id
            for row in sharing_repository.namespace_memberships(session, user_id)
            if row.role == ShareRole.EDITOR
        }
    )


def _others_mount(session: Session, user_id: uuid.UUID, node: Node) -> bool:
    if node.mount_namespace_id is None:
        return False
    namespace = namespace_repository.get(session, node.mount_namespace_id)
    return namespace is None or namespace.owner_user_id != user_id


def _visible(session: Session, user_id: uuid.UUID, node_id: uuid.UUID) -> tuple[Node, AccessRole]:
    """The node, if the user may see it in the trash (or, once restored, in their files)."""
    node = node_repository.get(session, node_id)
    if node is None:
        raise access.not_found()
    role = access.node_role(session, user_id, node)
    if role is None:
        raise access.not_found(denied=True, target=("node", str(node_id)))
    if node.trashed_at is None:
        return node, role
    if node.trash_root_id != node.id:  # it went with something else and comes back with it
        raise access.not_found()
    _require_yours(session, user_id, node, role)
    return node, role


def _require_yours(session: Session, user_id: uuid.UUID, node: Node, role: AccessRole) -> None:
    """In the user's trash: theirs as owner, or deleted by them as an editor (OQ-37). Anyone
    else with a role on the folder is refused as a denial (FR-AUDIT-01); a 404 still, which
    tells a member of the folder nothing new."""
    yours = role == AccessRole.OWNER and not _others_mount(session, user_id, node)
    deleted_by_you = role == AccessRole.EDITOR and node.trashed_by_user_id == user_id
    if not (yours or deleted_by_you):
        raise access.not_found(denied=True, target=("node", str(node.id)))


# --- listing ------------------------------------------------------------------------------------


def list_trash(
    session: Session, user_id: uuid.UUID, page: PageQuery
) -> tuple[list[TrashItemOut], Meta]:
    editing = _editing(session, user_id)
    nodes = node_repository.list_trash(
        session, user_id, editing, offset=(page.page - 1) * page.limit, limit=page.limit
    )
    total = node_repository.count_trash(session, user_id, editing)
    totals = node_repository.trash_totals(session, [node.id for node in nodes])
    names: dict[uuid.UUID, str | None] = {}
    items = []
    for node in nodes:
        assert node.trashed_at is not None and node.name is not None  # noqa: S101 - the filter
        count, size = totals.get(node.id, (0, node.size_bytes))
        location, shared_folder = _where(session, user_id, node)
        by = node.trashed_by_user_id
        if by is not None and by not in names:
            user = user_repository.get(session, by)
            names[by] = None if user is None else user.display_name
        namespace = namespace_repository.get(session, node.namespace_id)
        items.append(
            TrashItemOut(
                id=node.id,
                namespace_id=node.namespace_id,
                name=node.name,
                kind=node.kind,
                size_bytes=size,
                item_count=count,
                trashed_at=node.trashed_at,
                trashed_by=None if by is None else names[by],
                location=location,
                shared_folder=shared_folder,
                can_delete=namespace is not None and namespace.owner_user_id == user_id,
            )
        )
    return items, Meta(page=page.page, limit=page.limit, total=total)


def _where(session: Session, user_id: uuid.UUID, node: Node) -> tuple[str, str | None]:
    """The folder path the item was deleted from, and the shared folder it was in, if any.
    Folders deleted since are still named: they are what restoring recreates."""
    names: list[str] = []
    for ancestor_id in node_repository.ancestor_ids(session, node.id):
        ancestor = node_repository.get(session, ancestor_id)
        if ancestor is not None and ancestor.name is not None:
            names.append(ancestor.name)
    location = "/".join(reversed(names))
    namespace = namespace_repository.get(session, node.namespace_id)
    if namespace is None or namespace.kind != NamespaceKind.SHARED:
        return location, None
    return location, _shared_folder_name(session, user_id, namespace.id, namespace.owner_user_id)


def _shared_folder_name(
    session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID, owner_id: uuid.UUID
) -> str | None:
    """What the user calls the shared folder (their mount's name), else what its owner does."""
    for holder in (user_id, owner_id):
        personal = namespace_repository.get_personal(session, holder)
        mount = (
            None
            if personal is None
            else node_repository.live_mount(session, personal.id, namespace_id)
        )
        if mount is not None:
            return mount.name
    return None


# --- restore ------------------------------------------------------------------------------------


def restore(session: Session, ctx: CallContext, user_id: uuid.UUID, node_id: uuid.UUID) -> NodeOut:
    """Put an item back where it was deleted from (FR-TRASH-02). A deleted parent folder is
    recreated (or a live folder of that name reused); a name taken meanwhile gets "(restored)".
    Restoring again after it succeeded returns the item as it is (NFR-REL-04)."""
    node, role = _visible(session, user_id, node_id)
    if node.trashed_at is None:
        return node_service.to_out(node)

    group = node_repository.lock_trash_group(session, node.id)
    if not group:  # restored or purged by another request since it was read
        raise access.not_found()
    node = group[0]
    _require_yours(session, user_id, node, role)  # again, now that it is locked
    _require_shared_folder_live(session, node)
    parent, created = _live_parent(session, user_id, node)
    original = node.name
    assert original is not None  # noqa: S101 - never a root
    name, normalized = _free_name(session, parent, original, node.kind)
    node.parent_id, node.name, node.name_normalized = parent.id, name, normalized

    restored, mounts = _restorable(session, user_id, node, group)
    _free_names_beside(session, node, restored)
    # A new revision for each: clients remember a trashed item's revision and ignore anything
    # about it at that revision or below, so an unchanged one would never come back.
    for item in restored:
        item.revision += 1
    try:
        node_repository.restore(session, restored, mounts)
    except (LiveNameTakenError, MountTakenError):
        raise _changed_meanwhile() from None

    now = datetime.now(UTC)
    for namespace_nodes in _by_namespace(restored):  # clients dropped them on TRASHED
        change_service.record_many(session, namespace_nodes, ChangeType.CREATED, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.NODE_RESTORED,
        target=node_service.node_target(node),
        details={
            "kind": node.kind.value,
            "items": len(restored) - 1,
            "renamed": name != original,
            "recreated_folders": created,
        },
    )
    return node_service.to_out(node)


def _changed_meanwhile() -> AppError:
    """Something the restore depends on changed while it ran; trying again sorts it out."""
    return AppError(
        409,
        ErrorCode.REVISION_CONFLICT,
        "Something here changed while restoring. Try again.",
        RevisionConflictDetails(current_revision=None).model_dump(mode="json"),
    )


def _require_shared_folder_live(session: Session, node: Node) -> None:
    """An item deleted from a shared folder that has itself been deleted since comes back with
    the folder, not on its own (OQ-37). The owner's mount is locked, as deleting it locks it,
    so the folder can't go to the trash while this restore runs."""
    namespace = namespace_repository.get(session, node.namespace_id)
    if namespace is None or namespace.kind != NamespaceKind.SHARED:
        return
    personal = namespace_repository.get_personal(session, namespace.owner_user_id)
    mount = (
        None if personal is None else node_repository.live_mount(session, personal.id, namespace.id)
    )
    locked = None if mount is None else node_repository.get_for_update(session, mount.id)
    if locked is None or locked.trashed_at is not None:
        raise AppError(
            409,
            ErrorCode.SHARE_NOT_ALLOWED,
            "The shared folder this was in has been deleted. Restore that folder first.",
        )


def _live_parent(session: Session, user_id: uuid.UUID, node: Node) -> tuple[Node, int]:
    """The live folder to restore into, recreating deleted folders on the way down; and how
    many it created. The live one is locked, as creating a folder does."""
    missing: list[Node] = []
    assert node.parent_id is not None  # noqa: S101 - never a root
    parent = node_repository.get(session, node.parent_id)
    while parent is not None and parent.trashed_at is not None:
        missing.append(parent)
        parent = (
            None if parent.parent_id is None else node_repository.get(session, parent.parent_id)
        )
    if parent is None:  # roots are never trashed
        raise RuntimeError(f"No live folder above node {node.id}")
    locked = node_repository.get_for_update(session, parent.id)
    if locked is None or locked.trashed_at is not None:  # deleted while this was looking
        raise _changed_meanwhile()
    parent = locked
    for gone in reversed(missing):
        assert gone.name is not None  # noqa: S101 - never a root
        parent = _folder_named(session, user_id, parent, gone.name)
    return parent, len(missing)


def _folder_named(session: Session, user_id: uuid.UUID, parent: Node, name: str) -> Node:
    """The live folder `name` in `parent`, created if there is none. A file (or a shared
    folder) holding the name means a new folder with "(restored)"."""
    existing = node_repository.get_live_child(
        session, parent.namespace_id, parent.id, normalize_name(name)
    )
    if (
        existing is not None
        and existing.kind == NodeKind.FOLDER
        and existing.mount_namespace_id is None
    ):
        locked = node_repository.get_for_update(session, existing.id)
        if (
            locked is None
            or locked.trashed_at is not None
            or locked.parent_id != parent.id
            or locked.name_normalized != existing.name_normalized
        ):
            raise _changed_meanwhile()
        return locked
    name, normalized = _free_name(session, parent, name, NodeKind.FOLDER)
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
    node_service.add_live(session, folder)
    change_service.record(session, folder, ChangeType.CREATED, user_id, now)
    return folder


def restored_name(name: str, kind: NodeKind, attempt: int) -> str:
    """ "Report (restored).docx", then "Report (restored 2).docx", … within 255 characters."""
    suffix = " (restored)" if attempt == 1 else f" (restored {attempt})"
    stem, extension = name, ""
    dot = name.rfind(".")
    if kind == NodeKind.FILE and dot > 0:
        stem, extension = name[:dot], name[dot:]
    room = _NAME_MAX - len(suffix) - len(extension)
    if room < 1:  # an extension that long is part of the name
        stem, extension, room = name, "", _NAME_MAX - len(suffix)
    stem = stem[:room].rstrip(". ")
    return (stem + suffix if stem else suffix.lstrip()) + extension


def _free_name(session: Session, parent: Node, name: str, kind: NodeKind) -> tuple[str, str]:
    candidate = name
    for attempt in range(1, _RESTORED_TRIES + 1):
        try:
            nfc = validate_name(candidate)
        except InvalidNameError:
            nfc = None
        if nfc is not None:
            normalized = normalize_name(nfc)
            if (
                node_repository.get_live_child(session, parent.namespace_id, parent.id, normalized)
                is None
            ):
                return nfc, normalized
        candidate = restored_name(name, kind, attempt)
    raise AppError(
        409, ErrorCode.NAME_CONFLICT, "There's no free name for this item in its folder."
    )


def _free_names_beside(session: Session, node: Node, restored: Sequence[Node]) -> None:
    """Items coming back into a folder that stayed live, other than `node` itself: the contents
    of the user's own shared folder, under its root. Anything given their names meanwhile keeps
    its name; they get "(restored)" instead, so the restore can always finish."""
    ids = {item.id for item in restored}
    for item in restored:
        if item is node or item.parent_id is None or item.parent_id in ids:
            continue
        parent = node_repository.get(session, item.parent_id)
        assert parent is not None and item.name is not None  # noqa: S101 - restored items
        item.name, item.name_normalized = _free_name(session, parent, item.name, item.kind)


def _restorable(
    session: Session, user_id: uuid.UUID, node: Node, group: Sequence[Node]
) -> tuple[list[Node], list[Node]]:
    """What comes back (in the group's order), and the mounts among it. A mount of someone
    else's shared folder comes back only while the user still has access and hasn't added the
    folder again since; otherwise it stays in the trash on its own, hidden (OQ-37)."""
    restored: list[Node] = []
    mounts: list[Node] = []
    for item in group:
        if item.mount_namespace_id is not None:
            if item.id != node.id and not _mount_can_return(session, user_id, item):
                item.trash_root_id = item.id
                continue
            mounts.append(item)
        restored.append(item)
    return restored, mounts


def _mount_can_return(session: Session, user_id: uuid.UUID, mount: Node) -> bool:
    assert mount.mount_namespace_id is not None  # noqa: S101 - a mount
    return (
        access.namespace_role(session, user_id, mount.mount_namespace_id) is not None
        and node_repository.live_mount(session, mount.namespace_id, mount.mount_namespace_id)
        is None
    )


def _by_namespace(nodes: Iterable[Node]) -> list[list[Node]]:
    grouped: dict[uuid.UUID, list[Node]] = {}
    for node in nodes:
        grouped.setdefault(node.namespace_id, []).append(node)
    return list(grouped.values())


# --- permanent delete ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Purged:
    items: int  # the item and everything that went to the trash with it
    freed_bytes: int
    blob_keys: list[str]  # to delete from storage once the transaction has committed


def delete_forever(
    session: Session, ctx: CallContext, user_id: uuid.UUID, node_id: uuid.UUID
) -> Purged:
    """Permanently delete one item in the trash (FR-TRASH-03); only its owner may."""
    node, role = _visible(session, user_id, node_id)
    if node.trashed_at is None:
        raise _restored_meanwhile(node)
    if role != AccessRole.OWNER:
        raise AppError(
            403,
            ErrorCode.FORBIDDEN,
            "Only the owner can delete this permanently.",
            target=("node", str(node_id)),
        )
    target = node_service.node_target(node)
    kind = node.kind
    purged = _purge(session, node.id)
    if purged is None:  # restored or purged by another request since it was read
        live = node_repository.get(session, node_id)
        if live is not None and live.trashed_at is None:
            raise _restored_meanwhile(live)
        raise access.not_found()
    _audit_purge(session, ctx, target, kind, purged, automatic=False)
    return purged


def _restored_meanwhile(node: Node) -> AppError:
    return AppError(
        409,
        ErrorCode.REVISION_CONFLICT,
        "It was restored meanwhile, so it isn't in the trash any more.",
        RevisionConflictDetails(current_revision=node.revision).model_dump(mode="json"),
    )


def empty_trash(
    session: Session, ctx: CallContext, user_id: uuid.UUID
) -> tuple[EmptyTrashOut, list[str]]:
    """Permanently delete the trash of the namespaces the user owns, up to EMPTY_LIMIT items
    per call so one request never holds its locks for long; `more` says to call again.
    `removed` counts what the user saw in their trash: a hidden mount of someone else's
    shared folder goes too, uncounted (OQ-37)."""
    removed = freed = 0
    keys: list[str] = []
    left = False  # an item skipped this time
    batch = node_repository.owned_trash_roots(session, user_id, EMPTY_LIMIT + 1)
    for root_id in batch[:EMPTY_LIMIT]:
        node = node_repository.get(session, root_id)
        if node is None:
            continue
        target, kind = node_service.node_target(node), node.kind
        shown = not _others_mount(session, user_id, node)
        try:
            purged = _purge(session, root_id)
        except AppError:  # an upload into it is finishing: left for the next call
            left = True
            continue
        if purged is None:
            continue
        _audit_purge(session, ctx, target, kind, purged, automatic=False)
        removed += 1 if shown else 0
        freed += purged.freed_bytes
        keys.extend(purged.blob_keys)
    more = left or len(batch) > EMPTY_LIMIT
    audit_service.record(
        session,
        ctx,
        AuditAction.TRASH_EMPTIED,
        details={"items": removed, "freed_bytes": freed, "more": more},
    )
    return EmptyTrashOut(removed=removed, freed_bytes=freed, more=more), keys


def _audit_purge(
    session: Session,
    ctx: CallContext,
    target: audit_service.AuditTarget,
    kind: NodeKind,
    purged: Purged,
    *,
    automatic: bool,
) -> None:
    audit_service.record(
        session,
        ctx,
        AuditAction.NODE_PURGED,
        target=target,
        details={
            "kind": kind.value,
            "items": purged.items - 1,
            "freed_bytes": purged.freed_bytes,
            "automatic": automatic,
        },
    )


def _upload_finishing() -> AppError:
    return AppError(
        409,
        ErrorCode.REVISION_CONFLICT,
        "An upload into this item is finishing. Try again in a moment.",
        RevisionConflictDetails(current_revision=None).model_dump(mode="json"),
    )


def _purge(
    session: Session, root_id: uuid.UUID, *, trashed_before: datetime | None = None
) -> Purged | None:
    """Delete an item in the trash and everything that went with it: rows only, blobs after
    commit. Items deleted earlier from inside it stay in the trash, moved up to the nearest
    folder that remains (OQ-37). None if it isn't in the trash any more (or, with
    `trashed_before`, went there later). Raises 409 while an upload into it is finishing.

    Lock order: the owner's user row, then the nodes, as moves between namespaces do; uploads
    last and without waiting, since completing one holds its row while it locks nodes."""
    root = node_repository.get(session, root_id)
    if root is None or root.trash_root_id != root.id:
        return None
    _lock_owner(session, root)
    group = node_repository.lock_trash_group(session, root_id)
    if not group or (
        trashed_before is not None
        and (group[0].trashed_at is None or group[0].trashed_at >= trashed_before)
    ):
        return None
    by_id = {node.id: node for node in group}
    ids = list(by_id)
    for orphan in node_repository.children_outside(session, ids):
        if orphan.trashed_at is None:  # never under a trashed folder; don't make it worse
            raise RuntimeError(f"Live node {orphan.id} under trashed node {orphan.parent_id}")
        above = by_id[orphan.parent_id] if orphan.parent_id is not None else None
        while above is not None and above.parent_id in by_id:
            above = by_id[above.parent_id]
        orphan.parent_id = None if above is None else above.parent_id
    if not upload_repository.lock_for_nodes(session, ids):
        raise _upload_finishing()

    _release_quota(session, group)
    versions = node_repository.versions_of(session, ids)
    sharing_repository.delete_file_shares(session, ids)
    upload_repository.delete_for_nodes(session, ids)
    node_repository.delete_versions(session, [version.id for version in versions])
    node_repository.delete_nodes(session, group)
    return Purged(
        items=len(ids),
        freed_bytes=sum(node.size_bytes for node in group if node.kind == NodeKind.FILE),
        blob_keys=[version.blob_key for version in versions],
    )


def _lock_owner(session: Session, root: Node) -> None:
    """Lock the user whose quota the purge changes: the namespace owner (for the user's own
    shared folder, the same person owns its contents)."""
    namespace = namespace_repository.get(session, root.namespace_id)
    if namespace is not None:
        user_repository.get_for_update(session, namespace.owner_user_id)


def _release_quota(session: Session, nodes: Sequence[Node]) -> None:
    """Purged files stop counting against their namespace owner's quota (FR-TRASH-04)."""
    by_namespace: dict[uuid.UUID, int] = {}
    for node in nodes:
        if node.kind == NodeKind.FILE:
            by_namespace[node.namespace_id] = by_namespace.get(node.namespace_id, 0) + (
                node.size_bytes
            )
    by_owner: dict[uuid.UUID, int] = {}
    for namespace_id, size in by_namespace.items():
        namespace = namespace_repository.get(session, namespace_id)
        if namespace is not None:
            owner = namespace.owner_user_id
            by_owner[owner] = by_owner.get(owner, 0) + size
    for owner_id in sorted(by_owner):  # one lock order, whoever purges
        user = user_repository.get_for_update(session, owner_id)
        if user is not None:
            user.used_bytes = max(0, user.used_bytes - by_owner[owner_id])


# --- the purge-trash job ------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeResult:
    items: int  # items in the trash purged (each with what went to the trash with it)
    freed_bytes: int


def purge_expired(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    ctx: CallContext,
    now: datetime,
    retention_days: int,
) -> PurgeResult:
    """The purge-trash job (FR-TRASH-04, NFR-REL-05): permanently delete what has been in the
    trash longer than the retention period, a batch per transaction. Idempotent and safe to run
    while the API is serving: each item is locked and checked again before it goes."""
    before = now - timedelta(days=retention_days)
    items = freed = 0
    while True:
        with session_factory.begin() as session:
            batch = node_repository.expired_trash_roots(session, before, _PURGE_BATCH)
            keys: list[str] = []
            done = 0
            for root_id in batch:
                node = node_repository.get(session, root_id)
                if node is None:
                    continue
                target, kind = node_service.node_target(node), node.kind
                try:
                    # Checked again under the lock: restored, purged or deleted again since.
                    purged = _purge(session, root_id, trashed_before=before)
                except AppError:
                    logger.info("Skipping %s: an upload into it is finishing", root_id)
                    continue
                if purged is None:
                    continue
                _audit_purge(session, ctx, target, kind, purged, automatic=True)
                done += 1
                freed += purged.freed_bytes
                keys.extend(purged.blob_keys)
        blob_service.delete_blobs(storage, keys)
        items += done
        if len(batch) < _PURGE_BATCH or done == 0:  # 0: only items it must leave for now
            return PurgeResult(items=items, freed_bytes=freed)
