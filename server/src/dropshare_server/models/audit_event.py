import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome


class AuditEvent(Base):
    """Append-only audit log (FR-AUDIT-02/03). Application code only inserts rows."""

    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    actor_type: Mapped[AuditActorType] = mapped_column(string_enum(AuditActorType))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    # OS user for CLI actions; the attempted login name for anonymous sign-in failures.
    actor_label: Mapped[str | None] = mapped_column(String(255))
    device_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    source_ip: Mapped[str | None] = mapped_column(String(45))
    action: Mapped[AuditAction] = mapped_column(string_enum(AuditAction), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    target_name: Mapped[str | None] = mapped_column(String(255))
    outcome: Mapped[AuditOutcome] = mapped_column(string_enum(AuditOutcome))
    request_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[str | None] = mapped_column(Text)  # small JSON object; never queried
