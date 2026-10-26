from datetime import datetime

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from dropshare_server.models.base import Base
from dropshare_server.models.types import UtcDateTime


class LoginThrottle(Base):
    """Failed sign-in attempts for logins that match no account.

    Unknown logins are counted and locked exactly like real accounts (users.failed_login_count
    / locked_until), so a 423 never reveals whether an account exists (FR-AUTH-01, FR-AUTH-06).
    Keyed by a SHA-256 of the normalized login: what people type (sometimes a password) is never
    stored. Stale rows are removed by a cleanup job (roadmap F3).
    """

    __tablename__ = "login_throttles"

    login_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failed_login_count: Mapped[int] = mapped_column(Integer)
    locked_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), index=True)
