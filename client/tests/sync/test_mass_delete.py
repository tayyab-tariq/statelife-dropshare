"""FR-SYNC-08: a cycle that would delete more than 500 files or more than 25% of the synced
files, in either direction, stops and waits for the user before deleting anything."""

from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer

from dropshare_client.sync.reconciler import is_mass_delete


@pytest.mark.parametrize(
    ("deletes", "synced", "expected"),
    [
        (500, 100_000, False),
        (501, 100_000, True),
        (25, 100, False),
        (26, 100, True),
        (2, 4, False),  # small: below the 10-file floor of the share rule (D1)
        (10, 10, False),
        (11, 40, True),
        (11, 50, False),
        (0, 0, False),
    ],
)
def test_thresholds(deletes: int, synced: int, expected: bool) -> None:
    assert is_mass_delete(deletes, synced) is expected


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    device = Device(tmp_path, server)
    for n in range(40):
        device.write(f"Docs/{n}.txt", str(n).encode())
    device.sync()
    return device


def test_local_deletes_wait_for_confirmation(device: Device, server: FakeFileServer) -> None:
    for n in range(11):
        device.delete(f"Docs/{n}.txt")

    result = device.sync(confirm_deletes=False)

    assert result.confirm_deletes == (0, 11)
    assert len(server.tree()) == 41  # nothing deleted on the server yet
    assert "trash" not in server.calls

    device.sync()  # confirmed
    assert len(server.tree()) == 30


def test_remote_deletes_wait_for_confirmation(device: Device, server: FakeFileServer) -> None:
    server.remove("Docs")

    result = device.sync(confirm_deletes=False)

    assert result.confirm_deletes == (40, 0)
    assert len(device.tree()) == 41
    assert device.bin.items() == []

    device.sync()
    assert device.tree() == {}
    assert len(device.bin.items()) == 41  # the folder and its forty files


def test_other_changes_in_the_cycle_wait_too(device: Device, server: FakeFileServer) -> None:
    for n in range(11):
        device.delete(f"Docs/{n}.txt")
    device.write("new.txt", b"new")

    device.sync(confirm_deletes=False)

    assert "new.txt" not in server.tree()


def test_a_few_deletes_go_ahead(device: Device, server: FakeFileServer) -> None:
    device.delete("Docs/0.txt")
    device.delete("Docs/1.txt")

    result = device.sync(confirm_deletes=False)

    assert result.confirm_deletes is None
    assert len(server.tree()) == 39


def test_confirmation_covers_only_the_deletes_shown(device: Device, server: FakeFileServer) -> None:
    for n in range(11):
        device.delete(f"Docs/{n}.txt")
    device.sync(confirm_deletes=False)
    device.core.approve_deletes()
    for n in range(11, 14):  # more deletes before the next cycle: ask again
        device.delete(f"Docs/{n}.txt")

    result = device.sync(confirm_deletes=False)

    assert result.confirm_deletes == (0, 14)
    assert len(server.tree()) == 41
