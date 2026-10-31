"""The change journal: insert and read only, like the audit log."""

import uuid
from collections.abc import Collection, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dropshare_server.models import Change


def add(session: Session, change: Change) -> None:
    session.add(change)
    session.flush()


def add_all(session: Session, changes: Sequence[Change]) -> None:
    session.add_all(changes)
    session.flush()


def list_after(session: Session, namespace_id: uuid.UUID, cursor: int, limit: int) -> list[Change]:
    """Journal rows after `cursor`. Read from `changes` alone: rows of purged nodes still count."""
    return list(
        session.scalars(
            select(Change)
            .where(Change.namespace_id == namespace_id, Change.seq > cursor)
            .order_by(Change.seq)
            .limit(limit)
        )
    )


_IN_LIST_LIMIT = 1000  # Oracle ORA-01795


def any_after(
    session: Session, namespace_id: uuid.UUID, cursor: int, node_ids: Sequence[uuid.UUID]
) -> bool:
    """Whether any of `node_ids` has a journal row after `cursor`."""
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        batch = node_ids[start : start + _IN_LIST_LIMIT]
        found = session.scalar(
            select(Change.seq)
            .where(
                Change.namespace_id == namespace_id,
                Change.seq > cursor,
                Change.node_id.in_(batch),
            )
            .limit(1)
        )
        if found is not None:
            return True
    return False


def latest_cursors(session: Session, namespace_ids: Collection[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Highest cursor per namespace; namespaces without changes are left out.

    Callers pass at most WAIT_MAX_NAMESPACES ids, well under Oracle's IN-list limit.
    """
    rows = session.execute(
        select(Change.namespace_id, func.max(Change.seq))
        .where(Change.namespace_id.in_(namespace_ids))
        .group_by(Change.namespace_id)
    )
    return {namespace_id: latest for namespace_id, latest in rows}
