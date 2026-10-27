"""Requests retried after a timeout never duplicate or lose anything (NFR-REL-04,
transfers.md "Retries and idempotency"), and stale writes become conflicts, not overwrites."""

from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer, plain

from dropshare_client.services.errors import OfflineError


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    return Device(tmp_path, server)


@pytest.mark.parametrize("method", ["upload", "create_folder", "trash", "download"])
def test_an_answer_lost_after_the_server_acted(
    device: Device, server: FakeFileServer, method: str
) -> None:
    device.write("keep/a.txt", b"a")
    device.write("old.txt", b"old")
    for n in range(4):
        device.write(f"bulk/{n}.txt", b"bulk")
    device.sync()
    device.write("folder/new.txt", b"new")  # create_folder + upload
    device.delete("old.txt")  # trash
    server.put("keep/b.txt", b"b")  # download
    server.fail(method, OfflineError("timed out"), after=True)

    with pytest.raises(OfflineError):
        device.sync()
    device.sync()

    expected = {
        "keep": None,
        "keep/a.txt": b"a",
        "keep/b.txt": b"b",
        "folder": None,
        "folder/new.txt": b"new",
        **{"bulk": None},
        **{f"bulk/{n}.txt": b"bulk" for n in range(4)},
    }
    assert device.tree() == server.tree() == expected
    assert device.log.conflicts == []


def test_an_edit_uploaded_but_unconfirmed_is_not_uploaded_twice(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", b"v1")
    device.sync()
    device.write("doc.txt", b"v2")
    server.fail("upload", OfflineError("timed out"), after=True)
    with pytest.raises(OfflineError):
        device.sync()
    uploads = server.calls.count("upload")

    device.sync()

    assert server.calls.count("upload") == uploads
    assert device.tree() == server.tree() == {"doc.txt": b"v2"}


def test_offline_before_sending_retries_cleanly(device: Device, server: FakeFileServer) -> None:
    device.write("doc.txt", b"v1")
    server.offline = True
    with pytest.raises(OfflineError):
        device.sync()

    server.offline = False
    device.sync()

    assert server.tree() == {"doc.txt": b"v1"}


def test_a_delete_meeting_a_newer_server_edit_restores_the_edit(
    device: Device, server: FakeFileServer
) -> None:
    """FR-CONF-02 when the edit lands between fetching changes and deleting: the stale delete
    gets REVISION_CONFLICT and the edit comes back instead of being trashed."""
    for name in ("doc", "a", "b", "c", "d"):
        device.write(f"{name}.txt", b"base")
    device.sync()
    device.delete("doc.txt")
    device.core.fetch()
    server.put("doc.txt", b"edited there")

    result = device.core.cycle({""})
    assert result.again
    device.sync()

    assert device.tree()["doc.txt"] == b"edited there"
    assert server.tree()["doc.txt"] == b"edited there"
    assert server.trashed() == []


def test_an_edit_meeting_a_newer_server_edit_keeps_both(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", b"base")
    device.sync()
    device.write("doc.txt", b"edited here")
    device.core.fetch()
    server.put("doc.txt", b"edited there")

    device.core.cycle({""})  # the upload hits REVISION_CONFLICT
    device.sync()

    expected = {"doc.txt": b"edited there", "doc (conflict).txt": b"edited here"}
    assert plain(device.tree()) == plain(server.tree()) == expected


def test_a_folder_made_on_both_sides_at_once_is_one_folder(
    device: Device, server: FakeFileServer
) -> None:
    device.write("Shared/mine.txt", b"mine")
    device.core.connect()
    device.core.fetch()
    server.put("Shared")
    server.put("Shared/theirs.txt", b"theirs")

    device.core.cycle({""})  # create_folder hits NAME_CONFLICT with the server's folder
    device.sync()

    expected = {"Shared": None, "Shared/mine.txt": b"mine", "Shared/theirs.txt": b"theirs"}
    assert device.tree() == server.tree() == expected


def test_a_stale_copy_of_a_trashed_file_is_refreshed_not_retried_forever(
    device: Device, server: FakeFileServer
) -> None:
    """Our tree still has the file (as after a replayed upload answer), the server trashed
    it: the upload meets REVISION_CONFLICT, the node is checked, and the edit goes up as a
    new file (audit S1)."""
    device.write("doc.txt", b"v1")
    device.sync()
    server.remove("doc.txt")  # not fetched: the device's tree is now stale
    device.write("doc.txt", b"v2")

    first = device.core.cycle({""})
    second = device.core.cycle({""})

    assert first.again and not second.again
    assert server.tree() == {"doc.txt": b"v2"}


def test_a_lost_upload_answer_then_an_edit_is_not_a_conflict(
    device: Device, server: FakeFileServer
) -> None:
    """The upload reached the server but its answer didn't; the user saved again before
    the connection came back. Recovery runs on the next fetch, not only at restart (S2)."""
    device.write("doc.txt", b"v1")
    device.sync()
    device.write("doc.txt", b"v2")
    server.fail("upload", OfflineError("timed out"), after=True)
    with pytest.raises(OfflineError):
        device.sync()

    device.write("doc.txt", b"v3")
    device.sync()

    assert device.tree() == server.tree() == {"doc.txt": b"v3"}
    assert device.log.conflicts == []


def test_an_unknown_change_type_asks_for_that_node_and_carries_on(
    device: Device, server: FakeFileServer
) -> None:
    """A newer server's change type (B5): no endless re-reading of the journal."""
    server.put("doc.txt", b"v1")
    device.sync()
    node = server.put("doc.txt", b"v2")
    last = server.journal[-1]
    server.journal[-1] = last.model_copy(update={"type": "relabelled"})

    device.sync()

    assert device.tree() == {"doc.txt": b"v2"}
    assert server.calls.count("get_node") == 1
    assert node.id  # the change named this node


def test_a_folder_delete_never_takes_another_devices_new_file(
    device: Device, server: FakeFileServer
) -> None:
    """Deleted here; meanwhile another device added a file inside. The delete carries the
    cursor, the server refuses it, and the folder comes back with the new file (D2)."""
    for n in range(5):
        device.write(f"F/{n}.txt", b"x")
    for n in range(20):
        device.write(f"other/{n}.txt", b"x")
    device.sync()
    device.delete("F")
    device.core.fetch()
    server.put("F/new.txt", b"added elsewhere")

    device.core.cycle({""})
    device.sync()

    assert device.tree()["F/new.txt"] == b"added elsewhere"
    assert server.tree()["F/new.txt"] == b"added elsewhere"
    assert not any(name == "new.txt" for name, _ in server.trashed())
