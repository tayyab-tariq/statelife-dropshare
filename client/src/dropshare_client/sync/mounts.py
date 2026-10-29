"""Letting a shared folder go when the user left it or lost access (FR-SHARE-02/04, OQ-33).

Nothing in it is lost: files changed or added here and never uploaded move to
"<folder> (access removed)" next to it, which then syncs to the user's own DropShare; the rest,
already on the server for the people who still have the folder, goes to the Recycle Bin. Each
step can be repeated after a crash: files already moved aren't found again, and the folder goes
to the Recycle Bin last.
"""

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import FileAttributes, RecycleBin
from dropshare_client.store.index import Index
from dropshare_client.store.records import Entry
from dropshare_client.sync import paths
from dropshare_client.sync.errors import FileChangingError
from dropshare_client.sync.executor import ActivityLog
from dropshare_client.sync.scanner import LocalItem, Scanner
from dropshare_shared.schemas.nodes import NodeKind

logger = logging.getLogger(__name__)

KEPT_SUFFIX = " (access removed)"
_FREE_NAME_TRIES = 100


def _changed(scanner: Scanner, item: LocalItem, entry: Entry | None, stop: threading.Event) -> bool:
    """Whether the file holds something the server doesn't have. When in doubt, it does."""
    if entry is None or entry.kind != NodeKind.FILE:
        return True
    if (item.size, item.mtime_ns) == (entry.size, entry.mtime_ns):
        return False
    try:
        return scanner.hash(item, stop).sha256 != entry.sha256
    except (FileChangingError, OSError):
        return True


class ReleaseIncompleteError(Exception):
    """Something in the folder couldn't be moved yet (open in another program): retry."""


class MountRelease:
    def __init__(
        self,
        scanner: Scanner,
        index: Index,
        recycle_bin: RecycleBin,
        attributes: FileAttributes,
        log: ActivityLog,
        stop: threading.Event,
    ) -> None:
        self._attributes = attributes
        self._scanner = scanner
        self._index = index
        self._recycle_bin = recycle_bin
        self._log = log
        self._stop = stop

    def release(
        self, key: str, kept: str | None = None, remember: Callable[[str], None] = lambda _: None
    ) -> str | None:
        """Let the folder at `key` go. Returns the key of the folder the unsynced files were
        kept in, which the next cycle uploads, or None if there were none.

        `kept` is the folder a previous, interrupted attempt chose; a new choice is passed to
        `remember` before anything moves, so a retry uses the same folder. Raises
        FileChangingError while something in it is still being written, and
        ReleaseIncompleteError while something can't be moved: try again later. Nothing is
        forgotten until the folder is in the Recycle Bin."""
        path = self._scanner.resolve(key)
        if path is None:  # already gone here (the user deleted it, which is how they left)
            self._index.remove_entries(key, subtree=True)
            return None if kept is None else paths.key_of(kept)
        scan = self._scanner.scan(key)
        if scan.still_writing or scan.unreadable:
            # A file still being written, or a folder we can't list yet: look again shortly
            # rather than decide what to keep from half a picture.
            raise FileChangingError(path)
        base = self._index.entries_under(key)
        unsynced = [
            item.path
            for item_key, item in sorted(scan.items.items())
            if item.kind == NodeKind.FILE
            and (
                item_key in scan.deferred
                or _changed(self._scanner, item, base.get(item_key), self._stop)
            )
        ]
        # Items the scanner refused (a bad name, a link, too long a path) were never on the
        # server either: they are the user's too.
        unsynced.extend(problem.path for problem in scan.problems.values())
        if unsynced:
            if kept is None:
                kept = self._free_sibling(path)
                remember(kept)
            self._keep(path, sorted(unsynced), kept)
        self._recycle(path)
        self._index.remove_entries(key, subtree=True)
        detail = f"Unsynced files kept in {paths.name_of(kept)}" if kept else ""
        self._log.activity("shared_folder_removed", path, detail)
        return None if kept is None else paths.key_of(kept)

    def _keep(self, path: str, items: list[str], kept: str) -> None:
        """Move `items` (below `path`) into `kept`, keeping their place below it."""
        kept_disk = self._scanner.disk_path(kept)
        kept_disk.mkdir(exist_ok=True)
        for item in items:
            target = kept_disk / item[len(path) + 1 :]
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                rename_no_replace(self._scanner.disk_path(item), target)
            except FileNotFoundError:
                continue  # moved or deleted meanwhile
            except OSError as exc:  # open in another program, most likely
                raise ReleaseIncompleteError(item) from exc
            if target.is_file() and not target.is_symlink():  # theirs now: editable
                self._attributes.set_read_only(target, False)

    def _free_sibling(self, path: str) -> str:
        parent, name = paths.parent_of(path), paths.name_of(path)
        for attempt in range(_FREE_NAME_TRIES):
            candidate = f"{name}{KEPT_SUFFIX}" + (f" ({attempt})" if attempt else "")
            joined = paths.join(parent, candidate)
            if not self._scanner.disk_path(joined).exists():
                return joined
        raise FileExistsError(f"No free name next to {path}")

    def _recycle(self, path: str) -> None:
        disk: Path = self._scanner.disk_path(path)
        try:
            self._recycle_bin.move_to_recycle_bin(disk)
        except OSError as exc:
            raise ReleaseIncompleteError(path) from exc
