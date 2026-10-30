"""Recording security-relevant events (FR-AUDIT-01/02)."""

import csv
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TextIO

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, User
from dropshare_server.repositories import audit as audit_repository
from dropshare_server.repositories import audit_retention as audit_retention_repository
from dropshare_server.services.context import CallContext
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome

logger = logging.getLogger(__name__)

# Small, flat, and never file content, passwords or tokens (FR-AUDIT-02).
type AuditDetails = dict[str, str | int | bool | None]


@dataclass(frozen=True)
class AuditTarget:
    type: str
    id: str
    name: str | None = None


def user_target(user: User) -> AuditTarget:
    return AuditTarget(type="user", id=str(user.id), name=user.username)


def _event(
    ctx: CallContext,
    action: AuditAction,
    outcome: AuditOutcome,
    target: AuditTarget | None,
    details: AuditDetails | None,
) -> AuditEvent:
    return AuditEvent(
        occurred_at=datetime.now(UTC),
        actor_type=ctx.actor.type,
        actor_user_id=ctx.actor.user_id,
        actor_label=ctx.actor.label[:255] if ctx.actor.label else None,
        device_id=ctx.actor.device_id,
        source_ip=ctx.source_ip,
        action=action,
        target_type=target.type if target else None,
        target_id=target.id if target else None,
        target_name=target.name[:255] if target and target.name else None,
        outcome=outcome,
        request_id=ctx.request_id,
        details=json.dumps(details, sort_keys=True) if details else None,
    )


def record(
    session: Session,
    ctx: CallContext,
    action: AuditAction,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
    target: AuditTarget | None = None,
    details: AuditDetails | None = None,
) -> None:
    """Record in the caller's transaction, so the event commits together with the change."""
    audit_repository.add(session, _event(ctx, action, outcome, target, details))


@dataclass(frozen=True)
class ExportFilters:
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None  # exclusive
    actor_user_id: uuid.UUID | None = None
    action: AuditAction | None = None
    outcome: AuditOutcome | None = None


EXPORT_COLUMNS = (
    "id",
    "occurred_at",
    "actor_type",
    "actor_user_id",
    "actor_label",
    "device_id",
    "source_ip",
    "action",
    "target_type",
    "target_id",
    "target_name",
    "outcome",
    "request_id",
    "details",
)


def _csv_safe(value: object) -> str:
    """Stop spreadsheet apps from running cells such as "=HYPERLINK(...)" as formulas."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


def export_csv(session: Session, ctx: CallContext, out: TextIO, filters: ExportFilters) -> int:
    """Write matching events as CSV (FR-ADMIN-01); the export itself is audited. Returns rows."""
    writer = csv.writer(out)
    writer.writerow(EXPORT_COLUMNS)
    rows = 0
    for event in audit_repository.iter_events(
        session,
        occurred_from=filters.occurred_from,
        occurred_to=filters.occurred_to,
        actor_user_id=filters.actor_user_id,
        action=filters.action,
        outcome=filters.outcome,
    ):
        writer.writerow(
            [
                _csv_safe(event.occurred_at.isoformat() if name == "occurred_at" else value)
                for name, value in ((name, getattr(event, name)) for name in EXPORT_COLUMNS)
            ]
        )
        rows += 1
    record(
        session,
        ctx,
        AuditAction.AUDIT_EXPORTED,
        details={
            "from": filters.occurred_from.isoformat() if filters.occurred_from else None,
            "to": filters.occurred_to.isoformat() if filters.occurred_to else None,
            "user_id": str(filters.actor_user_id) if filters.actor_user_id else None,
            "action": filters.action.value if filters.action else None,
            "outcome": filters.outcome.value if filters.outcome else None,
            "rows": rows,
        },
    )
    return rows


def record_now(
    session_factory: sessionmaker[Session],
    ctx: CallContext,
    action: AuditAction,
    outcome: AuditOutcome,
    target: AuditTarget | None = None,
    details: AuditDetails | None = None,
) -> None:
    """Record in a separate transaction: for failures and denials whose request rolls back."""
    audit_repository.add_in_own_transaction(
        session_factory, _event(ctx, action, outcome, target, details)
    )


_PURGE_BATCH = 500


def purge_expired(
    purge_sessions: sessionmaker[Session],
    sessions: sessionmaker[Session],
    ctx: CallContext,
    now: datetime,
    retention_days: int,
) -> int:
    """The purge-audit job (FR-AUDIT-03): delete events older than the retention period, a batch
    per transaction, through the purge account (`purge_sessions`). Idempotent.

    The purge is always logged, also when it fails partway (batches already committed stay
    deleted): `audit.purged` with what was removed and the outcome, through the app's own
    account, or through the purge account if the app's can't write."""
    before = now - timedelta(days=retention_days)
    removed = 0
    outcome = AuditOutcome.FAILURE
    try:
        while True:
            with purge_sessions.begin() as session:
                deleted = audit_retention_repository.delete_before(session, before, _PURGE_BATCH)
            removed += deleted
            if deleted < _PURGE_BATCH:
                break
        outcome = AuditOutcome.SUCCESS
    finally:
        _log_purge(sessions, purge_sessions, ctx, outcome, removed, before)
    return removed


def _log_purge(
    sessions: sessionmaker[Session],
    purge_sessions: sessionmaker[Session],
    ctx: CallContext,
    outcome: AuditOutcome,
    removed: int,
    before: datetime,
) -> None:
    details: AuditDetails = {"removed": removed, "before": before.isoformat()}
    try:
        with sessions.begin() as session:
            record(session, ctx, AuditAction.AUDIT_PURGED, outcome, details=details)
    except SQLAlchemyError:
        logger.exception("Couldn't log the audit purge with the app account; using the purge one")
        with purge_sessions.begin() as session:
            record(session, ctx, AuditAction.AUDIT_PURGED, outcome, details=details)
