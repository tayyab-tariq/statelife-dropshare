"""A crash at every executor step, then a restart (algorithm.md §10, roadmap M5).

One scenario uses every kind of step at once. It is run once to list the crash points it
passes, then again for each of them with the power "cut" there. After a restart the engine
must reach exactly the state an uninterrupted run reaches: same files on both sides, same
Recycle Bin and server trash, no extra copies.
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from sync_fakes import Crash, Device, FakeFileServer, Listing, Tree, plain


def _prepare(base: Path) -> tuple[Device, FakeFileServer]:
    server = FakeFileServer(page_size=3)
    device = Device(base, server)
    for name in ("keep", "edit-here", "edit-there", "both", "gone-here", "gone-there"):
        device.write(f"{name}.txt", b"base")
    device.write("Kept/old.txt", b"old")
    device.write("Revived/old.txt", b"old")
    for _ in range(10):  # enough files that the deletes below aren't a mass delete
        device.write(f"bulk/{_}.txt", b"bulk")
    device.sync()

    device.write("new-here/n.txt", b"new here")  # remote folder + upload
    server.put("new-there")  # local folder + download
    server.put("new-there/n.txt", b"new there")
    device.write("edit-here.txt", b"edited here")  # new version up
    server.put("edit-there.txt", b"edited there")  # new version down
    device.write("both.txt", b"both here")  # conflict
    server.put("both.txt", b"both there")
    device.delete("gone-here.txt")  # to the server trash
    server.remove("gone-there.txt")  # to the Recycle Bin
    device.write("Kept/added.txt", b"added here")  # FR-CONF-03: folder kept for the new file
    server.remove("Kept")
    device.delete("Revived")  # FR-CONF-03 the other way
    server.put("Revived/new.txt", b"new there")
    return device, server


def _outcome(device: Device, server: FakeFileServer) -> tuple[Tree, Tree, Listing, Listing]:
    return plain(device.tree()), plain(server.tree()), server.trashed(), device.bin.items()


def _crash_at(index: int, seen: list[str]) -> Callable[[str], None]:
    def point(name: str) -> None:
        seen.append(name)
        if len(seen) == index + 1:
            raise Crash(name)

    return point


def _expected(tmp_path: Path) -> tuple[tuple[Tree, Tree, Listing, Listing], list[str]]:
    device, server = _prepare(tmp_path / "reference")
    points: list[str] = []
    device.restart(crash_point=points.append)
    device.sync()
    return _outcome(device, server), points


def test_the_scenario_passes_every_crash_point(tmp_path: Path) -> None:
    (local, remote, _, _), points = _expected(tmp_path)
    assert set(points) == {
        "conflict.renamed",
        "make_local_folder.created",
        "make_remote_folder.created",
        "upload.completed",
        "download.fetched",
        "download.replaced",
        "delete_local.done",
        "delete_remote.done",
    }
    assert local == remote


def test_a_crash_at_any_step_converges_after_restart(tmp_path: Path) -> None:
    expected, points = _expected(tmp_path)
    for index, name in enumerate(points):
        device, server = _prepare(tmp_path / f"crash-{index}")
        seen: list[str] = []
        device.restart(crash_point=_crash_at(index, seen))
        with pytest.raises(Crash):
            device.sync()

        device.restart()
        device.sync()

        assert _outcome(device, server) == expected, f"crash at {name} (step {index})"
        assert device.index.journal() == []


def test_a_crash_while_reading_the_journal_resumes_from_the_saved_cursor(
    tmp_path: Path,
) -> None:
    expected, _ = _expected(tmp_path)
    device, server = _prepare(tmp_path / "paged")
    server.fail("changes", Crash("between pages"), skip=1)  # page 1 saved, then the cut

    with pytest.raises(Crash):
        device.sync()
    device.restart()
    device.sync()

    assert _outcome(device, server) == expected


def test_an_upload_journaled_before_a_crash_is_not_a_conflict(tmp_path: Path) -> None:
    """The upload reached the server, the app died, then the user edited again: the edit is
    an ordinary new version, not a conflicted copy (the journal remembers the upload)."""
    server = FakeFileServer()
    device = Device(tmp_path, server)
    device.write("doc.txt", b"v1")
    device.sync()
    device.write("doc.txt", b"v2")
    device.restart(crash_point=_crash_at(0, []))
    with pytest.raises(Crash):
        device.sync()  # v2 is on the server; the index still says v1

    device.write("doc.txt", b"v3")
    device.restart()
    device.sync()

    assert device.tree() == server.tree() == {"doc.txt": b"v3"}
    assert device.log.conflicts == []
