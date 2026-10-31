"""Per-file transfer events for the Transfers view (FR-CLIENT-08): throttled to ~4 updates a
second per file, increasing, ending at 100%; failures say whether they'll be retried."""

import threading
from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer

from dropshare_client.services.errors import ApiError, TransferCancelledError
from dropshare_client.sync.errors import SyncStoppedError
from dropshare_client.sync.transfers import (
    Direction,
    TransferEvent,
    TransferReporter,
    TransferStatus,
)
from dropshare_shared.errors import ErrorCode


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_progress_is_throttled_increasing_and_ends_at_the_total() -> None:
    clock = Clock()
    events: list[TransferEvent] = []
    reporter = TransferReporter(events.append, clock=clock)
    reporter.queued("big.iso", "big.iso", Direction.UPLOAD, 1000)
    reporter.started("big.iso", "big.iso", Direction.UPLOAD, 1000)

    for step in range(1, 101):  # 100 chunks over 2 seconds
        clock.now = step * 0.02
        reporter.progress("big.iso", step * 10)
    reporter.finished("big.iso")

    updates = [e for e in events if e.status is TransferStatus.ACTIVE]
    # The start, then one every 0.25 s for 2 s: never more than ~4 a second.
    assert 8 <= len(updates) <= 9
    done = [e.done for e in updates]
    assert done == sorted(done)
    assert events[0].status is TransferStatus.QUEUED
    assert (events[-1].status, events[-1].done, events[-1].percent) == (
        TransferStatus.DONE,
        1000,
        100,
    )


def test_progress_for_a_file_that_isnt_active_is_ignored() -> None:
    events: list[TransferEvent] = []
    reporter = TransferReporter(events.append, clock=Clock())

    reporter.progress("nothing.txt", 5)
    reporter.finished("nothing.txt")
    reporter.failed("nothing.txt", "why")

    assert events == []


def test_a_failure_ends_the_transfer_once() -> None:
    events: list[TransferEvent] = []
    reporter = TransferReporter(events.append, clock=Clock())
    reporter.started("a.txt", "a.txt", Direction.DOWNLOAD, 10)

    reporter.failed("a.txt", "No space")
    reporter.waiting("a.txt", "Will try again shortly.")  # already ended: no second row

    assert [(e.status, e.reason) for e in events[1:]] == [(TransferStatus.FAILED, "No space")]


def test_an_empty_file_is_complete() -> None:
    event = TransferEvent("e", "e", Direction.UPLOAD, TransferStatus.ACTIVE, 0, 0)

    assert event.percent == 100


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    return Device(tmp_path, server)


def _record(device: Device) -> list[TransferEvent]:
    events: list[TransferEvent] = []
    device.restart(transfer=events.append)
    return events


def _by_file(events: list[TransferEvent]) -> dict[str, list[TransferStatus]]:
    result: dict[str, list[TransferStatus]] = {}
    for event in events:
        result.setdefault(event.path, []).append(event.status)
    return result


def test_a_cycle_reports_its_uploads_and_downloads_from_queued_to_done(
    device: Device, server: FakeFileServer
) -> None:
    device.write("up.txt", b"up")
    server.put("down.txt", b"down here")
    events = _record(device)

    device.sync()

    assert _by_file(events) == {
        "up.txt": [TransferStatus.QUEUED, TransferStatus.ACTIVE, TransferStatus.DONE],
        "down.txt": [TransferStatus.QUEUED, TransferStatus.ACTIVE, TransferStatus.DONE],
    }
    down = [e for e in events if e.path == "down.txt"]
    assert {(e.direction, e.total) for e in down} == {(Direction.DOWNLOAD, 9)}
    assert down[-1].done == 9


def test_a_refused_upload_fails_with_the_reason_and_a_busy_server_waits(
    device: Device, server: FakeFileServer
) -> None:
    device.write("big.bin", b"too big")
    device.write("later.txt", b"later")
    # The smaller file goes first (FR-XFER-03): it meets the busy server.
    server.fail("upload", ApiError(503, ErrorCode.SERVICE_UNAVAILABLE, "Busy"))
    server.fail("upload", ApiError(409, ErrorCode.QUOTA_EXCEEDED, "Over quota"))
    events = _record(device)

    device.core.connect()
    device.core.fetch()
    device.core.cycle({""})

    ended = {
        e.path: (e.status, e.reason)
        for e in events
        if e.status
        not in (
            TransferStatus.QUEUED,
            TransferStatus.ACTIVE,
        )
    }
    assert ended["big.bin"][0] is TransferStatus.FAILED
    assert "space" in ended["big.bin"][1]
    assert ended["later.txt"] == (TransferStatus.WAITING, "Will try again shortly.")


def test_a_file_open_in_another_program_waits_with_that_reason(
    device: Device, server: FakeFileServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.put("doc.txt", b"from the server")
    events = _record(device)

    def locked(self: Path, target: Path) -> Path:
        raise PermissionError(32, "in use")

    monkeypatch.setattr(Path, "replace", locked)
    device.core.connect()
    device.core.fetch()
    device.core.cycle({""})

    [last] = [e for e in events if e.path == "doc.txt"][-1:]
    assert last.status is TransferStatus.WAITING
    assert "another program" in last.reason


def test_stopping_mid_transfer_leaves_it_waiting(
    device: Device, server: FakeFileServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    device.write("a.txt", b"a")
    events = _record(device)

    def paused_while_sending(*_: object, **__: object) -> None:
        device.stop.set()  # the user pauses; the transfer notices and stops
        raise TransferCancelledError()

    monkeypatch.setattr(server, "upload", paused_while_sending)

    device.core.connect()
    device.core.fetch()
    with pytest.raises(SyncStoppedError):
        device.core.cycle({""})

    assert (events[-1].path, events[-1].status, events[-1].reason) == (
        "a.txt",
        TransferStatus.WAITING,
        "Paused.",
    )


def test_progress_from_several_threads_stays_in_order_per_file() -> None:
    """Transfers run side by side (FR-XFER-03): each file's events still only go forward and
    end at its size."""
    events: list[TransferEvent] = []
    reporter = TransferReporter(events.append, interval=0.0)
    keys = [f"f{n}.bin" for n in range(4)]
    for key in keys:
        reporter.started(key, key, Direction.UPLOAD, 1000)

    def send(key: str) -> None:
        for done in range(0, 1001, 10):
            reporter.progress(key, done)
        reporter.finished(key)

    threads = [threading.Thread(target=send, args=(key,)) for key in keys]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for key in keys:
        mine = [e for e in events if e.key == key and e.status is TransferStatus.ACTIVE]
        assert [e.done for e in mine] == sorted(e.done for e in mine)
        assert [e for e in events if e.key == key][-1].status is TransferStatus.DONE


def test_a_locked_file_waits_and_goes_once_it_is_released(
    device: Device, server: FakeFileServer
) -> None:
    """FR-SYNC-06 on any system: the file can't be read while another program holds it."""
    device.write("budget.xlsx", b"numbers")
    events = _record(device)
    server.fail("upload", PermissionError(32, "The process cannot access the file"))

    device.core.connect()
    device.core.fetch()
    first = device.core.cycle({""})
    device.sync()  # released: the retry goes through

    waiting = [e for e in events if e.status is TransferStatus.WAITING]
    assert "budget.xlsx" in first.retry and device.index.problems() == []
    assert waiting and "another program" in waiting[-1].reason
    assert server.tree() == {"budget.xlsx": b"numbers"}
    assert events[-1].status is TransferStatus.DONE
