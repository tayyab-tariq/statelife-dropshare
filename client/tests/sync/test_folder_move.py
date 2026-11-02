"""Moving the sync folder (FR-CLIENT-04 safe move): on one volume (rename) and to another (copy),
with every file kept exactly, a crash at every step finished or undone, and nothing of the
user's ever removed."""

import os
import shutil
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from sync_fakes import Crash, Device, FakeFileServer, FakeRecycleBin

from dropshare_client.platform import rename_no_replace
from dropshare_client.store.index import Index
from dropshare_client.store.records import Binding
from dropshare_client.sync import folder_move
from dropshare_client.sync.folder_move import (
    FolderMover,
    MoveAttentionError,
    MoveError,
    MoveProblem,
)

MODES = ["rename", "copy"]
_FILES = {
    "a.txt": b"alpha",
    "Docs/plan.docx": b"plan" * 1000,
    "Docs/Deep/notes.md": b"notes",
    ".dropshare-tmp/owner.json": b'{"owner": 1}',
    "Empty/": b"",
}

type Tree = dict[str, bytes]


def _write(root: Path, tree: Tree) -> None:
    for name, content in tree.items():
        path = root / name
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _read(root: Path) -> Tree:
    tree: Tree = {}
    for folder, folders, files in os.walk(root):
        here = Path(folder)
        for name in files:
            tree[(here / name).relative_to(root).as_posix()] = (here / name).read_bytes()
        for name in folders:
            if not any((here / name).iterdir()):
                tree[f"{(here / name).relative_to(root).as_posix()}/"] = b""
    return tree


class _Setup:
    def __init__(self, base: Path, mode: str) -> None:
        self.source = base / "Old" / "StateLife DropShare"
        self.target = base / "New" / "DropShare"
        self.journal = base / "app-data" / "folder-move.json"
        self.bin = FakeRecycleBin(base / "bin")
        self.switched: list[Path] = []
        self.mode = mode
        _write(self.source, _FILES)

    def mover(self, crash_point: Callable[[str], None] = lambda _: None) -> FolderMover:
        return FolderMover(
            self.journal,
            self.bin,
            self.switched.append,
            crash_point=crash_point,
            same_volume=lambda source, target: self.mode == "rename",
        )

    def move(self, crash_point: Callable[[str], None] = lambda _: None) -> folder_move.MoveOutcome:
        return self.mover(crash_point).move(self.source, self.target, threading.Event())


@pytest.fixture(params=MODES)
def setup(tmp_path: Path, request: pytest.FixtureRequest) -> _Setup:
    return _Setup(tmp_path, str(request.param))


def test_everything_moves_and_the_app_switches_to_the_new_place(setup: _Setup) -> None:
    outcome = setup.move()

    assert (outcome.root, outcome.moved, outcome.old_kept) == (setup.target, True, None)
    assert _read(setup.target) == _FILES
    assert setup.switched == [setup.target]
    assert not setup.source.exists()
    assert not setup.journal.exists()
    if setup.mode == "copy":  # the old folder, fully copied, is recoverable from the bin
        assert len(list(setup.bin.folder.iterdir())) == 1


def test_os_litter_in_the_new_place_is_never_overwritten(setup: _Setup) -> None:
    _write(setup.target, {"desktop.ini": b"theirs"})
    _write(setup.source, {"desktop.ini": b"ours"})

    setup.move()

    assert _read(setup.target) == {**_FILES, "desktop.ini": b"theirs"}
    assert not setup.source.exists()  # with its own desktop.ini, in the Recycle Bin
    assert setup.bin.items()


def _crash_points(setup: _Setup) -> list[str]:
    seen: list[str] = []
    setup.mover(seen.append).move(setup.source, setup.target, threading.Event())
    return seen


@pytest.mark.parametrize("mode", MODES)
def test_a_crash_at_any_step_is_finished_or_undone_without_losing_anything(
    tmp_path: Path, mode: str
) -> None:
    points = _crash_points(_Setup(tmp_path / "count", mode))
    assert len(points) >= 4

    for number, point in enumerate(points):
        setup = _Setup(tmp_path / str(number), mode)
        seen: list[str] = []

        def crash(name: str, number: int = number, seen: list[str] = seen) -> None:
            seen.append(name)
            if len(seen) == number + 1:
                raise Crash(name)

        with pytest.raises(Crash):
            setup.move(crash)
        outcome = setup.mover().recover()  # the app starts again

        assert outcome is not None, point
        assert not setup.journal.exists()
        if outcome.moved:
            assert outcome.root == setup.target
            assert _read(setup.target) == _FILES, point
            assert setup.switched[-1] == setup.target
        else:
            assert outcome.root == setup.source
            assert _read(setup.source) == _FILES, point
            assert not setup.target.exists(), point  # it was made by the move
            assert setup.switched == []


def test_undoing_a_copy_keeps_anything_that_isnt_an_identical_copy(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")
    copied: list[str] = []

    def crash(name: str) -> None:
        if name == "move_folder.copied":
            copied.append(name)
            if len(copied) == 3:
                raise Crash(name)

    with pytest.raises(Crash):
        setup.move(crash)
    _write(setup.target, {"Docs/mine.txt": b"put here by the user meanwhile"})
    (setup.target / "a.txt").write_bytes(b"edited in the new place")

    outcome = setup.mover().recover()

    assert outcome is not None and not outcome.moved
    assert _read(setup.source) == _FILES
    assert _read(setup.target) == {
        "a.txt": b"edited in the new place",
        "Docs/mine.txt": b"put here by the user meanwhile",
    }


def test_cancelling_a_copy_puts_everything_back(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")
    cancel = threading.Event()

    def progress(done: int, total: int) -> None:
        if done:
            cancel.set()

    with pytest.raises(MoveError) as raised:
        setup.mover().move(setup.source, setup.target, cancel, progress)

    assert raised.value.problem is MoveProblem.CANCELLED
    assert _read(setup.source) == _FILES
    assert not setup.target.exists()
    assert not setup.journal.exists() and setup.switched == []


def test_a_file_edited_while_it_is_copied_is_copied_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _Setup(tmp_path, "copy")
    original = folder_move._copy_hashing
    edits = [b"edited during the copy"]

    def copy_while_editing(
        source: Path, part: Path, cancel: threading.Event, on_bytes: Callable[[int], None]
    ) -> str:
        digest = original(source, part, cancel, on_bytes)
        if source.name == "a.txt" and edits:
            source.write_bytes(edits.pop())
            os.utime(source, ns=(1, 1))  # a visibly different modification time
        return digest

    monkeypatch.setattr(folder_move, "_copy_hashing", copy_while_editing)

    setup.move()

    assert _read(setup.target)["a.txt"] == b"edited during the copy"


def test_a_file_that_keeps_changing_stops_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _Setup(tmp_path, "copy")
    original = folder_move._copy_hashing
    count = iter(range(1000))

    def copy_while_editing(
        source: Path, part: Path, cancel: threading.Event, on_bytes: Callable[[int], None]
    ) -> str:
        digest = original(source, part, cancel, on_bytes)
        if source.name == "a.txt":
            os.utime(source, ns=(next(count) + 10, next(count) + 10))
        return digest

    monkeypatch.setattr(folder_move, "_copy_hashing", copy_while_editing)

    with pytest.raises(MoveError) as raised:
        setup.move()

    assert raised.value.problem is MoveProblem.CHANGING
    assert _read(setup.source) == _FILES and not setup.target.exists()


def test_not_enough_space_moves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = _Setup(tmp_path, "copy")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10, 10, 0))

    with pytest.raises(MoveError) as raised:
        setup.move()

    assert raised.value.problem is MoveProblem.NO_SPACE
    assert raised.value.needed > sum(len(content) for content in _FILES.values())
    assert _read(setup.source) == _FILES
    assert not setup.target.exists() and not setup.journal.exists()


def test_a_file_in_use_stops_a_rename_and_puts_back_what_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _Setup(tmp_path, "rename")
    original = rename_no_replace

    def locked(source: Path, target: Path) -> None:
        if source.name == "Docs" and target.parent == setup.target:
            raise PermissionError("in use")  # Windows: a file inside is open
        original(source, target)

    monkeypatch.setattr("dropshare_client.sync.folder_move.rename_no_replace", locked)

    with pytest.raises(MoveError) as raised:
        setup.move()

    assert raised.value.problem is MoveProblem.IN_USE
    assert _read(setup.source) == _FILES
    assert not setup.target.exists()


def test_links_are_refused_for_a_copy(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")
    (setup.source / "link").symlink_to(tmp_path)

    with pytest.raises(MoveError) as raised:
        setup.move()

    assert raised.value.problem is MoveProblem.HAS_LINKS
    assert not setup.journal.exists()


@pytest.mark.parametrize("inside", [True, False])
def test_a_place_inside_the_folder_or_holding_it_is_refused(tmp_path: Path, inside: bool) -> None:
    setup = _Setup(tmp_path, "rename")
    target = setup.source / "Sub" if inside else setup.source.parent

    with pytest.raises(MoveError) as raised:
        setup.mover().move(setup.source, target, threading.Event())

    assert raised.value.problem is MoveProblem.NESTED


def test_an_old_folder_that_cant_be_recycled_is_left_and_reported(setup: _Setup) -> None:
    setup.bin.broken = True
    _write(setup.source, {"Thumbs.db": b"x"})
    _write(setup.target, {"Thumbs.db": b"y"})  # so a renamed-away folder isn't empty

    outcome = setup.move()

    staged = setup.source.with_name("StateLife DropShare.dropshare-moving")
    assert outcome.moved and outcome.old_kept == staged
    assert staged.exists() and not setup.source.exists()


def test_rebinding_the_index_keeps_what_was_synced(tmp_path: Path) -> None:
    index = Index(tmp_path / "index.db")
    index.reset(Binding("https://dropshare.example.com", uuid.UUID(int=7), "/old"))
    index.add_activity("uploaded", "a.txt")

    index.rebind("/new")

    binding = index.binding()
    assert binding is not None and binding.sync_root == "/new"
    assert [item.path for item in index.recent_activity()] == ["a.txt"]
    index.close()


@pytest.mark.parametrize("mode", MODES)
def test_after_a_move_syncing_carries_on_without_sending_anything(
    tmp_path: Path, mode: str
) -> None:
    server = FakeFileServer()
    device = Device(tmp_path / "pc", server)
    device.write("a.txt", b"alpha")
    device.write("Docs/plan.docx", b"plan")
    device.sync()
    before = server.tree()
    server.calls.clear()
    new_root = tmp_path / "other" / "DropShare"

    FolderMover(
        tmp_path / "folder-move.json",
        device.bin,
        lambda root: device.index.rebind(str(root)),
        same_volume=lambda source, target: mode == "rename",
    ).move(device.root, new_root, threading.Event())
    device.root = new_root
    device.restart()  # syncing starts again, in the new place
    device.sync()

    assert server.tree() == before
    writes = [call for call in server.calls if call not in ("changes", "wait", "namespaces")]
    assert writes == [], writes


def test_the_new_place_filled_since_it_was_checked_is_refused(setup: _Setup) -> None:
    _write(setup.target, {"Documents/theirs.txt": b"theirs"})

    with pytest.raises(MoveError) as raised:
        setup.move()

    assert raised.value.problem is MoveProblem.NOT_EMPTY
    assert _read(setup.source) == _FILES
    assert _read(setup.target) == {"Documents/theirs.txt": b"theirs"}


def test_nothing_can_be_written_under_the_old_path_while_copying(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")
    old_path_seen: list[bool] = []

    def progress(done: int, total: int) -> None:
        old_path_seen.append(setup.source.exists())

    setup.mover().move(setup.source, setup.target, threading.Event(), progress)

    assert old_path_seen and not any(old_path_seen)  # renamed aside before the first copy


def test_a_file_open_in_the_old_folder_refuses_the_move_up_front(
    setup: _Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rename_no_replace

    def locked(source: Path, target: Path) -> None:
        if source == setup.source:
            raise PermissionError("a file inside is open")
        original(source, target)

    monkeypatch.setattr("dropshare_client.sync.folder_move.rename_no_replace", locked)

    with pytest.raises(MoveError) as raised:
        setup.move()

    assert raised.value.problem is MoveProblem.IN_USE
    assert _read(setup.source) == _FILES
    assert not setup.target.exists() and not setup.journal.exists()


def test_an_undo_that_cant_finish_keeps_the_journal_until_it_can(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _Setup(tmp_path, "rename")
    original = rename_no_replace
    # a.txt (moved last) refuses to move; Docs, already moved, then refuses to come back.
    locked = {"a.txt", "Docs"}

    def flaky(source: Path, target: Path) -> None:
        moving_in = source.name == "a.txt" and target.parent == setup.target
        moving_back = source.name == "Docs" and source.parent == setup.target
        if source.name in locked and (moving_in or moving_back):
            raise PermissionError("in use")
        original(source, target)

    monkeypatch.setattr("dropshare_client.sync.folder_move.rename_no_replace", flaky)

    with pytest.raises(MoveAttentionError):
        setup.move()
    assert setup.mover().pending  # syncing must wait: the old folder is missing Docs

    locked.clear()  # the program let go
    outcome = setup.mover().recover()

    assert outcome is not None and not outcome.moved
    assert _read(setup.source) == _FILES
    assert not setup.journal.exists() and setup.switched == []


def test_a_new_place_missing_at_restart_sends_the_move_back(tmp_path: Path) -> None:
    """The drive was unplugged after the copy was verified: the old folder is still whole, so
    the app goes back to it rather than switching to nothing."""
    setup = _Setup(tmp_path, "copy")
    with pytest.raises(Crash):
        setup.move(lambda name: _raise_at(name, "move_folder.verified"))
    shutil.rmtree(setup.target)  # the test's stand-in for a drive that isn't there

    outcome = setup.mover().recover()

    assert outcome is not None and not outcome.moved
    assert _read(setup.source) == _FILES
    assert setup.switched == [setup.source] and not setup.journal.exists()


def test_an_unreadable_journal_blocks_syncing_rather_than_being_ignored(setup: _Setup) -> None:
    setup.journal.parent.mkdir(parents=True)
    setup.journal.write_text("{not json")

    with pytest.raises(MoveAttentionError):
        setup.mover().recover()

    assert setup.mover().pending


def test_dropshares_own_folder_is_hidden_in_the_new_place(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")
    hidden: list[Path] = []
    mover = FolderMover(
        setup.journal,
        setup.bin,
        setup.switched.append,
        hide=hidden.append,
        same_volume=lambda source, target: False,
    )

    mover.move(setup.source, setup.target, threading.Event())

    assert hidden == [setup.target / ".dropshare-tmp"]


def _raise_at(name: str, point: str) -> None:
    if name == point:
        raise Crash(name)


def test_a_copy_that_cant_be_removed_never_stops_putting_the_folder_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read-only copy (from a view-only shared folder) or one an antivirus holds: the undo
    goes on, and the original folder is back under its name."""
    setup = _Setup(tmp_path, "copy")
    cancel = threading.Event()
    original_unlink = Path.unlink
    refused: list[Path] = []

    def held_open(path: Path, missing_ok: bool = False) -> None:
        if path.name == "owner.json" and path.is_relative_to(setup.target):
            refused.append(path)
            raise PermissionError("held open")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", held_open)

    def progress(done: int, total: int) -> None:
        if done:
            cancel.set()  # after the first file

    with pytest.raises(MoveError):
        setup.mover().move(setup.source, setup.target, cancel, progress)

    assert refused  # tried, made writable, tried again, then left
    assert _read(setup.source) == _FILES
    assert not setup.journal.exists()
    # The duplicate that couldn't go stays; nothing depends on it.
    assert _read(setup.target) == {".dropshare-tmp/owner.json": _FILES[".dropshare-tmp/owner.json"]}


def test_a_verified_move_whose_new_place_lost_a_file_goes_back(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")  # after renames, a file gone there is the user's delete
    with pytest.raises(Crash):
        setup.move(lambda name: _raise_at(name, "move_folder.verified"))
    (setup.target / "Docs" / "plan.docx").unlink()  # quarantined, or deleted by the user

    outcome = setup.mover().recover()

    assert outcome is not None and not outcome.moved
    assert _read(setup.source) == _FILES
    assert setup.switched == [setup.source]  # back to the old place, whatever was switched
    assert not setup.journal.exists()


def test_the_new_drive_is_flushed_before_the_move_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = _Setup(tmp_path, "copy")
    synced: list[int] = []
    original = os.fsync

    def fsync(fd: int) -> None:
        synced.append(fd)
        original(fd)

    monkeypatch.setattr(os, "fsync", fsync)

    setup.move()

    assert synced
    assert not any(
        p.name.startswith(".flush-") for p in (setup.target / ".dropshare-tmp").iterdir()
    )


def test_dropshares_own_copies_are_never_synced() -> None:
    from dropshare_client.sync.paths import is_ignored

    assert is_ignored("report.xlsx.dropshare-move")
    assert not is_ignored("report.xlsx")


def test_cancel_stops_within_a_large_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup = _Setup(tmp_path, "copy")
    for path in list(setup.source.iterdir()):
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    big = b"v" * (5 * 1024 * 1024)  # five blocks: one video, say
    _write(setup.source, {"video.mp4": big})
    monkeypatch.setattr(folder_move, "_PROGRESS_SECONDS", 0.0)  # see every block
    cancel = threading.Event()

    def progress(done: int, total: int) -> None:
        if 0 < done < total:
            cancel.set()  # pressed while the one file is still copying

    with pytest.raises(MoveError) as raised:
        setup.mover().move(setup.source, setup.target, cancel, progress)

    assert raised.value.problem is MoveProblem.CANCELLED
    assert _read(setup.source) == {"video.mp4": big}
    assert not setup.target.exists()


def test_many_small_files_send_few_progress_reports(tmp_path: Path) -> None:
    setup = _Setup(tmp_path, "copy")
    _write(setup.source, {f"Small/{n}.txt": b"x" for n in range(300)})
    reports: list[int] = []

    setup.mover().move(
        setup.source, setup.target, threading.Event(), lambda d, t: reports.append(d)
    )

    assert len(reports) < 50  # throttled, not one per file
    assert reports[-1] == sum(len(c) for c in _read(setup.target).values())
