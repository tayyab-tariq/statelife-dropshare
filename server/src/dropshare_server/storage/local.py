"""Filesystem backend for local development and NAS/SAN mounts (requirements §11).

Layout under the root: `staging/<upload id>/<index:06d>` for received chunks and
`blobs/ab/cd/<uuid>` for finished files. Both live on the same volume, so assembling a blob ends
with an atomic `os.replace` and a half-written blob is never visible. Files and the directories
holding them are fsynced, so a blob the database points to survives a power cut (NFR-REL-03).
"""

import contextlib
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

from dropshare_server.storage.base import BLOB_KEY_PATTERN, CommitResult

_CHUNK_NAME = re.compile(r"^\d{6}$")
_READ_SIZE = 1024 * 1024


class LocalFilesystemBackend:
    def __init__(self, root: Path) -> None:
        # Never create the root: a missing NAS/SAN mount must stop startup, not fill local disk.
        if not root.is_dir():
            raise NotADirectoryError(f"Storage root does not exist or is not a directory: {root}")
        self._root = root.resolve()

    def write_chunk(self, upload_id: uuid.UUID, index: int, data: bytes) -> None:
        staging = self._staging(upload_id)
        self._make_dirs(staging)
        self._write_atomically(staging / f"{index:06d}", [data])

    def chunk_indexes(self, upload_id: uuid.UUID) -> set[int]:
        staging = self._staging(upload_id)
        if not staging.is_dir():
            return set()
        return {int(path.name) for path in staging.iterdir() if _CHUNK_NAME.match(path.name)}

    def commit(self, upload_id: uuid.UUID, chunk_count: int, blob_key: str) -> CommitResult:
        staging = self._staging(upload_id)
        digest = hashlib.sha256()
        size = 0

        def pieces() -> Iterator[bytes]:
            nonlocal size
            for index in range(chunk_count):
                with (staging / f"{index:06d}").open("rb") as chunk:
                    while piece := chunk.read(_READ_SIZE):
                        digest.update(piece)
                        size += len(piece)
                        yield piece

        target = self._blob_path(blob_key)
        self._make_dirs(target.parent)
        self._write_atomically(target, pieces())
        return CommitResult(size_bytes=size, sha256=digest.hexdigest())

    def open_range(self, blob_key: str, start: int, end: int) -> Iterator[bytes]:
        # Opened now, so a missing blob fails before the response starts rather than midway.
        handle = self._blob_path(blob_key).open("rb")
        return self._read_range(handle, start, end)

    def delete_blob(self, blob_key: str) -> None:
        self._blob_path(blob_key).unlink(missing_ok=True)

    def discard_upload(self, upload_id: uuid.UUID) -> None:
        shutil.rmtree(self._staging(upload_id), ignore_errors=True)

    def staged_upload_ids(self) -> Iterator[uuid.UUID]:
        staging = self._root / "staging"
        if not staging.is_dir():
            return
        for path in staging.iterdir():
            with contextlib.suppress(ValueError):  # not one of ours: leave it alone
                yield uuid.UUID(hex=path.name)

    def health_check(self) -> None:
        with tempfile.NamedTemporaryFile(dir=self._root, prefix=".health-"):
            pass

    def _staging(self, upload_id: uuid.UUID) -> Path:
        return self._root / "staging" / upload_id.hex

    def _blob_path(self, blob_key: str) -> Path:
        if not BLOB_KEY_PATTERN.fullmatch(blob_key):
            raise ValueError("Not a blob key")
        return self._root.joinpath(*blob_key.split("/"))

    def _make_dirs(self, path: Path) -> None:
        """Create missing directories below the root, fsyncing each new one's parent."""
        missing = [
            p for p in (path, *path.parents) if p.is_relative_to(self._root) and not p.exists()
        ]
        for directory in reversed(missing):
            directory.mkdir(exist_ok=True)
            _fsync_directory(directory.parent)

    @staticmethod
    def _write_atomically(target: Path, pieces: Iterable[bytes]) -> None:
        """Write to a temp file beside the target, fsync, rename over it, fsync the directory."""
        handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".part-")
        temp = Path(temp_name)
        try:
            with os.fdopen(handle, "wb") as out:
                for piece in pieces:
                    out.write(piece)
                out.flush()
                os.fsync(out.fileno())
            temp.replace(target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        _fsync_directory(target.parent)

    @staticmethod
    def _read_range(handle: BinaryIO, start: int, end: int) -> Iterator[bytes]:
        with contextlib.closing(handle):
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                piece = handle.read(min(_READ_SIZE, remaining))
                if not piece:
                    raise OSError("Blob is shorter than its recorded size")
                remaining -= len(piece)
                yield piece


def _fsync_directory(path: Path) -> None:
    """Persist a directory's entries (a rename or a new file). POSIX only: Windows can't open a
    directory this way, and NTFS journals metadata itself."""
    if os.name != "posix":
        return
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
