import ctypes
import subprocess
from pathlib import Path

_MAX_PATH = 260


def _windows_folder() -> Path:
    """C:\\Windows, from the system (not %SystemRoot%, which the environment could change)."""
    buffer = ctypes.create_unicode_buffer(_MAX_PATH)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]  # windll exists on Windows only
    kernel32.GetWindowsDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
    kernel32.GetWindowsDirectoryW.restype = ctypes.c_uint
    if not kernel32.GetWindowsDirectoryW(buffer, _MAX_PATH):
        raise OSError("Couldn't find the Windows folder")
    return Path(buffer.value)


def reveal(path: Path) -> None:
    """Open Explorer at the item's folder with the item selected (FR-FILE-05). Explorer is
    started by its full path, never found by searching (a file called explorer.exe in the
    current folder must not run). Explorer parses its own command line, so the path is quoted
    as it expects; Windows names can't contain a double quote, so it can't be broken out of."""
    explorer = _windows_folder() / "explorer.exe"
    subprocess.Popen(f'"{explorer}" /select,"{path}"')  # noqa: S603 - fixed program, quoted path
