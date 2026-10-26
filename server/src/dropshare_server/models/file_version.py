import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime


class FileVersion(Base):
    """One stored version of a file's content; blobs are never overwritten (G6)."""

    __tablename__ = "file_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    node_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("nodes.id"), index=True)
    blob_key: Mapped[str] = mapped_column(String(128), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
