"""Content safety on this computer (FR-SAFE-01/03, OQ-57 (1), OQ-59, roadmap M15c).

Every case checks the same promises: no server item is trashed; no local file is changed,
moved or deleted; nothing is uploaded again while it is unchanged (no loop); and the item is
listed in "Can't sync" with its reason.
"""

import hashlib
import uuid
from pathlib import Path

import pytest
from cloud_fakes import FakeCloud
from sync_fakes import Device, FakeFileServer

from dropshare_client.services.errors import OfflineError
from dropshare_client.services.files import NewFile, upload_key
from dropshare_client.store.records import StoredUpload
from dropshare_client.sync.transfers import TransferEvent, TransferStatus
from dropshare_shared.schemas.nodes import AccessRole

BASE = b"base"
EXE = ".exe"


@pytest.fixture
def server() -> FakeFileServer:
    fake = FakeFileServer()
    fake.banned = {EXE}
    return fake


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    return Device(tmp_path, server)


def _rename(device: Device, old: str, new: str) -> None:
    (device.root / old).rename(device.root / new)


def _problems(device: Device) -> dict[str, str]:
    return {problem.path: problem.reason for problem in device.index.problems()}


def _uploads(server: FakeFileServer) -> int:
    return server.calls.count("upload")


# --- Banned types, checked here before anything is sent (FR-SAFE-03) ----------------------


def test_a_banned_file_is_never_uploaded_and_stays_untouched(
    device: Device, server: FakeFileServer
) -> None:
    device.write("setup.EXE", b"MZ program")
    device.write("notes.txt", BASE)

    device.sync()
    device.sync()  # and again: nothing more happens

    assert server.tree() == {"notes.txt": BASE}
    assert (device.root / "setup.EXE").read_bytes() == b"MZ program"
    assert "This file type isn't allowed in DropShare (.exe)" in _problems(device)["setup.EXE"]
    assert _uploads(server) == 1  # notes.txt only


def test_a_banned_file_inside_a_new_folder_doesnt_hold_the_rest(
    device: Device, server: FakeFileServer
) -> None:
    device.write("Tools/readme.txt", BASE)
    device.write("Tools/setup.exe", b"MZ")

    device.sync()

    assert server.tree() == {"Tools": None, "Tools/readme.txt": BASE}
    assert list(_problems(device)) == ["Tools/setup.exe"]


@pytest.mark.parametrize("move", [False, True])
def test_renaming_a_synced_file_to_a_banned_type_holds_it_and_renaming_back_resumes(
    device: Device, server: FakeFileServer, move: bool
) -> None:
    # OQ-59 (1): the server copy keeps its old name and is never trashed.
    device.write("doc.txt", BASE)
    device.mkdir("Other")
    device.sync()
    target = "Other/doc.exe" if move else "doc.exe"

    _rename(device, "doc.txt", target)
    device.sync()
    device.sync()

    assert server.tree() == {"doc.txt": BASE, "Other": None}
    assert server.trashed() == []
    assert (device.root / target).read_bytes() == BASE
    assert list(_problems(device)) == [target]
    assert "move" not in server.calls

    _rename(device, target, "doc.txt")
    device.sync()

    assert server.tree() == {"doc.txt": BASE, "Other": None}
    assert _problems(device) == {}
    assert _uploads(server) == 1  # the first upload only


def test_renaming_to_another_allowed_name_after_a_banned_one_moves_the_file(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "doc.txt", "doc.exe")
    device.sync()

    _rename(device, "doc.exe", "renamed.txt")
    device.sync()

    assert server.tree() == {"renamed.txt": BASE}
    assert server.trashed() == []
    assert _problems(device) == {}


def test_a_path_too_long_for_windows_holds_a_rename_too(
    tmp_path: Path, server: FakeFileServer
) -> None:
    # OQ-59 (1): the gap this closes existed before M15 for too-long paths.
    device = Device(tmp_path, server, max_path=len(str(tmp_path / "pc")) + 40)
    device.write("doc.txt", BASE)
    device.sync()

    long_name = "a" * 60 + ".txt"
    _rename(device, "doc.txt", long_name)
    device.sync()

    assert server.tree() == {"doc.txt": BASE}
    assert server.trashed() == []
    assert (device.root / long_name).read_bytes() == BASE


# --- Refused by the server (FR-SAFE-01/02) ----------------------------------------------------


def test_a_type_banned_on_the_server_since_is_listed_and_not_sent_again(
    device: Device, server: FakeFileServer
) -> None:
    # The server's list changed after this computer asked: its own check decides.
    server.banned = set()
    server.refuses = {".iso"}
    device.write("disk.iso", b"image")

    device.sync()
    device.sync()

    assert server.tree() == {}
    assert "aren't allowed" in _problems(device)["disk.iso"]
    assert _uploads(server) == 1  # tried once, then remembered until it changes
    assert (device.root / "disk.iso").read_bytes() == b"image"


def test_a_rename_the_server_refuses_is_held_without_a_retry_loop(
    device: Device, server: FakeFileServer
) -> None:
    server.banned = set()
    server.refuses = {".iso"}
    device.write("doc.txt", BASE)
    device.sync()

    _rename(device, "doc.txt", "doc.iso")
    device.sync()
    device.sync()

    assert server.tree() == {"doc.txt": BASE}
    assert server.trashed() == []
    assert server.calls.count("move") == 1
    assert list(_problems(device)) == ["doc.iso"]


@pytest.mark.parametrize("synced_first", [False, True])
def test_malware_is_listed_kept_here_and_not_sent_again(
    device: Device, server: FakeFileServer, synced_first: bool
) -> None:
    server.malware = {b"VIRUS": "Eicar-Signature"}
    if synced_first:  # a new version of a synced file
        device.write("report.docx", BASE)
        device.sync()
    device.write("report.docx", b"a VIRUS inside")
    uploads_before = _uploads(server)

    device.sync()
    device.sync()

    assert server.tree() == ({"report.docx": BASE} if synced_first else {})
    assert server.trashed() == []
    assert (device.root / "report.docx").read_bytes() == b"a VIRUS inside"
    reason = _problems(device)["report.docx"]
    assert reason.startswith("A virus was found in this file (Eicar-Signature)")
    assert _uploads(server) == uploads_before + 1

    device.write("report.docx", b"cleaned up")  # changed: sent again
    device.sync()

    assert server.tree() == {"report.docx": b"cleaned up"}
    assert _problems(device) == {}


def test_a_refusal_survives_a_restart(device: Device, server: FakeFileServer) -> None:
    server.malware = {b"VIRUS": "Eicar-Signature"}
    device.write("bad.txt", b"VIRUS")
    device.sync()

    device.restart()
    device.sync()

    assert _uploads(server) == 1
    assert list(_problems(device)) == ["bad.txt"]


# --- Waiting for the scan (FR-SAFE-01) --------------------------------------------------------


def test_an_upload_waiting_for_the_scan_finishes_after_a_restart_without_resending(
    device: Device, server: FakeFileServer
) -> None:
    server.scan_waits = 1
    device.write("big.bin", BASE)

    device.sync()  # the server is still scanning it
    assert server.tree() == {}
    assert _problems(device) == {}

    device.restart()
    server.finish_scans()  # the scan worker publishes it meanwhile
    device.sync()

    assert server.tree() == {"big.bin": BASE}
    assert _uploads(server) == 1  # recorded as published: nothing sent again
    assert device.index.upload_sessions() == {}


# --- Shared folders ---------------------------------------------------------------------------


def test_an_editor_in_a_shared_folder_doesnt_upload_a_banned_file(
    tmp_path: Path,
) -> None:
    cloud = FakeCloud()
    cloud.banned = {EXE}
    alice, bob = cloud.account("alice"), cloud.account("bob")
    alice.put("Team")
    alice.put("Team/plan.txt", b"plan")
    bob.accept(cloud.share("alice", "Team", "bob", AccessRole.EDITOR))
    bobs = Device(tmp_path / "bob", bob)
    bobs.sync()

    bobs.write("Team/tool.exe", b"MZ")
    bobs.sync()

    assert "Team/tool.exe" not in alice.tree()
    assert (bobs.root / "Team/tool.exe").read_bytes() == b"MZ"
    assert "Team/tool.exe" in {problem.path for problem in bobs.index.problems()}


def test_nothing_here_is_ever_deleted_for_a_ban(device: Device, server: FakeFileServer) -> None:
    # OQ-57 (1): DropShare only declines to sync; it never removes or quarantines.
    device.write("a.exe", b"MZ")
    device.write("b.txt", BASE)
    stamp = (device.root / "a.exe").stat().st_mtime_ns

    for _ in range(3):
        device.sync()

    assert (device.root / "a.exe").read_bytes() == b"MZ"
    assert (device.root / "a.exe").stat().st_mtime_ns == stamp
    assert device.bin.items() == []


def test_transfers_show_the_file_waiting_for_the_scan(
    device: Device, server: FakeFileServer
) -> None:
    events: list[TransferEvent] = []
    device.restart(transfer=events.append)
    server.scan_waits = 1
    device.write("big.bin", BASE)

    device.sync()

    waiting = [e for e in events if e.status == TransferStatus.WAITING and e.key == "big.bin"]
    assert (
        waiting
        and waiting[-1].reason == "Waiting for the virus scan. It syncs once the scan is done."
    )
    assert not any(e.status == TransferStatus.FAILED for e in events)


# --- Held renames survive partial cycles (audit 2026-11-07-0712, blocker 1) -------------------


def test_a_held_folder_keeps_its_server_copy_when_another_device_adds_to_it(
    device: Device, server: FakeFileServer
) -> None:
    device.write("Reports/a.docx", b"a")
    device.write("Reports/b.xlsx", b"b")
    device.sync()
    _rename(device, "Reports", "CON")  # a name Windows can't use: held
    device.sync()

    server.put("Reports/new.docx", b"new")  # another device
    device.sync(local=[])  # only what the server's changes touch, as the running engine does
    device.sync(local=[])

    assert {"Reports/a.docx", "Reports/b.xlsx"} <= set(server.tree())
    assert server.trashed() == []
    assert (device.root / "CON" / "a.docx").read_bytes() == b"a"


def test_a_held_file_keeps_its_server_copy_when_it_changes_elsewhere(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "doc.txt", "doc.exe")
    device.sync()

    server.put("doc.txt", b"edited elsewhere")
    device.sync(local=[])

    assert server.tree() == {"doc.txt": b"edited elsewhere"}
    assert server.trashed() == []
    assert (device.root / "doc.exe").read_bytes() == BASE  # never overwritten


def test_a_held_file_survives_its_folder_being_renamed(
    device: Device, server: FakeFileServer
) -> None:
    device.write("Docs/plan.txt", BASE)
    device.sync()
    _rename(device, "Docs/plan.txt", "Docs/plan.exe")
    device.sync()

    _rename(device, "Docs", "Docs2")
    device.sync(local=["Docs", "Docs2"])  # what the watcher reports
    device.sync(local=[])

    assert server.tree() == {"Docs2": None, "Docs2/plan.txt": BASE}
    assert server.trashed() == []
    assert (device.root / "Docs2" / "plan.exe").read_bytes() == BASE


def test_a_held_file_renamed_to_an_allowed_name_is_moved_not_deleted(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "doc.txt", "doc.exe")
    device.sync()

    _rename(device, "doc.exe", "final.txt")
    device.sync(local=["doc.exe", "final.txt"])  # what the watcher reports
    device.sync(local=[])

    assert server.tree() == {"final.txt": BASE}
    assert server.trashed() == []


def test_a_rename_on_the_server_doesnt_move_a_held_file_here(
    device: Device, server: FakeFileServer
) -> None:
    device.write("report.txt", BASE)
    device.sync()
    _rename(device, "report.txt", "report.exe")
    device.sync()

    server.relocate("report.txt", "final.txt")  # another device
    device.sync(local=[])

    assert (device.root / "report.exe").read_bytes() == BASE  # where the user put it
    assert not (device.root / "final.txt").exists()
    assert server.trashed() == []


# --- Files synced before their type was banned (OQ-57 (2), blocker 2) -------------------------


def test_a_file_synced_before_its_type_was_banned_keeps_syncing(
    device: Device, server: FakeFileServer
) -> None:
    server.banned = set()
    server.put("site")
    server.put("site/app.js", b"v1")
    device.sync()
    server.banned = {".js"}
    device.restart()  # the client asks for the list again when it connects
    device.sync()

    assert _problems(device) == {}  # unchanged and synced: not listed
    server.put("site/app.js", b"v2")  # changed on the server: still arrives
    device.sync(local=[])
    assert (device.root / "site/app.js").read_bytes() == b"v2"

    server.remove("site")  # deleted elsewhere: the delete arrives too
    device.sync(local=[])
    assert server.tree() == {}
    assert not (device.root / "site").exists()


def test_a_pre_ban_file_changed_here_isnt_sent(device: Device, server: FakeFileServer) -> None:
    server.banned = set()
    server.put("app.js", b"v1")
    device.sync()
    server.banned = {".js"}
    device.restart()
    device.sync()

    device.write("app.js", b"edited here")
    device.sync()

    assert server.tree() == {"app.js": b"v1"}
    assert list(_problems(device)) == ["app.js"]


# --- Changes made here while the server scans (audit 2026-11-07-0712) -------------------------


def test_a_new_file_deleted_while_it_is_scanned_doesnt_come_back(
    device: Device, server: FakeFileServer
) -> None:
    server.scan_waits = 1
    device.write("big.bin", BASE)
    device.sync()  # the server is scanning it
    device.delete("big.bin")

    server.finish_scans()  # published by the scan worker
    device.sync(local=[])
    device.sync()

    assert not (device.root / "big.bin").exists()  # the user's delete wins
    assert server.tree() == {}
    assert server.trashed() == [("big.bin", BASE)]  # recoverable from the trash


def test_a_new_file_renamed_while_it_is_scanned_ends_up_once_under_its_new_name(
    device: Device, server: FakeFileServer
) -> None:
    # Before it is published it has no base here, so the renamed file goes up as a new one;
    # the published original is then recorded as synced and follows the user's rename to the
    # trash. Nothing comes back under the old name, and nothing is lost.
    server.scan_waits = 1
    device.write("big.bin", BASE)
    device.sync()
    _rename(device, "big.bin", "final.bin")
    device.sync(local=["big.bin", "final.bin"])  # what the watcher reports

    server.finish_scans()
    device.sync(local=[])
    device.sync()

    assert server.tree() == {"final.bin": BASE}
    assert device.tree() == {"final.bin": BASE}


def test_an_edit_while_the_earlier_version_is_scanned_is_the_next_version(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", b"v1")
    device.sync()
    server.scan_waits = 1
    device.write("doc.txt", b"v2")
    device.sync()  # v2 is being scanned
    device.write("doc.txt", b"v3")
    device.sync()  # waits for v2's scan: no second upload racing it

    server.finish_scans()  # v2 published
    device.sync(local=[])
    device.sync()

    assert server.tree() == {"doc.txt": b"v3"}
    assert device.tree() == {"doc.txt": b"v3"}  # no conflicted copy of its own edit
    assert server.trashed() == []


def test_without_the_ban_list_the_engine_doesnt_count_as_connected(
    device: Device, server: FakeFileServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Audit 2026-11-07-0712: asking again on the next try, rather than syncing without it.
    def unreachable(*, refresh: bool = False) -> frozenset[str]:
        raise OfflineError("lost the connection")

    monkeypatch.setattr(server, "banned_extensions", unreachable)
    with pytest.raises(OfflineError):
        device.core.connect()
    assert not device.core.connected

    monkeypatch.undo()
    device.write("setup.exe", b"MZ")
    device.sync()
    assert server.tree() == {}  # the list arrived: the banned file isn't sent


def test_waiting_for_the_scan_asks_less_and_less_often(
    device: Device, server: FakeFileServer
) -> None:
    server.scan_waits = 1
    device.write("big.bin", BASE)
    delays = []
    for _ in range(4):
        result = device.sync()
        delays.append(result.retry["big.bin"])

    assert delays == sorted(delays) and delays[-1] > delays[0]
    assert delays[-1] <= 300


# --- Re-review of the fixes (audit 2026-11-07-0712) -------------------------------------------


def test_a_held_file_saved_by_a_safe_save_editor_stays_held(
    device: Device, server: FakeFileServer
) -> None:
    # The editor writes a new file and renames it over the old one: a new file ID and size.
    device.write("util.txt", BASE)
    device.sync()
    _rename(device, "util.txt", "util.exe")
    device.sync()

    saved = device.root / "util.exe.tmp"
    saved.write_bytes(b"a much longer edited version")
    saved.replace(device.root / "util.exe")
    device.sync(local=["util.exe"])
    device.sync()

    assert server.tree() == {"util.txt": BASE}
    assert server.trashed() == []
    assert (device.root / "util.exe").read_bytes() == b"a much longer edited version"


def test_a_left_behind_upload_session_never_takes_someone_elses_file(
    device: Device, server: FakeFileServer
) -> None:
    # An interrupted upload whose file was moved away leaves its session; someone then adds
    # the same file there. It is theirs: downloaded, not taken for ours and deleted.
    content = b"same report"
    device.sync()
    device.index.put_upload_session(
        upload_key(NewFile(server.root.id, "report.pdf")),
        StoredUpload(uuid.uuid4(), hashlib.sha256(content).hexdigest(), len(content)),
    )

    server.put("report.pdf", content)  # another device
    device.sync(local=[])
    device.sync()

    assert server.tree() == {"report.pdf": content}
    assert server.trashed() == []
    assert (device.root / "report.pdf").read_bytes() == content


def test_a_held_file_renamed_and_edited_elsewhere_isnt_downloaded_beside_it(
    device: Device, server: FakeFileServer
) -> None:
    device.write("a.docx", BASE)
    device.sync()
    _rename(device, "a.docx", "a.exe")
    device.sync()

    server.relocate("a.docx", "final.docx")  # another device renames it...
    device.sync(local=[])
    server.put("final.docx", b"edited there")  # ...then edits it
    device.sync(local=[])

    assert device.tree() == {"a.exe": BASE}  # no copy downloaded next to the held file
    assert server.tree() == {"final.docx": b"edited there"}
    assert server.trashed() == []


def test_a_held_rename_goes_through_once_the_type_is_allowed(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "doc.txt", "doc.exe")
    device.sync()

    server.banned = set()  # the administrator allows the type again
    device.restart()
    device.sync()

    assert server.tree() == {"doc.exe": BASE}
    assert server.trashed() == []
    assert device.index.held_moves() == []
