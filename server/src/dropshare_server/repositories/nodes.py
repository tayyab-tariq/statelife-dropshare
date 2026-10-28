"""Files and folders. The only code that sets `Node.live_key` and `Node.mount_key`
(database.md §5)."""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dropshare_server.models import FileVersion, Node

_LIVE_KEY_CONSTRAINT = "uq_nodes_live_key"
_IN_LIST_LIMIT = 1000  # Oracle ORA-01795


class LiveNameTakenError(Exception):
    """Another live item in the folder has the same normalized name (raced past the check)."""


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
