"""Upload from the app (FR-FILE-02, OQ-23): copy the chosen files and folders into a folder of
the sync folder; the sync engine uploads them. Blocking: run on a worker.

Safety rules:
- Nothing is copied unless every name can be used on the server and there is room for all
  of it, on disk and in the user's quota (planning checks the whole tree first).
- A file is copied to a temp file in `.dropshare-tmp` (never synced) and moved into place
  only when complete, so the engine never uploads half a file.
- Replace (OQ-26: a folder is merged) keeps the original as a hard link aside and swaps the
  new version in with one atomic replace: the path is never empty, so the engine sees a new
  version of the same file, never a delete.
- Cancel or a failure takes back exactly what this upload did (OQ-28): a placed file is
  removed only if it is still the very copy placed (another device's file or the user's
  edit stays), and each replaced original goes back. Nothing is ever deleted on a guess:
  what can't be put back is kept and reported. On success the replaced originals go to the
  Recycle Bin under their own names.
- Links (symlinks and NTFS junctions) are never followed (FR-SYNC-07).
"""

import logging
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import RecycleBin
from dropshare_client.services.errors import (
    FileActionError,
    FileProblem,
    TransferCancelledError,
)
from dropshare_client.services.sync_folder import TEMP_DIR, is_link
from dropshare_shared.naming import InvalidNameError, normalize_name, validate_name

logger = logging.getLogger(__name__)

_READ_SIZE = 1024 * 1024
_IMPORT_DIR = "import"
_MAX_NUMBER = 10_000

# Bytes copied so far, of the plan's total; called on the copying thread.
type Progress = Callable[[int], None]


class Resolution(StrEnum):
    KEEP_BOTH = "keep_both"  # default: the copy gets " (1)", " (2)" …
    REPLACE = "replace"  # files replaced; a folder merged into the one there (OQ-26)
    SKIP = "skip"


@dataclass(frozen=True)
class Source:
    path: Path  # what the user chose, outside or inside the sync folder
    name: str  # NFC, valid on the server
    is_folder: bool
    size: int  # bytes of everything in it


@dataclass(frozen=True)
class ImportPlan:
    target: str  # the folder in the sync folder, relative ("" = top)
    sources: list[Source]
    total: int
    clashes: list[str]  # names of sources whose name is taken in the target folder


@dataclass(frozen=True)
class ImportResult:
    changed: list[str]  # top-level paths created or changed in the sync folder
    kept: list[Path]  # replaced originals the Recycle Bin refused; still in .dropshare-tmp


class NotEnoughSpaceError(Exception):
    """Nothing was copied: `needed` bytes don't fit in the `available` ones, on this
    computer's disk or (`in_dropshare`) in the user's quota."""

    def __init__(self, needed: int, available: int, *, in_dropshare: bool) -> None:
        super().__init__(f"needs {needed} bytes, {available} available")
        self.needed, self.available, self.in_dropshare = needed, max(available, 0), in_dropshare


class ImportTakenBackError(Exception):
    """The upload stopped (cancelled, or it failed: see __cause__) and was taken back.
    `kept` lists what couldn't be removed or put back exactly, and so was left in place."""

    def __init__(self, *, cancelled: bool, kept: list[Path]) -> None:
        super().__init__("cancelled" if cancelled else "failed")
        self.cancelled, self.kept = cancelled, kept


type _Identity = tuple[int, int, int, int]  # device, inode, size, mtime_ns


@dataclass
class _Undo:
    """What this import did, to take it back exactly (OQ-28)."""

    folders: list[Path] = field(default_factory=list)  # made by this import, in order
    files: list[tuple[Path, _Identity]] = field(default_factory=list)  # placed, as placed
    replaced: list[tuple[Path, Path]] = field(default_factory=list)  # (place, original aside)


class Importer:
    def __init__(self, root: Path, recycle_bin: RecycleBin) -> None:
        self._root = root
        self._recycle_bin = recycle_bin

    def plan(
        self, chosen: Iterable[Path], target: str, stop: threading.Event | None = None
    ) -> ImportPlan:
        """Names, sizes and clashes, for the whole tree: a name inside a chosen folder that
        the server would refuse stops the upload here, before anything is copied."""
        stop = stop or threading.Event()
        folder = self._folder(target)
        sources = [self._source(path, folder, stop) for path in chosen]
        seen: set[str] = set()
        for source in sources:
            key = normalize_name(source.name)
            if key in seen:
                raise FileActionError(FileProblem.DUPLICATE_SOURCE, source.name)
            seen.add(key)
        taken = _names_in(folder)
        clashes = [s.name for s in sources if normalize_name(s.name) in taken]
        return ImportPlan(target, sources, sum(s.size for s in sources), clashes)

    def check_space(self, plan: ImportPlan, quota_left: int | None) -> None:
        """NotEnoughSpaceError unless the copy fits on this disk and in DropShare. Skipped and
        replaced items still count: the check errs on the side of copying nothing."""
        free = shutil.disk_usage(self._root).free
        if plan.total > free:
            raise NotEnoughSpaceError(plan.total, free, in_dropshare=False)
        if quota_left is not None and plan.total > quota_left:
            raise NotEnoughSpaceError(plan.total, quota_left, in_dropshare=True)

    def run(
        self,
        plan: ImportPlan,
        resolutions: dict[str, Resolution],
        stop: threading.Event,
        progress: Progress,
    ) -> ImportResult:
        """Copy everything. On cancel or any failure, everything is taken back first, then
        ImportTakenBackError is raised (from the cause)."""
        folder = self._folder(plan.target)
        undo = _Undo()
        aside = self._temp() / f"{_IMPORT_DIR}-{uuid.uuid4().hex}"
        done = [0]

        def count(size: int) -> None:
            done[0] += size
            progress(done[0])

        changed: list[str] = []
        try:
            aside.mkdir(parents=True)
            for source in plan.sources:
                choice = resolutions.get(source.name, Resolution.KEEP_BOTH)
                if source.name not in plan.clashes:
                    choice = Resolution.KEEP_BOTH  # nothing there: just copied
                if choice is Resolution.SKIP:
                    count(source.size)
                    continue
                if choice is Resolution.KEEP_BOTH:
                    name = _free_name(folder, source.name, folder_item=source.is_folder)
                else:
                    name = _existing_name(folder, source.name) or source.name
                self._copy(source.path, folder / name, aside, undo, stop, count)
                changed.append(_join(plan.target, name))
        except BaseException as exc:
            kept = self._take_back(undo, aside)
            raise ImportTakenBackError(
                cancelled=isinstance(exc, TransferCancelledError), kept=kept
            ) from exc
        return ImportResult(changed, self._finish(undo, aside))

    # --- copying -------------------------------------------------------------------------

    def _copy(
        self,
        source: Path,
        target: Path,
        aside: Path,
        undo: _Undo,
        stop: threading.Event,
        count: Callable[[int], None],
    ) -> None:
        if source.is_dir():
            if is_link(target):
                raise FileActionError(FileProblem.NOT_A_FOLDER, target.name)
            if target.is_dir():  # merged (OQ-26)
                pass
            elif os.path.lexists(target):
                raise FileActionError(FileProblem.NOT_A_FOLDER, target.name)
            else:
                target.mkdir()
                undo.folders.append(target)
            for child in sorted(source.iterdir()):
                if is_link(child):
                    continue  # links aren't synced (FR-SYNC-07); planning left them out too
                name = _nfc_name(child)
                existing = _existing_name(target, name) if target.is_dir() else None
                self._copy(child, target / (existing or name), aside, undo, stop, count)
            return
        temp = aside / f"{uuid.uuid4().hex}.copying"  # inside this import's own folder
        try:
            _copy_file(source, temp, stop, count)
            if os.path.lexists(target):
                if target.is_dir() or is_link(target):
                    raise FileActionError(FileProblem.NOT_A_FILE, target.name)
                held = aside / uuid.uuid4().hex / target.name  # restorable by its own name
                held.parent.mkdir()
                _swap_in(temp, target, held)
                undo.replaced.append((target, held))
            else:
                rename_no_replace(temp, target)
            undo.files.append((target, _identity(target)))
        finally:
            temp.unlink(missing_ok=True)

    def _take_back(self, undo: _Undo, aside: Path) -> list[Path]:
        """Remove what this import placed, if it is still exactly that, and put replaced
        originals back. Returns what had to be left as it is."""
        kept: list[Path] = []
        held_for = dict(undo.replaced)
        for place, placed in reversed(undo.files):
            original = held_for.pop(place, None)
            still_ours = _identity_or_none(place) == placed
            try:
                if original is not None and still_ours:
                    original.replace(place)  # over our own copy only: verified above
                elif original is not None:
                    kept.append(self._keep_beside(original, place))
                elif still_ours:
                    place.unlink()
                elif os.path.lexists(place):
                    logger.info(
                        "Left %s after a cancelled upload: it changed meanwhile", place.name
                    )
                    kept.append(place)
            except OSError:  # open in another program (Windows): leave both, report them
                logger.warning("Couldn't take back %s; it is left as it is", place.name)
                kept.append(place)
                if original is not None:
                    kept.append(self._keep_beside(original, place))
        for place, original in held_for.items():  # swapped in, but stopped before recording
            if not os.path.lexists(place):
                try:
                    rename_no_replace(original, place)
                    continue
                except OSError:
                    pass
            kept.append(self._keep_beside(original, place))
        for folder in reversed(undo.folders):
            try:
                folder.rmdir()  # only if empty: anything put in it meanwhile stays
            except OSError:
                kept.append(folder)
        _remove_empty(aside)
        return kept

    def _keep_beside(self, original: Path, place: Path) -> Path:
        """The original can't go back to its place (something else is there now): put it
        next to it under a free name, so the user sees both (never deleted)."""
        name = _free_name(place.parent, place.name, folder_item=False)
        try:
            rename_no_replace(original, place.parent / name)
        except OSError:
            logger.error("Kept the original of %s in the import folder", place.name)
            return original
        return place.parent / name

    def _finish(self, undo: _Undo, aside: Path) -> list[Path]:
        """Replaced originals go to the Recycle Bin, where the user can restore them.
        Returns those it refused; they stay in the import's folder in .dropshare-tmp."""
        kept: list[Path] = []
        for _, held in undo.replaced:
            try:
                self._recycle_bin.move_to_recycle_bin(held)
            except OSError:
                logger.warning("The Recycle Bin refused the replaced %s; it is kept", held.name)
                kept.append(held)
        _remove_empty(aside)
        return kept

    # --- planning helpers ----------------------------------------------------------------

    def _source(self, path: Path, folder: Path, stop: threading.Event) -> Source:
        if is_link(path):
            raise FileActionError(FileProblem.SOURCE_LINK, path.name)
        if not path.exists():
            raise FileActionError(FileProblem.SOURCE_GONE, path.name)
        name = _nfc_name(path)
        resolved, inside = path.resolve(), folder.resolve()
        if path.is_dir() and (inside == resolved or inside.is_relative_to(resolved)):
            raise FileActionError(FileProblem.INTO_ITSELF, name)
        return Source(path, name, path.is_dir(), _walk(path, stop))

    def _folder(self, target: str) -> Path:
        folder = self._root / target if target else self._root
        normal, root = Path(os.path.normpath(folder)), Path(os.path.normpath(self._root))
        if normal != root and not normal.is_relative_to(root):
            raise FileActionError(FileProblem.OUTSIDE)
        relative = normal.relative_to(root).as_posix()
        if relative == TEMP_DIR or relative.startswith(f"{TEMP_DIR}/"):
            raise FileActionError(FileProblem.OUTSIDE)
        if not folder.is_dir() or is_link(folder):
            raise FileActionError(FileProblem.FOLDER_GONE)
        return folder

    def _temp(self) -> Path:
        temp = self._root / TEMP_DIR
        temp.mkdir(exist_ok=True)
        return temp


def _swap_in(temp: Path, target: Path, held: Path) -> None:
    """Keep the original at `held` and put `temp` at `target` with no moment where the path
    is empty: a hard link aside, then one atomic replace. Without hard links (not NTFS or
    APFS) it falls back to two renames, a moment apart."""
    try:
        os.link(target, held)
    except OSError:
        rename_no_replace(target, held)
        rename_no_replace(temp, target)
        return
    temp.replace(target)


def _copy_file(
    source: Path, target: Path, stop: threading.Event, count: Callable[[int], None]
) -> None:
    with source.open("rb") as reading, target.open("xb") as writing:
        while chunk := reading.read(_READ_SIZE):
            if stop.is_set():
                raise TransferCancelledError()
            writing.write(chunk)
            count(len(chunk))
        writing.flush()
        os.fsync(writing.fileno())
    shutil.copystat(source, target)


def _walk(path: Path, stop: threading.Event) -> int:
    """Bytes in `path`, checking every name below it as the server would. Links are left out,
    as the copy leaves them out."""
    if not path.is_dir():
        return path.stat().st_size
    total = 0
    pending = [path]
    while pending:
        if stop.is_set():
            raise TransferCancelledError()
        folder = pending.pop()
        for child in folder.iterdir():
            if is_link(child):
                continue
            _nfc_name(child)
            if child.is_dir():
                pending.append(child)
            else:
                total += child.stat().st_size
    return total


def _identity(path: Path) -> _Identity:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _identity_or_none(path: Path) -> _Identity | None:
    try:
        return _identity(path) if not is_link(path) else None
    except OSError:
        return None


def _remove_empty(folder: Path) -> None:
    """Remove `folder` and the empty folders in it; anything with a file in it stays."""
    for current, _, _ in sorted(os.walk(folder), key=lambda item: -len(item[0])):
        try:
            Path(current).rmdir()
        except OSError:
            continue


def _nfc_name(path: Path) -> str:
    try:
        return validate_name(path.name)
    except InvalidNameError as exc:
        raise FileActionError(FileProblem.BAD_NAME, path.name, str(exc)) from None


def _names_in(folder: Path) -> set[str]:
    return {normalize_name(entry.name) for entry in folder.iterdir()}


def _existing_name(folder: Path, name: str) -> str | None:
    """The spelling on disk of the item called `name` ignoring case and Unicode form."""
    wanted = normalize_name(name)
    return next((e.name for e in folder.iterdir() if normalize_name(e.name) == wanted), None)


def _free_name(folder: Path, name: str, *, folder_item: bool) -> str:
    """`name`, or "Name (1).ext", "Name (2).ext" … whichever is free (FR-FILE-02)."""
    taken = _names_in(folder)
    if normalize_name(name) not in taken:
        return name
    stem, dot, extension = name.rpartition(".")
    if folder_item or not stem:
        stem, dot, extension = name, "", ""
    for number in range(1, _MAX_NUMBER):
        candidate = f"{stem} ({number}){dot}{extension}"
        if normalize_name(candidate) not in taken:
            return candidate
    raise FileActionError(FileProblem.NO_FREE_NAME, name)


def _join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name
