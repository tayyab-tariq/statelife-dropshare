"""User accounts (requirements §3, §8)."""

import re
import uuid
from enum import StrEnum

from pydantic import BaseModel

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
    role: UserRole
    quota_bytes: int
