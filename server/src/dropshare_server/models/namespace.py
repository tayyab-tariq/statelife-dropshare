import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.nodes import NamespaceKind


class Namespace(Base):
    """A sync root with its own change history (requirements §8)."""

    __tablename__ = "namespaces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    kind: Mapped[NamespaceKind] = mapped_column(string_enum(NamespaceKind))
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    # The last change cursor handed out. Incremented with this row locked, so the cursors of one
    # namespace commit in order and a client never skips a change (services/changes.record).
    last_change_seq: Mapped[int] = mapped_column(BigInteger, default=0)
