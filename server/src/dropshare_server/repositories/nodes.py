"""Files and folders. The only code that sets `Node.live_key` and `Node.mount_key`
(database.md §5)."""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, Select, and_, delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from dropshare_server.models import FileVersion, Namespace, Node

_LIVE_KEY_CONSTRAINT = "uq_nodes_live_key"
_MOUNT_KEY_CONSTRAINT = "uq_nodes_mount_key"
_IN_LIST_LIMIT = 1000  # Oracle ORA-01795


class LiveNameTakenError(Exception):
    """Another live item in the folder has the same normalized name (raced past the check)."""


class MountTakenError(Exception):
    """The user already has this shared folder (another request added it meanwhile)."""


def live_key(
    namespace_id: uuid.UUID, parent_id: uuid.UUID | None, name_normalized: str | None
) -> str:
    if parent_id is None:
        return hashlib.sha256(f"{namespace_id}/root".encode()).hexdigest()
    return hashlib.sha256(f"{namespace_id}/{parent_id}/{name_normalized}".encode()).hexdigest()


def mount_key(namespace_id: uuid.UUID, mount_namespace_id: uuid.UUID) -> str:
    return hashlib.sha256(f"{namespace_id}/mount/{mount_namespace_id}".encode()).hexdigest()


def add(session: Session, node: Node) -> None:
    """Insert a live node; raises LiveNameTakenError on a name collision.

    The insert runs in a savepoint, so after a collision the session is still usable and the
    caller can look up the item that holds the name. A mount (`mount_namespace_id` set) gets
    its `mount_key` here too: one live mount of a shared folder per user's namespace.
    """
    node.live_key = live_key(node.namespace_id, node.parent_id, node.name_normalized)
    if node.mount_namespace_id is not None:
        node.mount_key = mount_key(node.namespace_id, node.mount_namespace_id)
    try:
        with session.begin_nested():
            session.add(node)
    except IntegrityError as exc:
        if _LIVE_KEY_CONSTRAINT in str(exc.orig).lower():
            raise LiveNameTakenError from exc
        if _MOUNT_KEY_CONSTRAINT in str(exc.orig).lower():
            raise MountTakenError from exc
        raise


def move(
    session: Session,
    node: Node,
    parent_id: uuid.UUID,
    name: str,
    name_normalized: str,
) -> None:
    """Give a live node a new parent and/or name; raises LiveNameTakenError on a collision.

    Runs in a savepoint like `add`, so the caller can still look up the item holding the name.
    """
    try:
        with session.begin_nested():
            node.parent_id, node.name, node.name_normalized = parent_id, name, name_normalized
            node.live_key = live_key(node.namespace_id, parent_id, name_normalized)
            session.flush()
    except IntegrityError as exc:
        if _LIVE_KEY_CONSTRAINT in str(exc.orig).lower():
            raise LiveNameTakenError from exc
        raise


def ancestor_ids(session: Session, node_id: uuid.UUID) -> list[uuid.UUID]:
    """`node_id`'s parent, its parent, and so on up to the namespace root."""
    found: list[uuid.UUID] = []
    current: uuid.UUID | None = node_id
    while current is not None:
        current = session.scalar(select(Node.parent_id).where(Node.id == current))
        if current is not None:
            if current in found:  # a cycle would be a bug elsewhere; never loop on it
                raise RuntimeError(f"Folder cycle above node {node_id}")
            found.append(current)
    return found


def get(session: Session, node_id: uuid.UUID) -> Node | None:
    return session.get(Node, node_id)


def get_for_update(session: Session, node_id: uuid.UUID) -> Node | None:
    """Lock the row so a revision check and the write that follows are atomic (database.md §7)."""
    return session.scalars(
        select(Node)
        .where(Node.id == node_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def lock_root(session: Session, namespace_id: uuid.UUID) -> None:
    """Lock the namespace's root folder row until commit."""
    session.execute(
        select(Node.id).where(Node.live_key == live_key(namespace_id, None, None)).with_for_update()
    )


def get_root(session: Session, namespace_id: uuid.UUID) -> Node | None:
    return session.scalars(
        select(Node).where(Node.live_key == live_key(namespace_id, None, None))
    ).one_or_none()


def get_live_child(
    session: Session, namespace_id: uuid.UUID, parent_id: uuid.UUID, name_normalized: str
) -> Node | None:
    return session.scalars(
        select(Node).where(Node.live_key == live_key(namespace_id, parent_id, name_normalized))
    ).one_or_none()


def count_live_children(session: Session, parent_id: uuid.UUID) -> int:
    return (
        session.scalar(
            select(func.count()).where(Node.parent_id == parent_id, Node.trashed_at.is_(None))
        )
        or 0
    )


def list_live_children(
    session: Session, parent_id: uuid.UUID, *, offset: int, limit: int
) -> list[Node]:
    return list(
        session.scalars(
            select(Node)
            .where(Node.parent_id == parent_id, Node.trashed_at.is_(None))
            .order_by(Node.kind.desc(), Node.name_normalized, Node.id)  # folders first
            .offset(offset)
            .limit(limit)
        )
    )


def lock_live_descendant_ids(session: Session, node_id: uuid.UUID) -> list[uuid.UUID]:
    """Lock and return every live node below `node_id`, one level at a time.

    Each level is read after the level above is locked, and creating an item locks its parent
    first, so nothing can be added inside the subtree while it is being trashed.
    """
    found: list[uuid.UUID] = []
    level = [node_id]
    while level:
        next_level: list[uuid.UUID] = []
        for start in range(0, len(level), _IN_LIST_LIMIT):
            batch = level[start : start + _IN_LIST_LIMIT]
            next_level.extend(
                session.scalars(
                    select(Node.id)
                    .where(Node.parent_id.in_(batch), Node.trashed_at.is_(None))
                    .with_for_update()
                )
            )
        found.extend(next_level)
        level = next_level
    return found


def trash(
    session: Session, node: Node, descendant_ids: Sequence[uuid.UUID], by: uuid.UUID, at: datetime
) -> None:
    """Move a node and its live descendants to the trash; they stop holding their names, and
    mounts among them stop counting as the user's mount of that shared folder."""
    node.trashed_at, node.trashed_by_user_id, node.trash_root_id = at, by, node.id
    node.live_key = node.mount_key = None
    for start in range(0, len(descendant_ids), _IN_LIST_LIMIT):
        session.execute(
            update(Node)
            .where(Node.id.in_(descendant_ids[start : start + _IN_LIST_LIMIT]))
            .values(
                trashed_at=at,
                trashed_by_user_id=by,
                trash_root_id=node.id,
                live_key=None,
                mount_key=None,
            )
            .execution_options(synchronize_session=False)
        )
    session.flush()


def move_to_namespace(
    session: Session,
    node: Node,
    descendants: Sequence[Node],
    namespace_id: uuid.UUID,
    parent_id: uuid.UUID,
    name: str,
    name_normalized: str,
) -> None:
    """Move `node` with everything below it (IDs kept) under `parent_id` in another namespace;
    raises LiveNameTakenError if the name was taken meanwhile. In a savepoint, like `move`."""
    try:
        with session.begin_nested():
            node.namespace_id, node.parent_id = namespace_id, parent_id
            node.name, node.name_normalized = name, name_normalized
            node.live_key = live_key(namespace_id, parent_id, name_normalized)
            for item in descendants:
                item.namespace_id = namespace_id
                if item.live_key is not None:
                    item.live_key = live_key(namespace_id, item.parent_id, item.name_normalized)
            session.flush()
    except IntegrityError as exc:
        if _LIVE_KEY_CONSTRAINT in str(exc.orig).lower():
            raise LiveNameTakenError from exc
        raise


def trash_below(
    session: Session, trash_root: Node, node_ids: Sequence[uuid.UUID], by: uuid.UUID, at: datetime
) -> None:
    """Put `node_ids` in the trash as part of `trash_root`'s deletion (a shared folder's
    contents, deleted with the owner's mount of it)."""
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        session.execute(
            update(Node)
            .where(Node.id.in_(node_ids[start : start + _IN_LIST_LIMIT]))
            .values(
                trashed_at=at,
                trashed_by_user_id=by,
                trash_root_id=trash_root.id,
                live_key=None,
                mount_key=None,
            )
            .execution_options(synchronize_session=False)
        )
    session.flush()


def add_version(session: Session, version: FileVersion) -> None:
    session.add(version)
    session.flush()


def get_version(session: Session, version_id: uuid.UUID) -> FileVersion | None:
    return session.get(FileVersion, version_id)


def live_mount(
    session: Session, namespace_id: uuid.UUID, mount_namespace_id: uuid.UUID
) -> Node | None:
    """The user's live mount (in their namespace `namespace_id`) of a shared folder."""
    return session.scalars(
        select(Node).where(Node.mount_key == mount_key(namespace_id, mount_namespace_id))
    ).one_or_none()


def ever_mounted(session: Session, namespace_id: uuid.UUID, mount_namespace_id: uuid.UUID) -> bool:
    """Whether the user's namespace holds a mount of that shared folder, live or trashed."""
    found = session.scalar(
        select(Node.id)
        .where(Node.namespace_id == namespace_id, Node.mount_namespace_id == mount_namespace_id)
        .limit(1)
    )
    return found is not None


def live_mounts(session: Session, namespace_id: uuid.UUID) -> list[Node]:
    """Every live mount in a user's namespace."""
    return list(
        session.scalars(
            select(Node)
            .where(
                Node.namespace_id == namespace_id,
                Node.mount_namespace_id.is_not(None),
                Node.trashed_at.is_(None),
            )
            .order_by(Node.created_at, Node.id)
        )
    )


def mounts_among(session: Session, node_ids: Sequence[uuid.UUID]) -> list[Node]:
    """The mounts among these nodes."""
    found: list[Node] = []
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        found.extend(
            session.scalars(
                select(Node).where(
                    Node.id.in_(node_ids[start : start + _IN_LIST_LIMIT]),
                    Node.mount_namespace_id.is_not(None),
                )
            )
        )
    return found


def subtree(session: Session, node_id: uuid.UUID) -> list[Node]:
    """Every node below `node_id`, live or trashed, parents before children."""
    found: list[Node] = []
    level = [node_id]
    while level:
        children: list[Node] = []
        for start in range(0, len(level), _IN_LIST_LIMIT):
            children.extend(
                session.scalars(
                    select(Node)
                    .where(Node.parent_id.in_(level[start : start + _IN_LIST_LIMIT]))
                    .order_by(Node.id)
                )
            )
        found.extend(children)
        level = [child.id for child in children]
    return found


def rehome(
    session: Session, root: Node, descendants: Sequence[Node], namespace_id: uuid.UUID
) -> None:
    """Make `root` the root folder of `namespace_id`, with everything below it (IDs kept).

    The new keys all contain the new namespace, so they can't collide with the old ones.
    """
    root.namespace_id, root.parent_id, root.name, root.name_normalized = (
        namespace_id,
        None,
        None,
        None,
    )
    root.live_key = live_key(namespace_id, None, None)
    for node in descendants:
        node.namespace_id = namespace_id
        if node.live_key is not None:
            node.live_key = live_key(namespace_id, node.parent_id, node.name_normalized)
    session.flush()


# --- the trash (FR-TRASH-01 … 04) --------------------------------------------------------------


def _owned(user_id: uuid.UUID) -> Select[uuid.UUID]:
    return select(Namespace.id).where(Namespace.owner_user_id == user_id)


def _trash_roots_visible(user_id: uuid.UUID, editing: Sequence[uuid.UUID]) -> ColumnElement[bool]:
    """Items the user deleted or owns in the trash, as a filter: in a namespace they own (but
    not a mount of someone else's shared folder: that comes back from Shared with me, OQ-37),
    or deleted by them from a shared folder they can still edit (`editing`)."""
    owned = _owned(user_id)
    yours = and_(
        Node.namespace_id.in_(owned),
        or_(Node.mount_namespace_id.is_(None), Node.mount_namespace_id.in_(owned)),
    )
    if editing:
        in_editing = or_(
            *(
                Node.namespace_id.in_(editing[start : start + _IN_LIST_LIMIT])
                for start in range(0, len(editing), _IN_LIST_LIMIT)
            )
        )
        yours = or_(yours, and_(in_editing, Node.trashed_by_user_id == user_id))
    return and_(Node.trash_root_id == Node.id, yours)


def list_trash(
    session: Session,
    user_id: uuid.UUID,
    editing: Sequence[uuid.UUID],
    *,
    offset: int,
    limit: int,
) -> list[Node]:
    """The user's trash, most recently deleted first. `editing`: the shared folders'
    namespaces where they are an editor (few: one per share)."""
    return list(
        session.scalars(
            select(Node)
            .where(_trash_roots_visible(user_id, editing))
            .order_by(Node.trashed_at.desc(), Node.id)
            .offset(offset)
            .limit(limit)
        )
    )


def count_trash(session: Session, user_id: uuid.UUID, editing: Sequence[uuid.UUID]) -> int:
    return session.scalar(select(func.count()).where(_trash_roots_visible(user_id, editing))) or 0


def trash_totals(
    session: Session, root_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[int, int]]:
    """For each item in the trash: (how many items went with it, bytes of the files among
    them, its own included)."""
    found: dict[uuid.UUID, tuple[int, int]] = {}
    for start in range(0, len(root_ids), _IN_LIST_LIMIT):
        rows = session.execute(
            select(Node.trash_root_id, func.count(), func.coalesce(func.sum(Node.size_bytes), 0))
            .where(Node.trash_root_id.in_(root_ids[start : start + _IN_LIST_LIMIT]))
            .group_by(Node.trash_root_id)
        )
        for root_id, count, size in rows:
            if root_id is not None:
                found[root_id] = (int(count) - 1, int(size))
    return found


def owned_trash_roots(session: Session, user_id: uuid.UUID, limit: int) -> list[uuid.UUID]:
    """Up to `limit` items in the trash of the namespaces the user owns, oldest first (emptying
    the trash). Mounts of other people's shared folders count: they are the user's too."""
    return list(
        session.scalars(
            select(Node.id)
            .where(Node.trash_root_id == Node.id, Node.namespace_id.in_(_owned(user_id)))
            .order_by(Node.trashed_at, Node.id)
            .limit(limit)
        )
    )


def expired_trash_roots(session: Session, before: datetime, limit: int) -> list[uuid.UUID]:
    """Up to `limit` items deleted before `before`, oldest first (FR-TRASH-04)."""
    return list(
        session.scalars(
            select(Node.id)
            .where(Node.trash_root_id == Node.id, Node.trashed_at < before)
            .order_by(Node.trashed_at, Node.id)
            .limit(limit)
        )
    )


def lock_trash_group(session: Session, root_id: uuid.UUID) -> list[Node]:
    """Lock and return what went to the trash with `root_id` (the root first), parents before
    children within each namespace. Empty if it isn't in the trash (any more)."""
    nodes = list(
        session.scalars(
            select(Node)
            .where(Node.trash_root_id == root_id)
            .order_by(Node.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    by_id = {node.id: node for node in nodes}
    if root_id not in by_id:
        return []
    depth: dict[uuid.UUID, int] = {}
    for node in nodes:
        chain: list[Node] = []  # up to the first node whose depth is known (iterative)
        current: Node | None = node
        while current is not None and current.id not in depth:
            chain.append(current)
            parent = by_id.get(current.parent_id) if current.parent_id is not None else None
            current = None if current.id == root_id else parent
        known = -1 if current is None else depth[current.id]
        for offset, item in enumerate(reversed(chain), start=1):
            depth[item.id] = known + offset
    return sorted(nodes, key=lambda node: (node.id != root_id, depth[node.id]))


def children_outside(session: Session, node_ids: Sequence[uuid.UUID]) -> list[Node]:
    """Nodes whose parent is among `node_ids` but which aren't themselves: items deleted from
    inside a folder before the folder was."""
    ids = set(node_ids)
    found: list[Node] = []
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        found.extend(
            node
            for node in session.scalars(
                select(Node)
                .where(Node.parent_id.in_(node_ids[start : start + _IN_LIST_LIMIT]))
                .with_for_update()
            )
            if node.id not in ids
        )
    return found


def restore(session: Session, nodes: Sequence[Node], mounts: Sequence[Node]) -> None:
    """Bring nodes back from the trash with their (already set) parents and names; raises
    LiveNameTakenError or MountTakenError if a name or a shared folder was taken meanwhile.
    In a savepoint, like `add`. `mounts` (among `nodes`) count as the user's mounts again."""
    try:
        with session.begin_nested():
            for node in nodes:
                node.trashed_at = node.trashed_by_user_id = node.trash_root_id = None
                node.live_key = live_key(node.namespace_id, node.parent_id, node.name_normalized)
            for mount in mounts:
                assert mount.mount_namespace_id is not None  # noqa: S101 - a mount
                mount.mount_key = mount_key(mount.namespace_id, mount.mount_namespace_id)
            session.flush()
    except IntegrityError as exc:
        if _LIVE_KEY_CONSTRAINT in str(exc.orig).lower():
            raise LiveNameTakenError from exc
        if _MOUNT_KEY_CONSTRAINT in str(exc.orig).lower():
            raise MountTakenError from exc
        raise


def versions_of(session: Session, node_ids: Sequence[uuid.UUID]) -> list[FileVersion]:
    found: list[FileVersion] = []
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        found.extend(
            session.scalars(
                select(FileVersion).where(
                    FileVersion.node_id.in_(node_ids[start : start + _IN_LIST_LIMIT])
                )
            )
        )
    return found


def delete_versions(session: Session, version_ids: Sequence[uuid.UUID]) -> None:
    for start in range(0, len(version_ids), _IN_LIST_LIMIT):
        session.execute(
            delete(FileVersion)
            .where(FileVersion.id.in_(version_ids[start : start + _IN_LIST_LIMIT]))
            .execution_options(synchronize_session=False)
        )


def delete_nodes(session: Session, nodes: Sequence[Node]) -> None:
    """Delete nodes whose versions, shares and uploads are already gone. Their parent links are
    cleared first, so the rows can go in any order (no ON DELETE CASCADE, database.md)."""
    session.flush()
    node_ids = [node.id for node in nodes]
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        session.execute(
            update(Node)
            .where(Node.id.in_(node_ids[start : start + _IN_LIST_LIMIT]))
            .values(parent_id=None)
            .execution_options(synchronize_session=False)
        )
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        session.execute(
            delete(Node)
            .where(Node.id.in_(node_ids[start : start + _IN_LIST_LIMIT]))
            .execution_options(synchronize_session=False)
        )
    for node in nodes:  # gone: nothing may flush them again
        session.expunge(node)


def superseded_versions(session: Session, before: datetime, limit: int) -> list[FileVersion]:
    """Up to `limit` versions that a newer version of the same file replaced before `before`
    (v1 keeps only the current version, requirements §8)."""
    newer = aliased(FileVersion)
    return list(
        session.scalars(
            select(FileVersion)
            .join(Node, Node.id == FileVersion.node_id)
            .where(
                or_(Node.current_version_id.is_(None), Node.current_version_id != FileVersion.id),
                exists().where(
                    newer.node_id == FileVersion.node_id,
                    newer.id != FileVersion.id,
                    newer.created_at >= FileVersion.created_at,
                    newer.created_at < before,
                ),
            )
            .order_by(FileVersion.created_at, FileVersion.id)
            .limit(limit)
        )
    )


def known_blob_keys(session: Session, blob_keys: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(blob_keys), _IN_LIST_LIMIT):
        found.update(
            session.scalars(
                select(FileVersion.blob_key).where(
                    FileVersion.blob_key.in_(blob_keys[start : start + _IN_LIST_LIMIT])
                )
            )
        )
    return found
