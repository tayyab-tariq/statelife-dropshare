"""The one place audit rows are deleted: purging events past retention (FR-AUDIT-03).

Only the purge-audit job uses this, through its own database account; the app's account may
only insert and read audit rows (database.md).
"""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from dropshare_server.models import AuditEvent


def delete_before(session: Session, before: datetime, limit: int) -> int:
    """Delete up to `limit` events older than `before`, oldest first; returns how many."""
    ids = list(
        session.scalars(
            select(AuditEvent.id)
            .where(AuditEvent.occurred_at < before)
            .order_by(AuditEvent.id)
            .limit(limit)
        )
    )
    if ids:
        session.execute(
            delete(AuditEvent)
            .where(AuditEvent.id.in_(ids))
            .execution_options(synchronize_session=False)
        )
    return len(ids)
