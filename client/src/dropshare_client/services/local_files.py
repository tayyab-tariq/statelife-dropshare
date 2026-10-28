"""The file browser's actions on the local sync folder (FR-FILE-03/04/05, OQ-23): the sync
engine sends them to the server, so there is one write path. Blocking: call from a worker.

Every path is relative to the sync folder and checked to stay inside it, by its resolved
location and with no link or junction anywhere along it (FR-SYNC-07); DropShare's own
`.dropshare-tmp` is off limits. Nothing is ever overwritten (rename_no_replace, exclusive
create), names follow the server's rules (NFR-COMPAT-02) and are unique per folder ignoring
case, and deleting goes to the Recycle Bin (FR-SYNC-09). Errors are FileActionError; their
words are in controllers/messages.py.
"""

import contextlib
import logging
import os
import shutil
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import RecycleBin
from dropshare_client.services.errors import FileActionError, FileProblem
from dropshare_client.services.sync_folder import TEMP_DIR, is_link
from dropshare_shared.naming import InvalidNameError, normalize_name, validate_name

logger = logging.getLogger(__name__)

_READ_SIZE = 1024 * 1024


class LocalFiles:
    def __init__(self, root: Path, recycle_bin: RecycleBin, reveal: Callable[[Path], None]) -> None:
        self._root = root
        self._recycle_bin = recycle_bin
        self._reveal = reveal

    @property
    def root(self) -> Path:
        return self._root

    def new_folder(self, parent: str, name: str) -> str:
        """Create `name` in `parent` ("" = the top of the sync folder); returns its path."""
        nfc = _valid(name)
        folder = self._root if not parent else self._existing(parent)
        self._check_free(folder, nfc)
        path = _join(parent, nfc)
        with _as_file_error():
            folder.joinpath(nfc).mkdir()
        return path

    def rename(self, path: str, name: str) -> str:
        """Give `path` a new name in the same folder (case-only changes included)."""
        nfc = _valid(name)
        source = self._existing(path)
        parent = path.rpartition("/")[0]
        if nfc != source.name:
            self._check_free(source.parent, nfc, except_for=source.name)
            with _as_file_error():
                rename_no_replace(source, source.parent / nfc)
        return _join(parent, nfc)

    def move(self, paths: Iterable[str], folder: str) -> list[str]:
        """Move items into `folder`, keeping their names; returns their new paths."""
        target = self._root if not folder else self._existing(folder)
        moved: list[str] = []
        for path in paths:
            source = self._existing(path)
            if _key(folder) == _key(path) or _key(folder).startswith(f"{_key(path)}/"):
                raise FileActionError(FileProblem.INTO_ITSELF, source.name)
            if source.parent == target:
                continue
            self._check_free(target, source.name)
            with _as_file_error():
                rename_no_replace(source, target / source.name)
            moved.append(_join(folder, source.name))
        return moved

    def delete(self, paths: Iterable[str]) -> None:
        """To the Recycle Bin; the sync engine then sends it to the server trash."""
        for path in paths:
            item = self._existing(path)
            with _as_file_error():
                self._recycle_bin.move_to_recycle_bin(item)

    def save_copy(self, path: str, destination: Path) -> Path:
        """Copy a synced file or folder to a place outside DropShare (FR-FILE-03). Never into
        itself or the sync folder, never over anything; a partial copy is removed."""
        source = self._existing(path)
        chosen = destination.resolve()
        if chosen == source.resolve() or chosen.is_relative_to(source.resolve()):
            raise FileActionError(FileProblem.INTO_ITSELF, source.name)
        if chosen.is_relative_to(self._root.resolve()):
            raise FileActionError(FileProblem.OUTSIDE)
        target = destination / source.name
        if os.path.lexists(target):
            raise FileActionError(FileProblem.COPY_EXISTS, source.name)
        made: list[Path] = []
        try:
            with _as_file_error():
                _copy_tree(source, target, made)
        except BaseException:
            for item in reversed(made):  # only what this copy made, newest first
                with contextlib.suppress(OSError):
                    item.rmdir() if item.is_dir() else item.unlink()
            raise
        return target

    def show(self, path: str) -> None:
        """Open Explorer at the item, selected (FR-FILE-05)."""
        self._reveal(self._existing(path))

    def relative(self, folder: Path) -> str:
        """`folder` (chosen on disk) as a path in the sync folder; for "Move to…"."""
        root = self._root.resolve()
        if _has_link(folder, self._root.parent):
            raise FileActionError(FileProblem.OUTSIDE)
        resolved = folder.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            raise FileActionError(FileProblem.OUTSIDE)
        relative = "" if resolved == root else resolved.relative_to(root).as_posix()
        if relative == TEMP_DIR or relative.startswith(f"{TEMP_DIR}/"):
            raise FileActionError(FileProblem.OUTSIDE)
        return relative

    def disk(self, path: str) -> Path:
        """The absolute path of an item in the sync folder (not the folder itself, not
        `.dropshare-tmp`), reached without going through a link."""
        parts = path.split("/")
        if not path or any(p in ("", ".", "..") for p in parts) or parts[0] == TEMP_DIR:
            raise FileActionError(FileProblem.OUTSIDE)
        candidate = self._root.joinpath(*parts)
        normal = Path(os.path.normpath(candidate))
        if not normal.is_relative_to(Path(os.path.normpath(self._root))):
            raise FileActionError(FileProblem.OUTSIDE)
        if _has_link(candidate.parent, self._root):
            raise FileActionError(FileProblem.OUTSIDE)
        return candidate

    def _existing(self, path: str) -> Path:
        disk = self.disk(path)
        if not os.path.lexists(disk) or is_link(disk):
            raise FileActionError(FileProblem.GONE)
        return disk

    @staticmethod
    def _check_free(folder: Path, name: str, except_for: str | None = None) -> None:
        wanted = normalize_name(name)
        skip = normalize_name(except_for) if except_for is not None else None
        try:
            names = [entry.name for entry in folder.iterdir()]
        except OSError as exc:
            logger.info("Couldn't list a folder: %s", type(exc).__name__)
            raise FileActionError(FileProblem.GONE) from None
        for existing in names:
            folded = normalize_name(existing)
            if folded == wanted and folded != skip:
                raise FileActionError(FileProblem.NAME_TAKEN, name)


def _has_link(path: Path, stop_at: Path) -> bool:
    """Whether `path` or any folder above it, up to `stop_at`, is a link or junction."""
    current = path
    while current != stop_at and current != current.parent:
        if is_link(current):
            return True
        current = current.parent
    return False


def _copy_tree(source: Path, target: Path, made: list[Path]) -> None:
    """Exclusive creates throughout: nothing at the destination is ever overwritten, even
    if it appears during the copy. Links inside are left out (FR-SYNC-07)."""
    if source.is_dir():
        target.mkdir()
        made.append(target)
        for child in sorted(source.iterdir()):
            if not is_link(child):
                _copy_tree(child, target / child.name, made)
        return
    with source.open("rb") as reading, target.open("xb") as writing:
        made.append(target)
        shutil.copyfileobj(reading, writing, _READ_SIZE)
    shutil.copystat(source, target)


@contextlib.contextmanager
def _as_file_error() -> Iterator[None]:
    """A file-system failure as a FileProblem; only its type goes to the log."""
    try:
        yield
    except OSError as exc:
        logger.warning("A file action failed: %s", type(exc).__name__)
        if isinstance(exc, FileExistsError):
            raise FileActionError(FileProblem.NAME_TAKEN) from None
        if isinstance(exc, FileNotFoundError):
            raise FileActionError(FileProblem.GONE) from None
        if isinstance(exc, PermissionError):
            raise FileActionError(FileProblem.IN_USE) from None
        raise FileActionError(FileProblem.FAILED) from None


def _valid(name: str) -> str:
    try:
        return validate_name(name.strip())
    except InvalidNameError as exc:
        raise FileActionError(FileProblem.BAD_NAME, name, str(exc)) from None


def _key(path: str) -> str:
    """Compared as NTFS does: per name, ignoring case and Unicode form."""
    return "/".join(normalize_name(part) for part in path.split("/"))


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name
