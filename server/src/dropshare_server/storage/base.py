"""The only interface through which file bytes are read or written (NFR-COMPAT-05)."""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# Generated, never derived from user input (requirements §11): blobs/ab/cd/<uuid hex>.
BLOB_KEY_PATTERN = re.compile(r"^blobs/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{32}$")


def new_blob_key() -> str:
    value = uuid.uuid4().hex
    return f"blobs/{value[:2]}/{value[2:4]}/{value}"


@dataclass(frozen=True)
class CommitResult:
    """What was actually stored, measured while assembling (NFR-REL-02)."""

    size_bytes: int
    sha256: str


class StorageBackend(Protocol):
    def write_chunk(self, upload_id: uuid.UUID, index: int, data: bytes) -> None:
        """Store one chunk atomically; writing an index again replaces it."""

    def chunk_indexes(self, upload_id: uuid.UUID) -> set[int]:
        """Indexes of the chunks fully stored for this upload."""

    def commit(self, upload_id: uuid.UUID, chunk_count: int, blob_key: str) -> CommitResult:
        """Assemble chunks 0..chunk_count-1 into a new durable blob.

        The staged chunks are kept: the caller discards them once the database has recorded the
        blob, so a failure in between leaves the upload retryable.
        """

    def open_range(self, blob_key: str, start: int, end: int) -> Iterator[bytes]:
        """Stream bytes start..end (inclusive) of a blob, without loading it into memory."""

    def delete_blob(self, blob_key: str) -> None:
        """Remove a blob; a missing blob is not an error."""

    def discard_upload(self, upload_id: uuid.UUID) -> None:
        """Remove an upload's staged chunks; nothing staged is not an error."""

    def staged_upload_ids(self) -> Iterator[uuid.UUID]:
        """Uploads that have staged chunks, for the cleanup job."""

    def blobs(self) -> Iterator[tuple[str, datetime]]:
        """Every blob key with when it was written (UTC), for the gc-blobs job."""

    def health_check(self) -> None:
        """Raise OSError if the storage can't currently be written."""
