import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.sharing import ShareRole

# Exactly one of user_id and group_id. Spelled out: Oracle has no boolean comparison.
_ONE_GRANTEE = (
    "(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)"
)


class NamespaceMember(Base):
    """A user or group with a role on a shared folder's namespace (FR-SHARE-01)."""

    __tablename__ = "namespace_members"
    __table_args__ = (CheckConstraint(_ONE_GRANTEE, name="one_grantee"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    namespace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("namespaces.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("groups.id"), index=True)
    role: Mapped[ShareRole] = mapped_column(string_enum(ShareRole))
    # sha256(namespace/kind/grantee): one row per user or group, without a composite unique
    # index over nullable columns, which collides on Oracle (database.md §5). Set only by
    # repositories.sharing.
    grantee_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())


class FileShare(Base):
    """A user or group with a role on one file (FR-SHARE-03). Not synced: recipients see it in
    Shared with me only."""

    __tablename__ = "file_shares"
    __table_args__ = (CheckConstraint(_ONE_GRANTEE, name="one_grantee"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    group_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("groups.id"), index=True)
    role: Mapped[ShareRole] = mapped_column(string_enum(ShareRole))
    grantee_key: Mapped[str] = mapped_column(String(64), unique=True)  # sha256(node/kind/grantee)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
