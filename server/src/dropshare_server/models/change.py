import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Identity, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.nodes import NodeKind
from dropshare_shared.schemas.sync import ChangeType


class Change(Base):
    """Append-only journal of node changes per namespace (§10.1).

    The sync cursor is `seq`, numbered per namespace under a namespace row lock, so it follows
    commit order. `id` comes from an identity column, whose values can commit out of order
    between concurrent transactions; it must never be used as a cursor.

    Holds a snapshot of the node right after the change, so clients can apply changes in order
    without fetching each node.
    """

    __tablename__ = "changes"
    __table_args__ = (UniqueConstraint("namespace_id", "seq"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    namespace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("namespaces.id"))
    seq: Mapped[int] = mapped_column(BigInteger)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), index=True)
    change_type: Mapped[ChangeType] = mapped_column(string_enum(ChangeType))
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime())
    actor_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    name: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[NodeKind] = mapped_column(string_enum(NodeKind))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    modified_at: Mapped[datetime] = mapped_column(UtcDateTime())
    modified_by_user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
