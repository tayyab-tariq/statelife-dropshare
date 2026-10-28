"""User accounts (requirements §3, §8)."""

import re
import uuid
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX_LENGTH = 320
DISPLAY_NAME_MAX_LENGTH = 255


class UserRole(StrEnum):
    USER = "user"
    ADMIN = "admin"


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class UserOut(BaseModel):
    id: uuid.UUID
    username: str
    email: str
    display_name: str
    # Tolerant: a role added by a newer server must not stop installed clients parsing /me.
    role: Annotated[UserRole | str, Field(union_mode="left_to_right")]
    quota_bytes: int
    # Includes trashed items (FR-TRASH-01). Defaults to 0 for servers from before it was sent;
    # the server's own quota check at upload start is what's enforced.
    used_bytes: int = 0
