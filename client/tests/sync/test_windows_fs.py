"""NTFS behaviour the engine relies on (windows-fs.md). Runs on the Windows test machine only:
macOS doesn't lock open files, so these can't be reproduced on the development Mac."""

import ctypes
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer

from dropshare_client.platform.windows.attributes import WindowsFileAttributes
from dropshare_client.sync.errors import UnsafePathError

if sys.platform == "win32":
    from dropshare_client.platform.windows.recycle_bin import WindowsRecycleBin

pytestmark = pytest.mark.windows_only

_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_INVALID_HANDLE = ctypes.c_void_p(-1).value


@contextmanager
def exclusively_open(path: Path) -> Iterator[None]:
    """Open with no sharing at all, as Office does with a document it is editing."""
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = ctypes.c_void_p
    handle = kernel32.CreateFileW(str(path), _GENERIC_READ, 0, None, _OPEN_EXISTING, 0, None)
    assert handle != _INVALID_HANDLE
    try:
        yield
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_the_temp_folder_is_hidden(tmp_path: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    folder = tmp_path / ".dropshare-tmp"
    folder.mkdir()

    WindowsFileAttributes().hide(folder)

    attributes = ctypes.windll.kernel32.GetFileAttributesW(str(folder))
    assert attributes & _FILE_ATTRIBUTE_HIDDEN


def test_a_download_waits_while_the_file_is_open_elsewhere(tmp_path: Path) -> None:
    """FR-SYNC-06: retried, not failed; the verified download is kept for the retry."""
    server = FakeFileServer()
    device = Device(tmp_path, server)
    device.write("doc.txt", b"old")
    device.sync()
    server.put("doc.txt", b"new from the server")

    with exclusively_open(device.root / "doc.txt"):
        result = device.core.cycle({""})
        assert "doc.txt" in result.retry
        assert (device.root / "doc.txt").read_bytes() == b"old"
        assert list((device.root / ".dropshare-tmp").glob("*.part"))

    device.sync()
    assert (device.root / "doc.txt").read_bytes() == b"new from the server"


def test_an_upload_waits_while_the_file_is_locked(tmp_path: Path) -> None:
    server = FakeFileServer()
    device = Device(tmp_path, server)
    device.write("budget.xlsx", b"numbers")
    device.core.connect()
    device.core.fetch()

    with exclusively_open(device.root / "budget.xlsx"):
        result = device.core.cycle({""})
        assert "budget.xlsx" in result.retry
        assert device.index.problems() == []  # waiting, not "can't sync"

    device.sync()
    assert server.tree() == {"budget.xlsx": b"numbers"}


def test_the_recycle_bin_receives_a_remote_delete(tmp_path: Path) -> None:
    """FR-SYNC-09 with the real Recycle Bin: check the Recycle Bin afterwards for keep.txt."""
    server = FakeFileServer()
    device = Device(tmp_path, server)
    if sys.platform == "win32":
        device.restart(recycle_bin=WindowsRecycleBin())
    for name in ("keep", "a", "b", "c", "d"):
        device.write(f"{name}.txt", b"x")
    device.sync()

    server.remove("keep.txt")
    device.sync()

    assert not (device.root / "keep.txt").exists()
    assert not list((device.root / ".dropshare-tmp").glob("deleted/**/keep.txt"))


@pytest.mark.parametrize("path", ["C:\\Windows\\x.txt", "..\\x.txt", "\\\\server\\share\\x.txt"])
def test_windows_path_forms_never_leave_the_sync_folder(tmp_path: Path, path: str) -> None:
    """On Windows "\\", drive letters and UNC prefixes are paths, not names (audit B3)."""
    device = Device(tmp_path, FakeFileServer())
    scanner = device.core._scanner

    with pytest.raises(UnsafePathError):
        scanner.disk_path(path)


def test_a_long_path_is_listed_not_lost(tmp_path: Path) -> None:
    """D4: past MAX_PATH the file is "can't sync", not deleted on the server."""
    from dropshare_client import platform

    server = FakeFileServer()
    device = Device(tmp_path, server, max_path=platform.max_path_length())
    deep = "/".join(["a-folder-with-a-long-name"] * 12)
    server.put("keep.txt", b"x")
    device.sync()
    for n in range(1, 13):
        server.put("/".join(["a-folder-with-a-long-name"] * n))
    server.put(f"{deep}/file.txt", b"deep")

    device.sync()

    assert server.tree()["keep.txt"] == b"x"
    assert any("too long" in p.reason for p in device.index.problems())


def test_a_rename_never_replaces_an_existing_file_on_ntfs(tmp_path: Path) -> None:
    """FR-SYNC-10: a server move applied here must refuse, not overwrite, a file in the way."""
    from dropshare_client import platform

    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")

    with pytest.raises(FileExistsError):
        platform.rename_no_replace(tmp_path / "a.txt", tmp_path / "b.txt")
    platform.rename_no_replace(tmp_path / "a.txt", tmp_path / "A.TXT")  # case only

    assert sorted(p.name for p in tmp_path.iterdir()) == ["A.TXT", "b.txt"]
    assert (tmp_path / "b.txt").read_bytes() == b"b"


def test_ntfs_file_ids_survive_a_move_and_the_move_is_sent_as_one(tmp_path: Path) -> None:
    """The real file index (st_ino via os.stat) pairs a moved file with its old path."""
    from dropshare_client.platform.common import StatFileIdentity

    server = FakeFileServer()
    device = Device(tmp_path, server)
    device.restart(file_identity=StatFileIdentity())  # what the app uses
    device.write("Docs/report.docx", b"report")
    device.sync()
    node = next(n.id for n in server.nodes.values() if n.name == "report.docx")
    server.calls.clear()

    (device.root / "Docs").rename(device.root / "Reports")
    (device.root / "Reports" / "report.docx").rename(device.root / "Reports" / "Report.docx")
    device.sync()

    assert server.tree() == {"Reports": None, "Reports/Report.docx": b"report"}
    assert next(n.id for n in server.nodes.values() if n.name == "Report.docx") == node
    assert "upload" not in server.calls and server.trashed() == []


def test_show_in_folder_starts_explorer_by_its_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never a search that could find a planted explorer.exe (audit S12). Check by eye that
    Explorer opens with the file selected (Windows check list)."""
    from dropshare_client.platform.windows import explorer

    started: list[str] = []
    monkeypatch.setattr(subprocess, "Popen", lambda command: started.append(command))
    item = tmp_path / "report.docx"
    item.write_text("x")

    explorer.reveal(item)

    [command] = started
    assert command.lower().startswith('"c:\\windows\\explorer.exe" /select,')
    assert command.endswith(f'"{item}"')


def test_junctions_are_never_followed_by_file_actions_or_uploads(tmp_path: Path) -> None:
    """is_symlink() misses NTFS junctions (audit S13)."""
    from dropshare_client.platform.macos.recycle_bin import MacTrash
    from dropshare_client.services.errors import FileActionError, FileProblem
    from dropshare_client.services.importer import Importer
    from dropshare_client.services.local_files import LocalFiles

    root = tmp_path / "DropShare"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("secret")
    _junction(root / "Junction", elsewhere)
    chosen = tmp_path / "Chosen"
    chosen.mkdir()
    (chosen / "a.txt").write_text("a")
    _junction(chosen / "Junction", elsewhere)
    files = LocalFiles(root, MacTrash(tmp_path / "trash"), lambda _: None)

    with pytest.raises(FileActionError) as raised:
        files.delete(["Junction/secret.txt"])
    plan = Importer(root, MacTrash(tmp_path / "trash")).plan([chosen], "")

    assert raised.value.problem is FileProblem.OUTSIDE
    assert plan.total == 1  # a.txt only; the junction's contents are left out


def _junction(link: Path, target: Path) -> None:
    """An NTFS junction, made as a user would (mklink /J needs no admin rights)."""
    subprocess.run(  # noqa: S603 - fixed program, paths from the test
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607 - cmd is on PATH
        check=True,
        capture_output=True,
    )


def _attributes(path: Path) -> int:
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    value: int = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    return value


def test_files_of_a_view_only_folder_get_the_read_only_attribute(tmp_path: Path) -> None:
    """FR-SHARE-05: Office and Explorer then refuse to save over them; cleared again when the
    role becomes editor."""
    file = tmp_path / "plan.docx"
    file.write_bytes(b"plan")
    attributes = WindowsFileAttributes()

    attributes.set_read_only(file, True)
    marked = _attributes(file) & _FILE_ATTRIBUTE_READONLY
    with pytest.raises(PermissionError):
        file.write_bytes(b"edit")
    attributes.set_read_only(file, False)

    assert marked and not _attributes(file) & _FILE_ATTRIBUTE_READONLY
    file.write_bytes(b"edit")


def test_a_new_version_replaces_a_read_only_file(tmp_path: Path) -> None:
    """A download clears the attribute before replacing: Windows won't replace a read-only
    file, and the new version is marked again afterwards."""
    file, part = tmp_path / "plan.docx", tmp_path / "plan.part"
    file.write_bytes(b"old")
    part.write_bytes(b"new")
    attributes = WindowsFileAttributes()
    attributes.set_read_only(file, True)

    attributes.set_read_only(file, False)
    part.replace(file)
    attributes.set_read_only(file, True)

    assert file.read_bytes() == b"new" and _attributes(file) & _FILE_ATTRIBUTE_READONLY
