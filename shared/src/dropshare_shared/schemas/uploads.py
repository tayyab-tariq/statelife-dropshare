"""Resumable chunked uploads (FR-XFER-01, references/storage.md)."""

import re
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dropshare_shared.schemas.nodes import NAME_INPUT_MAX_LENGTH

# Request header carrying the hex SHA-256 of a chunk's bytes.
CHUNK_SHA256_HEADER = "X-Chunk-SHA256"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class UploadStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"  # set by the expire-uploads job (NFR-REL-05)


class UploadCreateIn(BaseModel):
    """A new file (`parent_id` + `name`) or a new version (`node_id` + `base_revision`)."""

    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID | None = None
    name: str | None = Field(default=None, min_length=1, max_length=NAME_INPUT_MAX_LENGTH)
    node_id: uuid.UUID | None = None
    base_revision: int | None = Field(default=None, ge=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=SHA256_PATTERN.pattern, description="Lower-case hex SHA-256")

    @model_validator(mode="after")
    def _one_target(self) -> Self:
        new_file = (self.parent_id, self.name)
        new_version = (self.node_id, self.base_revision)
        if not any(
            all(value is not None for value in chosen) and all(value is None for value in other)
            for chosen, other in ((new_file, new_version), (new_version, new_file))
        ):
            raise ValueError("Send either parent_id and name, or node_id and base_revision")
        return self


class UploadOut(BaseModel):
    id: uuid.UUID
    status: Annotated[UploadStatus | str, Field(union_mode="left_to_right")]
    size_bytes: int
    chunk_size: int
    chunk_count: int
    received_chunks: list[int]  # indexes the server already has; send only the others
    expires_at: datetime
    node_id: uuid.UUID | None  # the edited file, or the created file once completed
