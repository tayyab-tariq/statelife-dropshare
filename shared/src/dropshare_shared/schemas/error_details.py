"""Typed `error.details` for errors the client must act on, not just display."""

import uuid
from datetime import datetime

from pydantic import BaseModel

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


class RangeNotSatisfiableDetails(BaseModel):
    """416 RANGE_NOT_SATISFIABLE: the file is this long (FR-XFER-02). Also in Content-Range."""

    size_bytes: int
