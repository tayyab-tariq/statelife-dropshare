"""Files and folders. The only code that sets `Node.live_key` (database.md §5)."""

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


def add(session: Session, node: Node) -> None:
    """Insert a live node; raises LiveNameTakenError on a name collision.

    The insert runs in a savepoint, so after a collision the session is still usable and the
    caller can look up the item that holds the name.
    """
    node.live_key = live_key(node.namespace_id, node.parent_id, node.name_normalized)
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
    """Move a node and its live descendants to the trash; they stop holding their names."""
    node.trashed_at, node.trashed_by_user_id, node.trash_root_id, node.live_key = (
        at,
        by,
        node.id,
        None,
    )
    for start in range(0, len(descendant_ids), _IN_LIST_LIMIT):
        session.execute(
            update(Node)
            .where(Node.id.in_(descendant_ids[start : start + _IN_LIST_LIMIT]))
            .values(trashed_at=at, trashed_by_user_id=by, trash_root_id=node.id, live_key=None)
            .execution_options(synchronize_session=False)
        )
    session.flush()


def add_version(session: Session, version: FileVersion) -> None:
    session.add(version)
    session.flush()


def get_version(session: Session, version_id: uuid.UUID) -> FileVersion | None:
    return session.get(FileVersion, version_id)
