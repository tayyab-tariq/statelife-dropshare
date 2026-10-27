"""What is on disk now: one snapshot of part of the sync folder (algorithm.md §1, §3)."""

import hashlib
import logging
import os
import stat
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dropshare_client.platform.base import FileIdentity
from dropshare_client.store.records import Problem
from dropshare_client.sync import paths
from dropshare_client.sync.errors import (
    FileChangingError,
    SyncRootMissingError,
    SyncStoppedError,
    UnsafePathError,
)
from dropshare_shared.schemas.nodes import NodeKind

logger = logging.getLogger(__name__)

STABLE_AFTER_NS = 2_000_000_000  # FR-SYNC-06: unchanged for 2 s before it's uploaded
_READ_SIZE = 1024 * 1024
_LINKS_NOT_SYNCED = "Shortcuts and links aren't synced. Move the real file into the folder."
_SAME_NAME = (
    "Another item in this folder has the same name apart from letter case or accents. "
    "Rename one of them to sync both."
)
_TOO_LONG = (
    "The full path is too long for Windows. Use a shorter name or move it to a folder "
    "nearer the top of your DropShare folder."
)


@dataclass(frozen=True)
class LocalItem:
    path: str  # on-disk spelling, relative to the sync root
    kind: NodeKind
    size: int
    mtime_ns: int
    sha256: str | None = None  # filled in only when the engine needs the content's identity
    file_id: str | None = None


@dataclass
class LocalScan:
    items: dict[str, LocalItem] = field(default_factory=dict)
    # Keys whose state isn't known yet: still being written, in an unreadable folder, or
    # not syncable. Nothing at or below them is uploaded or deleted this cycle.
    deferred: set[str] = field(default_factory=set)
    problems: dict[str, Problem] = field(default_factory=dict)
    still_writing: set[str] = field(default_factory=set)  # look again in a moment
    unreadable: set[str] = field(default_factory=set)  # folders we couldn't list

    def refuse(self, key: str, path: str, reason: str) -> None:
        """Leave `key` and everything below it alone, and list why."""
        self.items.pop(key, None)
        self.deferred.add(key)
        self.problems[key] = Problem(key, path, reason)


class Scanner:
    def __init__(
        self,
        root: Path,
        file_identity: FileIdentity,
        *,
        stable_after_ns: int = STABLE_AFTER_NS,
        clock_ns: Callable[[], int] = time.time_ns,
        max_path: int | None = None,
    ) -> None:
        self._root = root
        self._normal_root = Path(os.path.normpath(root))
        self._file_identity = file_identity
        self._stable_after_ns = stable_after_ns
        self._clock_ns = clock_ns
        self._max_path = max_path

    def disk_path(self, path: str) -> Path:
        """The item's absolute path. UnsafePathError if it would lead out of the sync folder:
        names come from the server and are checked, but nothing outside the root is ever
        touched even if one slipped through."""
        if not path:
            return self._root
        candidate = self._root / path
        normal = Path(os.path.normpath(candidate))
        if normal == self._normal_root or not normal.is_relative_to(self._normal_root):
            raise UnsafePathError(path, "it leads outside the sync folder")
        return candidate

    def too_long(self, path: str, *, folder: bool = False) -> bool:
        """Longer than Windows allows without long-path support (NFR-COMPAT-03, D4). A folder
        needs room for an 8.3 file name inside it, so it may be 12 characters shorter."""
        if self._max_path is None:
            return False
        limit = self._max_path - 12 if folder else self._max_path
        return len(str(self.disk_path(path))) > limit

    def require_root(self) -> None:
        if not self._root.is_dir():
            raise SyncRootMissingError(str(self._root))

    def scan(self, key: str) -> LocalScan:
        """Everything at and below `key`."""
        self.require_root()
        result = LocalScan()
        if not key:
            self._walk("", result)
            return result
        try:
            path = self.resolve(key)
        except UnsafePathError as exc:
            result.refuse(key, exc.path, exc.reason)
            return result
        if path is not None:
            try:
                info = self.disk_path(path).lstat()
            except FileNotFoundError:
                return result
            if self._visit(path, info, result):
                self._walk(path, result)
        return result

    def resolve(self, key: str) -> str | None:
        """The on-disk spelling of `key`, or None if nothing is there. Looked up segment by
        segment, so names that differ in case or Unicode form from the key still match.

        UnsafePathError if a folder on the way is a link or junction (it would lead out of
        the sync folder), or if two items on disk match one segment."""
        path = ""
        parts = key.split("/") if key else []
        for index, part in enumerate(parts):
            try:
                with os.scandir(self.disk_path(path)) as entries:
                    found = [e.name for e in entries if paths.key_of(_nfc(e.name)) == part]
            except (FileNotFoundError, NotADirectoryError):
                return None
            if not found:
                return None
            path = paths.join(path, found[0])
            if len(found) > 1:
                raise UnsafePathError(path, _SAME_NAME)
            if index < len(parts) - 1 and _is_link(self.disk_path(path)):
                raise UnsafePathError(path, _LINKS_NOT_SYNCED)
        return path

    def file_id(self, path: str) -> str | None:
        try:
            identity = self._file_identity.file_id(self.disk_path(path))
        except OSError:
            return None
        return f"{identity.device}:{identity.inode}"

    def _walk(self, folder: str, result: LocalScan) -> None:
        pending = [folder]
        while pending:
            current = pending.pop()
            try:
                with os.scandir(self.disk_path(current)) as entries:
                    listed = list(entries)
            except FileNotFoundError:
                continue  # gone since its parent was listed
            except OSError:
                logger.warning("Can't read folder %s; leaving it alone this time", current)
                key = paths.key_of(_nfc(current))
                result.deferred.add(key)
                result.unreadable.add(key)
                continue
            seen: dict[str, str] = {}
            for entry in listed:
                path = paths.join(current, entry.name)
                key = paths.key_of(_nfc(path))
                if key in seen:  # NFC and NFD, or "ß" and "ss", side by side (NTFS allows it)
                    result.refuse(key, seen[key], _SAME_NAME)
                    continue
                seen[key] = path
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue  # deleted between listing and looking: just this one item
                except OSError:
                    result.refuse(key, path, "DropShare can't read this item.")
                    continue
                if self._visit(path, info, result):
                    pending.append(path)

    def _visit(self, path: str, info: os.stat_result, result: LocalScan) -> bool:
        """Record one item; True if it is a folder to look inside."""
        name = _nfc(paths.name_of(path))
        if paths.is_ignored(name):
            return False
        key = paths.key_of(_nfc(path))
        if key in result.problems:
            return False
        if stat.S_ISLNK(info.st_mode) or _is_link(self.disk_path(path)):
            result.refuse(key, path, _LINKS_NOT_SYNCED)
            return False
        problem = paths.name_problem(name)
        if problem is not None:
            result.refuse(key, path, f"{problem} Rename it to sync it.")
            return False
        if self.too_long(path, folder=stat.S_ISDIR(info.st_mode)):
            result.refuse(key, path, _TOO_LONG)
            return False
        if stat.S_ISDIR(info.st_mode):
            result.items[key] = LocalItem(path, NodeKind.FOLDER, 0, info.st_mtime_ns)
            return True
        if stat.S_ISREG(info.st_mode):
            result.items[key] = LocalItem(path, NodeKind.FILE, info.st_size, info.st_mtime_ns)
            if abs(self._clock_ns() - info.st_mtime_ns) < self._stable_after_ns:
                result.deferred.add(key)  # still being written (FR-SYNC-06)
                result.still_writing.add(key)
        return False

    def hash(self, item: LocalItem, stop: threading.Event) -> LocalItem:
        """`item` with its SHA-256 and current size, mtime and file ID. FileChangingError if
        it changes while being read; PermissionError if another program has it locked."""
        disk = self.disk_path(item.path)
        before = disk.stat()
        digest = hashlib.sha256()
        read = 0
        with disk.open("rb") as source:
            while chunk := source.read(_READ_SIZE):
                if stop.is_set():
                    raise SyncStoppedError()
                digest.update(chunk)
                read += len(chunk)
        after = disk.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or (
            read != after.st_size
        ):
            raise FileChangingError(item.path)
        return LocalItem(
            item.path,
            NodeKind.FILE,
            after.st_size,
            after.st_mtime_ns,
            digest.hexdigest(),
            self.file_id(item.path),
        )


def _nfc(text: str) -> str:
    """macOS can hand back decomposed (NFD) names; the server and the keys use NFC."""
    return unicodedata.normalize("NFC", text)


def _is_link(path: Path) -> bool:
    """Symlinks and NTFS junctions both lead out of the sync folder."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return False
