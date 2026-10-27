"""NTFS behaviour the engine relies on (windows-fs.md). Runs on the Windows test machine only:
macOS doesn't lock open files, so these can't be reproduced on the development Mac."""

import ctypes
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
