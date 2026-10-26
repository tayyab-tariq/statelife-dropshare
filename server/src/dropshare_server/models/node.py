import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.nodes import NodeKind


class Node(Base):
    """A file or folder (requirements §8). Trash is a state of the node, not a separate table.

    Each namespace has one root folder: the node with no parent and no name.
    """

    __tablename__ = "nodes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    namespace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("namespaces.id"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"), index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    # NFC + casefold; casefolding can lengthen a name (ß -> ss), hence the wider column.
    name_normalized: Mapped[str | None] = mapped_column(String(1000))
    kind: Mapped[NodeKind] = mapped_column(string_enum(NodeKind))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    # No foreign key: file_versions already references nodes, and a cycle would complicate
    # inserts and purges. Set together with content_hash and size_bytes.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    modified_at: Mapped[datetime] = mapped_column(UtcDateTime())
    modified_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    trashed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    trashed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # The node the user deleted; its descendants carry the same value, so restoring it (roadmap
    # F3) brings back exactly what went to the trash with it.
    trash_root_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    # sha256(namespace/parent/name_normalized) while live, NULL in the trash: "unique name per
    # folder among live items" without partial indexes (database.md §5). Set only by
    # repositories.nodes.
    live_key: Mapped[str | None] = mapped_column(String(64), unique=True)
