import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import Flag, UtcDateTime, string_enum
from dropshare_shared.schemas.users import UserRole, UserStatus


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64))
    # NFC + casefold, computed in Python: Oracle compares case-sensitively (database.md §4).
    username_normalized: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[str] = mapped_column(String(320))
    email_normalized: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(string_enum(UserRole))
    status: Mapped[UserStatus] = mapped_column(string_enum(UserStatus))
    quota_bytes: Mapped[int] = mapped_column(BigInteger)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    must_change_password: Mapped[bool] = mapped_column(Flag())
    # Bumped on password change/reset and disable; access tokens carry it, so old ones stop working.
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime())
