"""User administration from the app (FR-ADMIN-02/06/07, requirements §9 Admin).

Admins only. The server checks the caller's role on every request; the client merely hides the
Administration section from everyone else.
"""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dropshare_shared.schemas.users import (
    DISPLAY_NAME_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    UserOut,
    UserRole,
    UserStatus,
)

# Far above any real quota, and within a 64-bit integer column on every database.
QUOTA_MAX_BYTES = 2**60

type TolerantUserStatus = Annotated[UserStatus | str, Field(union_mode="left_to_right")]


class AdminUserOut(UserOut):
    """A user as the Administration section lists them: everything in `UserOut` plus status."""

    status: TolerantUserStatus


class AdminUserCreateIn(BaseModel):
    """`POST /admin/users`. The server applies the same rules as the CLI (FR-ADMIN-01); these
    bounds only stop oversized input early."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=1, max_length=EMAIL_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=DISPLAY_NAME_MAX_LENGTH)
    role: UserRole
    quota_bytes: int = Field(ge=0, le=QUOTA_MAX_BYTES)


class AdminUserUpdateIn(BaseModel):
    """`PATCH /admin/users/{id}`: any of status, role and quota; at least one."""

    model_config = ConfigDict(extra="forbid")

    status: UserStatus | None = None
    role: UserRole | None = None
    quota_bytes: int | None = Field(default=None, ge=0, le=QUOTA_MAX_BYTES)

    @model_validator(mode="after")
    def _something_to_change(self) -> Self:
        if self.status is None and self.role is None and self.quota_bytes is None:
            raise ValueError("Give a status, a role or a quota to change")
        return self


class EmailOutcome(StrEnum):
    """Whether the temporary password reached the user's mailbox (FR-ADMIN-07)."""

    SENT = "sent"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"


type TolerantEmailOutcome = Annotated[EmailOutcome | str, Field(union_mode="left_to_right")]


class TemporaryPasswordOut(BaseModel):
    """Answer to creating a user or resetting a password. The temporary password is in this
    response only: it can't be fetched again (FR-ADMIN-02)."""

    user: AdminUserOut
    temporary_password: str
    email: TolerantEmailOutcome
