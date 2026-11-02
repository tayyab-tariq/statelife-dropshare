"""File content ready to stream, shared by file downloads and client installers (FR-XFER-02)."""

from collections.abc import Iterator
from dataclasses import dataclass

from dropshare_server.core.errors import AppError
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.error_details import RangeNotSatisfiableDetails


@dataclass(frozen=True)
class Content:
    """A file's bytes start..end (inclusive), ready to stream."""

    name: str
    size_bytes: int
    sha256: str
    start: int
    end: int
    partial: bool
    chunks: Iterator[bytes]


def resolve_range(byte_range: tuple[int | None, int | None], size: int) -> tuple[int, int]:
    """`byte_range` is a parsed `Range: bytes=first-last` (first None = the last `last` bytes);
    returns the inclusive start and end, or raises 416 RANGE_NOT_SATISFIABLE."""
    first, last = byte_range
    if first is None:  # suffix: the last `last` bytes
        if not last or size == 0:
            raise _unsatisfiable(size)
        return max(0, size - last), size - 1
    if first >= size:
        raise _unsatisfiable(size)
    return first, size - 1 if last is None else min(last, size - 1)


def _unsatisfiable(size: int) -> AppError:
    return AppError(
        416,
        ErrorCode.RANGE_NOT_SATISFIABLE,
        "The requested range is outside the file.",
        RangeNotSatisfiableDetails(size_bytes=size).model_dump(mode="json"),
        headers={"Content-Range": f"bytes */{size}"},
    )
