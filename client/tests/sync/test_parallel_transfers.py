"""Several files transfer at once, smallest first (FR-XFER-03). Uploads wait "on the wire"
until the test lets them through, so what is running at any moment is decided by the test,
not by timing: many small changes never wait behind one large file, at most `max_transfers`
move at a time, a pause sends nothing more, and a crash with several in flight loses nothing.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer

from dropshare_client.services.errors import ApiError, TransferCancelledError
from dropshare_client.services.files import (
    Progress,
    SourceFile,
    UploadSessions,
    UploadTarget,
    no_progress,
)
from dropshare_client.sync.core import SMALL_TRANSFER_BYTES
from dropshare_client.sync.errors import SyncStoppedError
from dropshare_client.sync.transfers import TransferEvent, TransferStatus
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.nodes import NodeOut

_WAIT = 10.0  # seconds; reached only if the engine never gets there
BIG = b"B" * (SMALL_TRANSFER_BYTES + 1)


class WireServer(FakeFileServer):
    """Uploads wait on the wire until let through: all of them while `hold` is set, and the
    files named in `held` until they are released. Records what started and what is on the
    wire."""

    def __init__(self) -> None:
        super().__init__()
        self.hold = threading.Event()
        self.held: set[str] = set()
        self.started: list[str] = []
        self.on_wire: set[str] = set()
        self.most_on_wire = 0
        self.changed = threading.Condition()

    def upload(
        self,
        source: SourceFile,
        target: UploadTarget,
        stop: threading.Event,
        progress: Progress = no_progress,
        *,
        sessions: UploadSessions | None = None,
    ) -> NodeOut:
        name = source.path.name
        with self.changed:
            self.started.append(name)
            self.on_wire.add(name)
            self.most_on_wire = max(self.most_on_wire, len(self.on_wire))
            self.changed.notify_all()
        try:
            with self.changed:
                while self.hold.is_set() or name in self.held:
                    if stop.is_set():
                        raise TransferCancelledError()
                    self.changed.wait(0.01)
            return super().upload(source, target, stop, progress, sessions=sessions)
        finally:
            with self.changed:
                self.on_wire.discard(name)
                self.changed.notify_all()

    def wait_until(self, check: Callable[[], bool]) -> None:
        with self.changed:
            assert self.changed.wait_for(check, timeout=_WAIT), "never got there"

    def release(self, name: str | None = None) -> None:
        with self.changed:
            if name is None:
                self.hold.clear()
            else:
                self.held.discard(name)
            self.changed.notify_all()


@pytest.fixture
def server() -> WireServer:
    return WireServer()


@pytest.fixture
def device(tmp_path: Path, server: WireServer) -> Device:
    return Device(tmp_path / "laptop", server)


@contextmanager
def _syncing(device: Device, server: WireServer) -> Iterator[list[SyncStoppedError]]:
    """Sync on another thread, as the engine does, while the test holds the wire. Whatever
    happens in the test, the wire is let go at the end so the sync can finish."""
    errors: list[SyncStoppedError] = []

    def run() -> None:
        try:
            device.sync()
        except SyncStoppedError as exc:  # handed to the test, which checks it
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield errors
    finally:
        with server.changed:
            server.hold.clear()
            server.held.clear()
            server.changed.notify_all()
        thread.join(_WAIT)
        assert not thread.is_alive(), "sync never finished"


def _files(device: Device, count: int) -> None:
    for n in range(count):
        device.write(f"f{n:02d}.txt", f"{n}".encode())


def test_at_most_four_files_move_at_once(device: Device, server: WireServer) -> None:
    _files(device, 10)
    server.hold.set()

    with _syncing(device, server):
        server.wait_until(lambda: len(server.on_wire) == 4)
        assert len(server.started) == 4  # the rest wait their turn, not even started
        server.release()

    assert server.most_on_wire == 4
    assert len(server.tree()) == 10


def test_the_limit_is_a_setting(device: Device, server: WireServer) -> None:
    device.restart(max_transfers=2)  # as Settings will set it (M11)
    _files(device, 5)
    server.hold.set()

    with _syncing(device, server):
        server.wait_until(lambda: len(server.on_wire) == 2)
        server.release()

    assert server.most_on_wire == 2


def test_small_files_pass_a_large_one_that_is_still_moving(
    device: Device, server: WireServer
) -> None:
    device.write("big.bin", b"B" * 1000)
    _files(device, 5)
    server.held.add("big.bin")

    with _syncing(device, server):
        # Every small file gets through while the large one is still on the wire.
        server.wait_until(lambda: len(server.tree()) == 5 and "big.bin" in server.on_wire)
        server.release("big.bin")

    assert "big.bin" in server.tree()


def test_a_change_made_while_a_large_file_moves_goes_beside_it(
    device: Device, server: WireServer
) -> None:
    """FR-XFER-03 and NFR-PERF-02 across cycles: once only large files are moving, the cycle
    ends, and the next one sends what changed meanwhile without waiting for them."""
    device.write("big.bin", BIG)
    server.held.add("big.bin")

    device.sync()  # returns while big.bin is still on the wire
    assert server.on_wire == {"big.bin"}
    device.write("note.txt", b"saved meanwhile")
    device.sync()

    assert "note.txt" in server.tree()
    assert "big.bin" not in server.tree() and server.on_wire == {"big.bin"}
    server.release("big.bin")
    server.wait_until(lambda: not server.on_wire)
    device.sync()  # records it
    assert server.tree()["big.bin"] == BIG
    assert device.index.entry("big.bin") is not None


def test_a_pause_sends_nothing_more_and_stops_what_is_moving(
    device: Device, server: WireServer
) -> None:
    _files(device, 10)
    events: list[TransferEvent] = []
    device.restart(transfer=events.append)
    server.hold.set()

    with _syncing(device, server) as errors:
        server.wait_until(lambda: len(server.on_wire) == 4)
        device.stop.set()  # the user pauses
        server.wait_until(lambda: not server.on_wire)  # each noticed and stopped

    assert [type(e) for e in errors] == [SyncStoppedError]
    assert len(server.started) == 4  # the six queued ones never reached the server
    assert server.tree() == {} and not server.on_wire
    last = {e.path: e for e in events}
    assert {n: last[n].status for n in last} == {
        f"f{n:02d}.txt": TransferStatus.WAITING for n in range(10)
    }


def test_out_of_space_while_others_are_moving_waits_for_them(
    device: Device, server: WireServer
) -> None:
    """Each running upload reserves its whole size (D1): one refused for space while others
    are still moving is tried again, not listed as can't sync."""
    device.write("a-first.bin", b"A" * 100)
    device.write("b-second.bin", b"B" * 200)
    server.held.add("a-first.bin")
    server.fail("upload", ApiError(409, ErrorCode.QUOTA_EXCEEDED, "Not enough space"))

    with _syncing(device, server):
        server.wait_until(lambda: "b-second.bin" in server.started and len(server.on_wire) == 1)
        server.release("a-first.bin")

    assert device.index.problems() == []
    device.sync()
    assert set(server.tree()) == {"a-first.bin", "b-second.bin"}


def test_a_crash_with_several_files_in_flight_converges_after_a_restart(
    tmp_path: Path,
) -> None:
    server = FakeFileServer()
    device = Device(tmp_path / "laptop", server)
    _files(device, 6)
    seen: list[str] = []

    def crash_on_second_upload(point: str) -> None:
        seen.append(point)
        if seen.count("upload.completed") == 2:
            raise RuntimeError("power cut")

    device.restart(crash_point=crash_on_second_upload)
    with pytest.raises(RuntimeError):
        device.sync()

    device.restart()
    device.sync()

    assert server.tree() == device.tree()
    assert len(server.tree()) == 6
    assert not any("conflicted copy" in name for name in device.tree())
