import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.uploads import UploadStatus


class UploadSession(Base):
    """An in-progress resumable upload (FR-XFER-01).

    Received chunks are not tracked here: the storage backend is the record of what it holds.
    Target is either parent_id + name (new file) or node_id + base_revision (new version).
    """

    __tablename__ = "upload_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    namespace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("namespaces.id"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"))
    name: Mapped[str | None] = mapped_column(String(255))
    # The edited file; for a new file, the file created on completion.
    node_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("nodes.id"))
    base_revision: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    chunk_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[UploadStatus] = mapped_column(string_enum(UploadStatus), index=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    # The file's revision as this upload left it: a repeated `complete` answers with this, not
    # with whatever later edits made of the file (NFR-REL-04).
    result_revision: Mapped[int | None] = mapped_column(Integer)

    @property
    def chunk_count(self) -> int:
        return -(-self.size_bytes // self.chunk_size)  # ceiling; an empty file has no chunks
