"""Typed `error.details` for errors the client must act on, not just display."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

from dropshare_shared.schemas.nodes import TolerantNodeKind


class AccountLockedDetails(BaseModel):
    """423 ACCOUNT_LOCKED (FR-AUTH-06). Also sent as the Retry-After header."""

    locked_until: datetime
    retry_after_seconds: int


class UpgradeRequiredDetails(BaseModel):
    """426 UPGRADE_REQUIRED (FR-UPD-04)."""

    min_version: str


class NameConflictDetails(BaseModel):
    """409 NAME_CONFLICT: the live item that already has the name, so a retried create can
    recognise its own earlier success (NFR-REL-04)."""

    node_id: uuid.UUID
    kind: TolerantNodeKind


class RevisionConflictDetails(BaseModel):
    """409 REVISION_CONFLICT. `current_revision` is None when the item is gone (trashed)."""

    current_revision: int | None


class UploadIncompleteDetails(BaseModel):
    """409 UPLOAD_INCOMPLETE: send these chunks, then complete again (FR-XFER-01)."""

    missing_chunks: list[int]  # at most the first 1000


class FileTooLargeDetails(BaseModel):
    """413 FILE_TOO_LARGE (NFR-SCALE-01)."""

    max_file_size_bytes: int


class FileTypeProblem(StrEnum):
    BANNED_EXTENSION = "banned_extension"  # the file's own last extension is banned
    EXECUTABLE_CONTENT = "executable_content"  # a program (PE, ELF, Mach-O), whatever its name
    CONTAINS_BANNED_FILE = "contains_banned_file"  # a folder holding banned files (OQ-57)


# A newer server may add a reason; clients show the message rather than failing.
type TolerantFileTypeProblem = Annotated[FileTypeProblem | str, Field(union_mode="left_to_right")]


class FileTypeNotAllowedDetails(BaseModel):
    """422 FILE_TYPE_NOT_ALLOWED (FR-SAFE-02). `extension` is the banned one (None for
    executable content); `file_names` are up to 10 banned files inside a folder."""

    reason: TolerantFileTypeProblem
    extension: str | None = None
    file_names: list[str] = []


class MalwareDetectedDetails(BaseModel):
    """422 MALWARE_DETECTED (FR-SAFE-01): the scanner's name for what it found."""

    detection: str


class RangeNotSatisfiableDetails(BaseModel):
    """416 RANGE_NOT_SATISFIABLE: the file is this long (FR-XFER-02). Also in Content-Range."""

    size_bytes: int
