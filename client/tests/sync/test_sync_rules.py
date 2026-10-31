"""Safety rules around the matrix: ignored and unsyncable names, files still being written,
a missing sync folder, the Recycle Bin fallback, a rebuilt index, and server refusals."""

import os
import threading
import unicodedata
from pathlib import Path

import pytest
from sync_fakes import (
    Device,
    FakeFileServer,
    FakeRecycleBin,
    InodeIdentity,
    NoAttributes,
    Recorder,
    plain,
)

from dropshare_client.services.errors import ApiError
from dropshare_client.store.index import Index
from dropshare_client.sync.core import STILL_WRITING_DELAY, SyncCore
from dropshare_client.sync.errors import SyncRootMissingError, UnsafePathError
from dropshare_client.sync.scanner import LocalItem
from dropshare_shared.errors import ErrorCode


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    return Device(tmp_path, server)


def test_ignored_names_never_sync(device: Device, server: FakeFileServer) -> None:
    """FR-SYNC-07, in both directions."""
    for name in ("desktop.ini", "Thumbs.db", "~$report.docx", "draft.tmp", ".~lock.a.odt#"):
        device.write(f"Docs/{name}", b"noise")
    device.write("Docs/.DS_Store", b"noise")
    server.put("Docs")
    server.put("Docs/~$other.docx", b"noise")
    server.put("Docs/real.txt", b"real")

    device.sync()

    assert server.tree() == {"Docs": None, "Docs/~$other.docx": b"noise", "Docs/real.txt": b"real"}
    assert "Docs/~$other.docx" not in device.tree()
    assert device.tree()["Docs/real.txt"] == b"real"


def test_a_name_the_server_refuses_is_listed_not_uploaded(
    device: Device, server: FakeFileServer
) -> None:
    device.write("bad:name.txt", b"x")
    device.write("Trailing.", b"x")
    device.write("fine.txt", b"x")

    device.sync()

    assert server.tree() == {"fine.txt": b"x"}
    problems = {p.path: p.reason for p in device.index.problems()}
    assert set(problems) == {"bad:name.txt", "Trailing."}
    assert "Rename it" in problems["bad:name.txt"]


def test_links_are_listed_not_followed(
    device: Device, server: FakeFileServer, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"not for syncing")
    (device.root / "link").symlink_to(outside, target_is_directory=True)

    device.sync()

    assert server.tree() == {}
    assert [p.path for p in device.index.problems()] == ["link"]


def test_a_file_still_being_written_waits(tmp_path: Path, server: FakeFileServer) -> None:
    """FR-SYNC-06: modified within the last 2 s means not yet."""
    root = tmp_path / "DropShare"
    root.mkdir()
    (root / "growing.bin").write_bytes(b"part")
    core = SyncCore(
        root,
        Index(tmp_path / "index.db"),
        server,
        FakeRecycleBin(tmp_path / "bin"),
        InodeIdentity(),
        NoAttributes(),
        Recorder(),
        display_name="A",
        stop=threading.Event(),
    )
    core.connect()
    core.fetch()

    result = core.cycle({""})

    assert server.tree() == {}
    assert result.retry == {"growing.bin": STILL_WRITING_DELAY}


def test_a_missing_sync_folder_deletes_nothing(device: Device, server: FakeFileServer) -> None:
    device.write("Docs/a.txt", b"a")
    device.sync()
    (device.root / "Docs" / "a.txt").unlink()
    (device.root / "Docs").rmdir()
    device.root.rmdir()

    with pytest.raises(SyncRootMissingError):
        device.sync()

    assert server.tree() == {"Docs": None, "Docs/a.txt": b"a"}


def test_without_a_recycle_bin_items_are_kept_aside(device: Device, server: FakeFileServer) -> None:
    """algorithm.md §8: never deleted outright, even when the Recycle Bin refuses."""
    for n in range(5):
        device.write(f"{n}.txt", b"keep me")
    device.sync()
    device.bin.broken = True
    server.remove("0.txt")

    device.sync()

    kept = list((device.root / ".dropshare-tmp" / "deleted").rglob("0.txt"))
    assert [path.read_bytes() for path in kept] == [b"keep me"]
    assert "0.txt" not in device.tree()
    assert [p.path for p in device.index.problems()] == ["0.txt"]


def test_a_rebuilt_index_links_equal_files_and_keeps_both_of_different_ones(
    tmp_path: Path, server: FakeFileServer
) -> None:
    device = Device(tmp_path, server)
    device.write("same.txt", b"same")
    device.write("differs.txt", b"mine")
    device.write("only-here.txt", b"here")
    server.put("same.txt", b"same")
    server.put("differs.txt", b"theirs")
    server.put("only-there.txt", b"there")

    device.sync()

    expected = {
        "same.txt": b"same",
        "differs.txt": b"theirs",
        "differs (conflict).txt": b"mine",
        "only-here.txt": b"here",
        "only-there.txt": b"there",
    }
    assert plain(device.tree()) == plain(server.tree()) == expected
    assert server.trashed() == []
    assert device.bin.items() == []


def test_a_refused_upload_is_listed_and_the_rest_continues(
    device: Device, server: FakeFileServer
) -> None:
    device.write("big.iso", b"too big")
    device.write("small.txt", b"fine")
    server.fail(
        "upload",
        ApiError(409, ErrorCode.QUOTA_EXCEEDED, "There isn't enough space left in your quota."),
        skip=1,  # the smaller file goes first (FR-XFER-03)
    )

    device.sync()

    assert server.tree() == {"small.txt": b"fine"}
    (problem,) = device.index.problems()
    assert problem.path == "big.iso"
    assert "space" in problem.reason


def test_decomposed_local_names_reach_the_server_composed(
    device: Device, server: FakeFileServer
) -> None:
    decomposed = unicodedata.normalize("NFD", "Café.txt")
    device.write(decomposed, b"x")

    device.sync()

    assert list(server.tree()) == ["Café.txt"]


def test_names_differing_only_in_case_are_the_same_item(
    device: Device, server: FakeFileServer
) -> None:
    device.write("report.txt", b"same")
    server.put("Report.txt", b"same")

    device.sync()

    assert server.tree() == {"Report.txt": b"same"}
    assert [p.lower() for p in device.tree()] == ["report.txt"]


def test_a_file_changed_after_hashing_is_not_uploaded_as_it_was(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", b"first")
    device.core.connect()
    device.core.fetch()
    original_hash = device.core._scanner.hash

    def hash_then_edit(item: LocalItem, stop: threading.Event) -> LocalItem:
        hashed = original_hash(item, stop)
        target = device.root / "doc.txt"
        target.write_bytes(b"second, longer")
        os.utime(target, ns=(hashed.mtime_ns + 10**9, hashed.mtime_ns + 10**9))
        return hashed

    device.core._scanner.hash = hash_then_edit  # type: ignore[method-assign]
    result = device.core.cycle({""})
    assert server.tree() == {}
    assert result.again

    device.restart()
    device.sync()
    assert server.tree() == {"doc.txt": b"second, longer"}


def test_two_names_for_one_key_are_listed_not_merged(
    device: Device, server: FakeFileServer
) -> None:
    """ "Straße" and "Strasse" are different names on disk but one name on the server (S5)."""
    (device.root / "Straße.txt").write_bytes(b"one")
    try:
        (device.root / "Strasse.txt").write_bytes(b"two")
    except FileExistsError:
        pytest.skip("this file system treats the two names as one")
    if len(list(device.root.iterdir())) < 2:
        pytest.skip("this file system treats the two names as one")

    device.sync()

    assert server.tree() == {}
    assert next(p.reason for p in device.index.problems()).startswith("Another item")


def test_a_folder_replaced_by_a_link_is_never_written_through(
    device: Device, server: FakeFileServer, tmp_path: Path
) -> None:
    """A remote change below it must not land outside the sync folder (S6)."""
    device.write("Docs/a.txt", b"a")
    device.sync()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (device.root / "Docs" / "a.txt").unlink()
    (device.root / "Docs").rmdir()
    (device.root / "Docs").symlink_to(outside, target_is_directory=True)
    server.put("Docs/b.txt", b"from the server")

    device.sync(local=[])

    assert list(outside.iterdir()) == []
    assert server.tree()["Docs/a.txt"] == b"a"  # nothing deleted because of the link


def test_paths_never_lead_out_of_the_sync_folder(device: Device) -> None:
    scanner = device.core._scanner
    for path in ("../outside.txt", "a/../../x", "/etc/passwd"):
        with pytest.raises(UnsafePathError):
            scanner.disk_path(path)


def test_paths_too_long_for_windows_are_listed(tmp_path: Path, server: FakeFileServer) -> None:
    """D4: without long-path support, an over-long path is "can't sync", never "gone"."""
    device = Device(tmp_path, server)
    limit = len(str(device.root)) + 30
    device.max_path = limit
    device.restart()
    long_name = "n" * 40 + ".txt"
    device.write(long_name, b"local")
    server.put("r" * 40 + ".txt", b"remote")

    device.sync()

    assert server.tree() == {"r" * 40 + ".txt": b"remote"}
    assert ("r" * 40 + ".txt") not in device.tree()
    reasons = {p.path: p.reason for p in device.index.problems()}
    assert "too long" in reasons[long_name]
    assert "too long" in reasons["r" * 40 + ".txt"]
