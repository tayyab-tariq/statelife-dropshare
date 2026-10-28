"""The running engine: its threads, long-poll wake-ups, pause and resume, offline backoff,
mass-delete confirmation and stopping (FR-SYNC-03/04/05/08, transfers.md)."""

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sync_fakes import FakeFileServer, FakeRecycleBin, InodeIdentity, NoAttributes

from dropshare_client.services.errors import SessionExpiredError
from dropshare_client.store.index import Index
from dropshare_client.sync.engine import SyncEngine, SyncProblem, SyncState

TIMEOUT = 10.0


class Listener:
    def __init__(self) -> None:
        self.states: list[tuple[SyncState, SyncProblem | None]] = []
        self.conflicts: list[tuple[str, str]] = []
        self.confirmations: list[tuple[int, int]] = []
        self.expired = threading.Event()
        self.pending: list[int] = []
        self._changed = threading.Condition()

    def state_changed(self, state: SyncState, problem: SyncProblem | None) -> None:
        with self._changed:
            self.states.append((state, problem))
            self._changed.notify_all()

    def conflict(self, path: str, copy_path: str) -> None:
        self.conflicts.append((path, copy_path))

    def confirm_deletes(self, here: int, on_server: int) -> None:
        with self._changed:
            self.confirmations.append((here, on_server))
            self._changed.notify_all()

    def session_expired(self) -> None:
        self.expired.set()

    def progress(self, pending: int) -> None:
        self.pending.append(pending)

    @property
    def state(self) -> SyncState | None:
        return self.states[-1][0] if self.states else None

    def wait_for(self, condition: Callable[[], bool]) -> None:
        deadline = time.monotonic() + TIMEOUT
        with self._changed:
            while not condition():
                remaining = deadline - time.monotonic()
                assert remaining > 0, f"timed out; states were {self.states}"
                self._changed.wait(min(remaining, 0.05))


def eventually(condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer(wait_seconds=0.2)


@pytest.fixture
def listener() -> Listener:
    return Listener()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "DropShare"
    folder.mkdir()
    return folder


@pytest.fixture
def engine(
    tmp_path: Path, root: Path, server: FakeFileServer, listener: Listener
) -> Iterator[SyncEngine]:
    engine = SyncEngine(
        root,
        Index(tmp_path / "index.db"),
        server,
        FakeRecycleBin(tmp_path / "bin"),
        InodeIdentity(),
        NoAttributes(),
        listener,
        display_name="Alice",
        watch=False,
        stable_after_ns=0,
        settle_seconds=0.05,
    )
    yield engine
    engine.stop()


def _write(root: Path, name: str, content: bytes) -> None:
    (root / name).write_bytes(content)


def test_local_changes_reach_the_server(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    engine.start()
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)

    _write(root, "a.txt", b"a")
    engine.local_changed("a.txt")

    eventually(lambda: server.tree() == {"a.txt": b"a"})
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)


def test_remote_changes_arrive_through_the_long_poll(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    engine.start()
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)

    server.put("b.txt", b"from elsewhere")

    eventually(lambda: (root / "b.txt").exists())
    assert (root / "b.txt").read_bytes() == b"from elsewhere"


def test_paused_engine_syncs_nothing_until_resumed(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    engine.start()
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)

    engine.pause()
    _write(root, "a.txt", b"a")
    engine.local_changed("a.txt")
    time.sleep(0.3)
    assert server.tree() == {}
    assert listener.state == SyncState.PAUSED

    engine.resume()
    eventually(lambda: server.tree() == {"a.txt": b"a"})


def test_offline_shows_and_recovers(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    server.offline = True
    _write(root, "a.txt", b"a")
    engine.start()
    listener.wait_for(lambda: listener.state == SyncState.OFFLINE)

    server.offline = False

    eventually(lambda: server.tree() == {"a.txt": b"a"})
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)


def test_mass_delete_waits_for_the_answer(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    server.put("Docs")
    for n in range(12):
        server.put(f"Docs/{n}.txt", b"x")
    engine.start()
    eventually(lambda: len(list(root.glob("Docs/*.txt"))) == 12)
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)

    for n in range(11):
        server.remove(f"Docs/{n}.txt")
    listener.wait_for(lambda: listener.confirmations == [(11, 0)])
    assert listener.state == SyncState.NEEDS_CONFIRMATION
    assert (root / "Docs" / "0.txt").exists()

    engine.confirm_deletes(approved=True)

    eventually(lambda: len(list(root.glob("Docs/*.txt"))) == 1)


def test_declining_a_mass_delete_pauses(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    for n in range(12):
        server.put(f"{n}.txt", b"x")
    engine.start()
    eventually(lambda: len(list(root.glob("*.txt"))) == 12)
    for n in range(11):
        server.remove(f"{n}.txt")
    listener.wait_for(lambda: bool(listener.confirmations))

    engine.confirm_deletes(approved=False)

    listener.wait_for(lambda: listener.state == SyncState.PAUSED)
    time.sleep(0.2)
    assert (root / "0.txt").exists()


def test_a_missing_folder_is_an_error_not_a_delete(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    server.put("a.txt", b"a")
    root.rmdir()

    engine.start()

    listener.wait_for(lambda: (SyncState.ERROR, SyncProblem.ROOT_MISSING) in listener.states)
    assert server.tree() == {"a.txt": b"a"}


def test_an_expired_session_stops_the_engine(
    engine: SyncEngine, server: FakeFileServer, listener: Listener
) -> None:
    server.fail("namespaces", SessionExpiredError())

    engine.start()

    assert listener.expired.wait(TIMEOUT)


def test_stop_finishes_the_engine_thread(
    engine: SyncEngine, server: FakeFileServer, listener: Listener
) -> None:
    engine.start()
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)

    assert engine.stop()
    assert listener.state == SyncState.STOPPED


def test_with_the_watcher_on_downloaded_folders_stay_put(
    tmp_path: Path, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    """The engine's own downloads fire watcher events for the new folder; the cycles they
    start must look inside it, not trash its files on the server (audit B1)."""
    engine = SyncEngine(
        root,
        Index(tmp_path / "watched.db"),
        server,
        FakeRecycleBin(tmp_path / "bin"),
        InodeIdentity(),
        NoAttributes(),
        listener,
        display_name="Alice",
        watch=True,
        stable_after_ns=0,
        settle_seconds=0.05,
    )
    engine.start()
    try:
        listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)
        server.put("Project")
        for n in range(3):
            server.put(f"Project/{n}.txt", b"x")
        eventually(lambda: len(list(root.glob("Project/*.txt"))) == 3)
        time.sleep(1.0)  # the watcher's events arrive and their cycles run

        (root / "Project" / "new.txt").write_bytes(b"added here")
        eventually(lambda: "Project/new.txt" in server.tree())
    finally:
        engine.stop()
    assert server.trashed() == []
    assert len(server.tree()) == 5


def test_a_server_rename_that_fails_once_here_is_retried_as_a_rename(
    engine: SyncEngine,
    root: Path,
    server: FakeFileServer,
    listener: Listener,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Word holds the file when another device renames it: the rename waits and is tried
    again as one, never as Recycle Bin + download (audit B1)."""
    from dropshare_client import platform
    from dropshare_client.sync import executor

    _write(root, "doc.txt", b"doc")
    engine.start()
    eventually(lambda: server.tree() == {"doc.txt": b"doc"})
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)
    locked = [True]

    def rename(source: Path, target: Path) -> None:
        if locked:
            locked.clear()
            raise PermissionError(32, "The file is in use", str(source))
        platform.rename_no_replace(source, target)

    monkeypatch.setattr(executor, "rename_no_replace", rename)
    server.calls.clear()
    server.relocate("doc.txt", "plan.txt")

    eventually(lambda: (root / "plan.txt").exists() and not (root / "doc.txt").exists())
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)
    assert not locked  # it did fail once
    assert "download" not in server.calls
    assert list((tmp_path / "bin").iterdir()) == []


def test_a_local_rename_whose_request_fails_once_is_retried_as_a_rename(
    engine: SyncEngine, root: Path, server: FakeFileServer, listener: Listener
) -> None:
    """A 503 on the PATCH: the folder's old path waits with it, so it isn't trashed and
    uploaded again (audit B1)."""
    from dropshare_client.services.errors import ApiError
    from dropshare_shared.errors import ErrorCode

    (root / "F").mkdir()
    _write(root, "F/a.txt", b"a")
    engine.start()
    eventually(lambda: server.tree() == {"F": None, "F/a.txt": b"a"})
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)
    ids = {node.id for node in server.nodes.values()}
    server.fail("move", ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Busy"))
    server.calls.clear()

    (root / "F").rename(root / "G")
    engine.local_changed("F")
    engine.local_changed("G")

    eventually(lambda: server.tree() == {"G": None, "G/a.txt": b"a"})
    listener.wait_for(lambda: listener.state == SyncState.UP_TO_DATE)
    assert server.calls.count("move") == 2  # the 503, then the retry
    assert server.trashed() == [] and "upload" not in server.calls
    assert {node.id for node in server.nodes.values()} == ids
