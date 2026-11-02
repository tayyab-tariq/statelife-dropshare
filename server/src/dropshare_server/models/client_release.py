import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime, string_enum
from dropshare_shared.schemas.releases import ReleaseChannel, ReleasePlatform


class ClientRelease(Base):
    """One published client installer (FR-UPD-05). Never deleted: withdrawing hides it."""

    __tablename__ = "client_releases"
    __table_args__ = (
        UniqueConstraint("platform", "channel", "version"),
        Index(
            "ix_client_releases_latest",
            "platform",
            "channel",
            "version_major",
            "version_minor",
            "version_patch",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    platform: Mapped[ReleasePlatform] = mapped_column(string_enum(ReleasePlatform))
    channel: Mapped[ReleaseChannel] = mapped_column(string_enum(ReleaseChannel))
    version: Mapped[str] = mapped_column(String(16))
    # The version's parts, so "1.10.0" sorts after "1.9.0" (string order wouldn't).
    version_major: Mapped[int] = mapped_column(Integer)
    version_minor: Mapped[int] = mapped_column(Integer)
    version_patch: Mapped[int] = mapped_column(Integer)
    min_supported_version: Mapped[str] = mapped_column(String(16))
    notes: Mapped[str | None] = mapped_column(Text)  # None for none: Oracle stores '' as NULL
    blob_key: Mapped[str] = mapped_column(String(128), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime] = mapped_column(UtcDateTime())
    withdrawn_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
