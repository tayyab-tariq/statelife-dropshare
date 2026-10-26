"""Audit events: insert and read only (FR-AUDIT-03). There is deliberately no update or delete."""

import logging
import uuid
from collections.abc import Iterator
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome

logger = logging.getLogger(__name__)

_EXPORT_BATCH = 500


def add(session: Session, event: AuditEvent) -> None:
    session.add(event)


def add_in_own_transaction(session_factory: sessionmaker[Session], event: AuditEvent) -> None:
    """Commit an event independently of the caller's transaction, which is about to roll back.

    Used for failures and denials. If the database is unavailable the event is logged instead,
    so the original error still reaches the client.
    """
    try:
        with session_factory.begin() as session:
            session.add(event)
    except SQLAlchemyError:
        logger.exception("Couldn't write audit event %s (%s)", event.action, event.outcome)


def iter_events(
    session: Session,
    *,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    actor_user_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    outcome: AuditOutcome | None = None,
) -> Iterator[AuditEvent]:
    """Matching events in id order, streamed in batches so large exports stay out of memory."""
    query = select(AuditEvent).order_by(AuditEvent.id).execution_options(yield_per=_EXPORT_BATCH)
    if occurred_from is not None:
        query = query.where(AuditEvent.occurred_at >= occurred_from)
    if occurred_to is not None:
        query = query.where(AuditEvent.occurred_at < occurred_to)
    if actor_user_id is not None:
        query = query.where(AuditEvent.actor_user_id == actor_user_id)
    if action is not None:
        query = query.where(AuditEvent.action == action)
    if outcome is not None:
        query = query.where(AuditEvent.outcome == outcome)
    yield from session.scalars(query)
