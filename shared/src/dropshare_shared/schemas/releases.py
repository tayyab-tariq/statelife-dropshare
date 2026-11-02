"""Client releases and updates (FR-UPD-03 … FR-UPD-05, OQ-49).

A release is one signed installer for one platform on one channel. Each user is on a channel
(`UserOut.update_channel`); the server picks the release for that channel, so the client only
says which platform it runs on.
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dropshare_shared.version import parse_version

RELEASE_NOTES_MAX_LENGTH = 10_000


class ReleaseChannel(StrEnum):
    STABLE = "stable"
    BETA = "beta"


# Tolerant: a channel added by a newer server must not stop installed clients parsing it.
type TolerantReleaseChannel = Annotated[ReleaseChannel | str, Field(union_mode="left_to_right")]


class ReleasePlatform(StrEnum):
    WINDOWS_X64 = "windows-x64"


class LatestReleaseQuery(BaseModel):
    """`GET /client/releases/latest?platform=`: 404 RELEASE_NOT_FOUND when none is published."""

    model_config = ConfigDict(extra="forbid")

    platform: ReleasePlatform


class ReleaseOut(BaseModel):
    id: uuid.UUID
    version: str
    channel: TolerantReleaseChannel
    min_supported_version: str
    notes: str
    size_bytes: int
    sha256: str  # hex, lower case: checked before installing, as is the signature (FR-UPD-03)
    published_at: datetime

    @field_validator("version", "min_supported_version")
    @classmethod
    def _is_version(cls, value: str) -> str:
        parse_version(value)
        return value
