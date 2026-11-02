"""Moving the sync folder somewhere else (FR-CLIENT-04 "sync folder location, with safe move").

Called with syncing stopped (and held, so nothing starts it meanwhile). The new place is new or
empty, OS litter such as desktop.ini aside, which stays and is never overwritten. First the old
folder is renamed aside (`<name>.dropshare-moving`): a file open in another program refuses
that, and nothing can go on writing under the old path while the move runs. On the same volume
each item is then renamed into the new place; on another volume everything is copied and each
copy checked by SHA-256, a file changing while it is copied being copied again. Only when the
new place holds everything does the app switch to it (the index keeps its entries: their paths
are relative), and the old folder, emptied or fully copied, goes to the Recycle Bin. Nothing of
the user's is ever deleted.

A journal next to the index records the move before anything happens. After a crash,
`recover` finishes a move that was verified (checking the new place again first) and undoes one
that wasn't: renamed items go back, and a copy is taken away only where it matches its source
byte for byte or is one of our own part files (algorithm.md §8). When something can't be put
right (a file stays locked), the journal stays and `MoveAttentionError` says so: syncing must not
start until a later `recover` succeeds, or it would see the missing items as deleted.
"""

import contextlib
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO, Literal

from pydantic import BaseModel, ValidationError

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import RecycleBin
from dropshare_client.services.sync_folder import OS_LITTER, TEMP_DIR, is_link
from dropshare_client.sync.executor import no_crash

logger = logging.getLogger(__name__)

_READ_SIZE = 1024 * 1024
_PART_SUFFIX = ".dropshare-move"  # a copy until it is checked; ours alone
_STAGED_SUFFIX = ".dropshare-moving"  # the old folder while it moves
_COPY_ATTEMPTS = 3  # a file still changing after this many copies stops the move
_SPACE_MARGIN = 100 * 1024 * 1024  # left free on the new drive
_PROGRESS_SECONDS = 0.1  # copy progress reported at most this often

type Progress = Callable[[int, int], None]  # bytes copied, bytes in all


class MoveProblem(StrEnum):
    NO_SPACE = "no_space"
    IN_USE = "in_use"
    HAS_LINKS = "has_links"
    CHANGING = "changing"
    CANCELLED = "cancelled"
    NESTED = "nested"
    MISSING = "missing"
    NOT_EMPTY = "not_empty"


class MoveError(Exception):
    """The move didn't happen; everything is where it was. `needed` (NO_SPACE): bytes."""

    def __init__(self, problem: MoveProblem, needed: int = 0) -> None:
        super().__init__(problem.value)
        self.problem = problem
        self.needed = needed


class MoveAttentionError(Exception):
    """A move couldn't be finished or undone completely: the journal stays, and syncing must
    wait for a later `recover`. `folder` is where the user should look."""

    def __init__(self, folder: Path, *, unreadable: bool = False) -> None:
        super().__init__(str(folder))
        self.folder = folder
        self.unreadable = unreadable  # the journal itself: the user can't sort it out alone


@dataclass(frozen=True)
class MoveOutcome:
    root: Path  # the sync folder from now on
    moved: bool  # False: a move interrupted by a crash was undone
    old_kept: Path | None = None  # the old folder, left where it was: not recyclable


class _Journal(BaseModel):
    source: str
    staged: str  # the old folder, renamed aside while it moves
    target: str
    mode: Literal["rename", "copy"]
    phase: Literal["started", "verified"]
    target_existed: bool
    target_names: list[str]  # what the new place held before (OS litter only)


@dataclass(frozen=True)
class _File:
    path: str  # relative, "/"-separated
    size: int
    mtime_ns: int


class FolderMover:
    def __init__(
        self,
        journal_path: Path,
        recycle_bin: RecycleBin,
        switch: Callable[[Path], object],
        hide: Callable[[Path], None] = lambda _: None,
        crash_point: Callable[[str], None] = no_crash,
        same_volume: Callable[[Path, Path], bool] | None = None,
    ) -> None:
        """`switch(root)` makes `root` the sync folder: the index's binding and the setting.
        It must be safe to call again for the same root (recovery may repeat it). `hide`
        hides DropShare's own folder in the new place. `same_volume` is for tests, to copy
        between folders of one disk."""
        self._journal_path = journal_path
        self._recycle_bin = recycle_bin
        self._switch = switch
        self._hide = hide
        self._crash_point = crash_point
        self._same_volume = same_volume or _same_volume

    @property
    def pending(self) -> bool:
        """A move is unfinished: syncing must not start (see `recover`)."""
        return self._journal_path.exists()

    def move(
        self,
        source: Path,
        target: Path,
        cancel: threading.Event,
        progress: Progress = lambda done, total: None,
    ) -> MoveOutcome:
        """Blocking; run it on a worker with syncing stopped and held. MoveError if it can't
        be done, with everything left where it was; MoveAttentionError if putting it back
        failed partway (the journal stays)."""
        if self.pending:
            raise RuntimeError("recover() an interrupted move first")
        if not source.is_dir():
            raise MoveError(MoveProblem.MISSING)
        if _inside(target, source) or _inside(source, target):
            raise MoveError(MoveProblem.NESTED)
        mode: Literal["rename", "copy"] = "rename" if self._same_volume(source, target) else "copy"
        existed = target.is_dir()
        names = sorted(entry.name for entry in target.iterdir()) if existed else []
        if any(name.casefold() not in OS_LITTER for name in names):
            raise MoveError(MoveProblem.NOT_EMPTY)  # filled since it was checked
        journal = _Journal(
            source=str(source),
            staged=str(_free_name(source)),
            target=str(target),
            mode=mode,
            phase="started",
            target_existed=existed,
            target_names=names,
        )
        self._write(journal)
        self._crash_point("move_folder.started")

        staged, taken = Path(journal.staged), _folded(names)
        try:
            try:
                rename_no_replace(source, staged)
            except PermissionError:
                raise MoveError(MoveProblem.IN_USE) from None
            self._crash_point("move_folder.staged")
            if mode == "rename":
                self._rename_into(staged, target, taken)
            else:
                files, folders = _inventory(staged)
                _check_space(target, sum(f.size for f in files))
                _copy_all(
                    staged,
                    target,
                    files,
                    folders,
                    taken,
                    cancel,
                    progress,
                    lambda: self._crash_point("move_folder.copied"),
                )
            if (target / TEMP_DIR).is_dir():
                self._hide(target / TEMP_DIR)
            _flush_volume(target)  # what is on the new drive survives a power cut from here on
            if not _complete(journal):
                raise MoveError(MoveProblem.CHANGING)
        except Exception:
            logger.info("Moving the sync folder failed; putting everything back", exc_info=True)
            self._undo(journal)  # MoveAttentionError when it can't
            self._journal_path.unlink(missing_ok=True)
            raise
        verified = journal.model_copy(update={"phase": "verified"})
        self._write(verified)
        self._crash_point("move_folder.verified")
        return self._finish(verified)

    def recover(self) -> MoveOutcome | None:
        """Before syncing starts: finish or undo a move a crash interrupted; None if there was
        none. MoveAttentionError if that isn't possible yet (the journal stays)."""
        if not self.pending:
            return None
        try:
            journal = _Journal.model_validate_json(self._journal_path.read_bytes())
        except (OSError, ValidationError):
            logger.error("Unreadable folder-move journal %s", self._journal_path)
            raise MoveAttentionError(self._journal_path, unreadable=True) from None
        if journal.phase == "verified":
            return self._finish(journal)
        self._undo(journal)
        self._journal_path.unlink(missing_ok=True)
        return MoveOutcome(Path(journal.source), moved=False)

    # --- steps ---------------------------------------------------------------------------

    def _rename_into(self, staged: Path, target: Path, taken: set[str]) -> None:
        target.mkdir(parents=True, exist_ok=True)
        for child in sorted(staged.iterdir()):
            if child.name.casefold() in taken:
                continue  # OS litter in both: the old one goes with the old folder
            try:
                rename_no_replace(child, target / child.name)
            except PermissionError:
                raise MoveError(MoveProblem.IN_USE) from None
            self._crash_point("move_folder.renamed")

    def _finish(self, journal: _Journal) -> MoveOutcome:
        source, staged, target = Path(journal.source), Path(journal.staged), Path(journal.target)
        if not _complete(journal):
            # The new place lost something since (a drive gone, a copy deleted or quarantined):
            # the old folder is still whole, so go back to it.
            logger.warning("The moved sync folder is incomplete; going back to %s", source)
            self._undo(journal)  # MoveAttentionError when it can't
            self._switch(source)  # in case the app had switched already
            self._journal_path.unlink(missing_ok=True)
            return MoveOutcome(source, moved=False)
        self._switch(target)
        self._crash_point("move_folder.switched")
        kept: Path | None = None
        if staged.exists():
            try:
                if journal.mode == "rename" and not any(staged.iterdir()):
                    staged.rmdir()
                else:  # a full copy, or the OS litter left behind
                    self._recycle_bin.move_to_recycle_bin(staged)
            except OSError:
                logger.warning("Couldn't put the old sync folder away; leaving it", exc_info=True)
                kept = staged
        if kept is None and source.exists():
            logger.warning("Something made %s again during the move; leaving it", source)
            kept = source
        self._journal_path.unlink(missing_ok=True)
        return MoveOutcome(target, moved=True, old_kept=kept)

    def _undo(self, journal: _Journal) -> None:
        """Put everything back as it was, item by item, going on past what fails;
        MoveAttentionError (journal kept) if anything couldn't go back."""
        source, staged, target = Path(journal.source), Path(journal.staged), Path(journal.target)
        origin = staged if staged.exists() else source
        stuck: list[Path] = []
        if target.is_dir():
            taken = _folded(journal.target_names)
            if journal.mode == "rename":
                for child in sorted(target.iterdir()):
                    if child.name.casefold() in taken:
                        continue
                    try:
                        rename_no_replace(child, origin / child.name)
                    except OSError:
                        logger.warning("Couldn't move %s back", child, exc_info=True)
                        stuck.append(child)
            else:  # leftover copies are duplicates of the originals: nothing depends on them
                _remove_copies(origin, target, taken)
            if not journal.target_existed:
                with contextlib.suppress(OSError):  # not empty: something isn't ours
                    target.rmdir()
        if staged.exists():
            try:
                rename_no_replace(staged, source)
            except OSError:
                logger.warning("Couldn't rename %s back", staged, exc_info=True)
                stuck.append(staged)
        if stuck:
            raise MoveAttentionError(target if target.is_dir() else staged)

    def _write(self, journal: _Journal) -> None:
        self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=self._journal_path.parent, prefix=".move-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(journal.model_dump_json())
                out.flush()
                os.fsync(out.fileno())
            Path(temp_name).replace(self._journal_path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(temp_name).unlink()
            raise


def _flush_volume(target: Path) -> None:
    """Make the renames on the new drive durable before the move counts as done: fsyncing a
    file there (FlushFileBuffers on Windows) commits the volume's log, which on NTFS holds
    them in order. Directories can't be fsynced on Windows."""
    folder = target / TEMP_DIR
    folder.mkdir(exist_ok=True)
    handle, name = tempfile.mkstemp(dir=folder, prefix=".flush-")
    try:
        os.write(handle, b"flush")
        os.fsync(handle)
    finally:
        os.close(handle)
        Path(name).unlink(missing_ok=True)


def _free_name(source: Path) -> Path:
    """A name beside `source` for it while it moves, not already taken."""
    candidate = source.with_name(source.name + _STAGED_SUFFIX)
    number = 2
    while os.path.lexists(candidate):
        candidate = source.with_name(f"{source.name}{_STAGED_SUFFIX}-{number}")
        number += 1
    return candidate


def _complete(journal: _Journal) -> bool:
    """Whether the new place holds everything the old folder did: renamed, nothing left
    behind but OS litter; copied, every file there with its size. Looked at again before
    switching, so a drive not connected (or a write the disk lost) never counts as moved."""
    staged, target = Path(journal.staged), Path(journal.target)
    if not target.is_dir():
        return False
    if not staged.exists():
        return True  # put away already: the switch happened (recovery repeating the end)
    taken = _folded(journal.target_names)
    if journal.mode == "rename":
        return all(
            child.name.casefold() in taken or child.name.casefold() in OS_LITTER
            for child in staged.iterdir()
        )
    files, _ = _inventory(staged)
    for file in files:
        if _top(file.path) in taken:
            continue
        copy = _disk(target, file.path)
        if not copy.is_file() or copy.stat().st_size != file.size:
            return False
    return True


# --- helpers -------------------------------------------------------------------------------


def _folded(names: list[str]) -> set[str]:
    return {name.casefold() for name in names}


def _inside(path: Path, folder: Path) -> bool:
    child = os.path.normcase(os.path.normpath(path.absolute()))
    parent = os.path.normcase(os.path.normpath(folder.absolute()))
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _same_volume(source: Path, target: Path) -> bool:
    return source.stat().st_dev == _existing_parent(target).stat().st_dev


def _inventory(source: Path) -> tuple[list[_File], list[str]]:
    """Every file and folder below `source`. A link or junction can't be copied as one, and
    following it would copy what it points at: the move is refused instead."""
    files: list[_File] = []
    folders: list[str] = []

    def walk(folder: Path, prefix: str) -> None:
        with os.scandir(folder) as entries:
            for entry in sorted(entries, key=lambda e: e.name):
                path = Path(entry.path)
                relative = f"{prefix}{entry.name}"
                if is_link(path):
                    raise MoveError(MoveProblem.HAS_LINKS)
                if entry.is_dir(follow_symlinks=False):
                    folders.append(relative)
                    walk(path, f"{relative}/")
                elif entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    files.append(_File(relative, stat.st_size, stat.st_mtime_ns))

    walk(source, "")
    return files, folders


def _check_space(target: Path, size: int) -> None:
    free = shutil.disk_usage(_existing_parent(target)).free
    if free < size + _SPACE_MARGIN:
        raise MoveError(MoveProblem.NO_SPACE, needed=size + _SPACE_MARGIN - free)


def _disk(root: Path, relative: str) -> Path:
    return root.joinpath(*relative.split("/"))


def _top(relative: str) -> str:
    return relative.split("/", 1)[0].casefold()


def _copy_all(
    source: Path,
    target: Path,
    files: list[_File],
    folders: list[str],
    taken: set[str],
    cancel: threading.Event,
    progress: Progress,
    file_copied: Callable[[], None],
) -> None:
    """Copy everything, reporting bytes as they move (at most every _PROGRESS_SECONDS, so
    thousands of small files don't flood the UI) and stopping within a block of a cancel."""
    target.mkdir(parents=True, exist_ok=True)
    for folder in folders:
        if _top(folder) not in taken:
            _disk(target, folder).mkdir(exist_ok=True)
    total = sum(f.size for f in files)
    done = 0
    reported_at = 0.0

    def report(more: int) -> None:
        nonlocal done, reported_at
        done += more
        now = time.monotonic()
        if now - reported_at >= _PROGRESS_SECONDS:
            reported_at = now
            progress(done, total)

    progress(done, total)
    for file in files:
        if cancel.is_set():
            raise MoveError(MoveProblem.CANCELLED)
        before = done
        if _top(file.path) not in taken:
            _copy_checked(_disk(source, file.path), _disk(target, file.path), cancel, report)
        done = before + file.size  # a file copied again counts once
        file_copied()
    progress(total, total)


def _copy_checked(
    source: Path, target: Path, cancel: threading.Event, on_bytes: Callable[[int], None]
) -> None:
    """Copy one file and check the copy by SHA-256; copy again if the source changed while it
    was being copied. (The old folder is renamed aside, so nothing edits it afterwards.)"""
    part = target.with_name(target.name + _PART_SUFFIX)
    for _ in range(_COPY_ATTEMPTS):
        try:
            before = source.stat()
            digest = _copy_hashing(source, part, cancel, on_bytes)
            after = source.stat()
        except PermissionError:
            part.unlink(missing_ok=True)
            raise MoveError(MoveProblem.IN_USE) from None
        except MoveError:
            part.unlink(missing_ok=True)
            raise
        unchanged = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        if unchanged and _sha256(part) == digest:
            shutil.copystat(source, part)
            rename_no_replace(part, target)
            return
        part.unlink()
    raise MoveError(MoveProblem.CHANGING)


def _copy_hashing(
    source: Path, part: Path, cancel: threading.Event, on_bytes: Callable[[int], None]
) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as reader, part.open("xb") as writer:
        for block in _blocks(reader):
            if cancel.is_set():  # a large file stops within a block, not at its end
                raise MoveError(MoveProblem.CANCELLED)
            digest.update(block)
            writer.write(block)
            on_bytes(len(block))
        writer.flush()
        os.fsync(writer.fileno())
    return digest.hexdigest()


def _blocks(reader: BinaryIO) -> Iterator[bytes]:
    while block := reader.read(_READ_SIZE):
        yield block


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        for block in _blocks(reader):
            digest.update(block)
    return digest.hexdigest()


def _remove_copies(source: Path, target: Path, taken: set[str]) -> None:
    """Undo a copy: remove what is verifiably ours (a part file, or a copy identical to its
    source, so nothing is lost), then the folders that end up empty. Anything else stays."""
    for folder, subfolders, names in os.walk(target, topdown=False):
        here = Path(folder)
        relative = here.relative_to(target).as_posix()
        if relative != "." and _top(relative) in taken:
            continue
        for name in names:
            path = here / name
            if relative == "." and name.casefold() in taken:
                continue
            original = source / (Path(relative) / name if relative != "." else Path(name))
            if name.endswith(_PART_SUFFIX) or _same_content(original, path):
                _remove_ours(path)
        for name in subfolders:
            with contextlib.suppress(OSError):  # not empty: holds something not ours
                (here / name).rmdir()


def _remove_ours(path: Path) -> None:
    """Remove a copy known to be ours. A read-only one (copied from a view-only shared folder,
    FR-SHARE-05) is made writable first; one that still can't go (held open by an antivirus) is
    left: it is a duplicate, and putting the original back matters more."""
    try:
        path.unlink()
    except PermissionError:
        try:
            path.chmod(path.stat().st_mode | stat.S_IWRITE)
            path.unlink()
        except OSError:
            logger.warning("Couldn't remove the copy %s; leaving it", path, exc_info=True)
    except OSError:
        logger.warning("Couldn't remove the copy %s; leaving it", path, exc_info=True)


def _same_content(original: Path, copy: Path) -> bool:
    try:
        if is_link(original) or is_link(copy) or not original.is_file():
            return False
        if original.stat().st_size != copy.stat().st_size:
            return False
        return _sha256(original) == _sha256(copy)
    except OSError:
        return False
