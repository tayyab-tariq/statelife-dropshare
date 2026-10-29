"""File attributes on NTFS (windows-fs.md "Sync root")."""

import ctypes
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE_ATTRIBUTE_READONLY = 0x1
_FILE_ATTRIBUTE_HIDDEN = 0x2
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


def _change(path: Path, add: int, remove: int) -> bool:
    """Set `add` and clear `remove` on the item's attributes; whether it worked."""
    if sys.platform != "win32":
        return False
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_attributes = kernel32.GetFileAttributesW
    get_attributes.argtypes = [wintypes.LPCWSTR]
    get_attributes.restype = wintypes.DWORD  # unsigned: INVALID_FILE_ATTRIBUTES is 2^32-1
    set_attributes = kernel32.SetFileAttributesW
    set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    set_attributes.restype = wintypes.BOOL
    current = get_attributes(str(path))
    if current == _INVALID_FILE_ATTRIBUTES:
        return False
    wanted = (current | add) & ~remove
    return wanted == current or bool(set_attributes(str(path), wanted))


class WindowsFileAttributes:
    def hide(self, path: Path) -> None:
        if sys.platform != "win32":
            return
        if not _change(path, _FILE_ATTRIBUTE_HIDDEN, 0):
            logger.warning("Couldn't hide %s (error %d)", path, ctypes.get_last_error())

    def set_read_only(self, path: Path, read_only: bool) -> None:
        """The read-only attribute: Office and Explorer then refuse to save over the file
        (FR-SHARE-05). Cleared again before DropShare itself replaces or moves the file."""
        if sys.platform != "win32":
            return
        add, remove = (_FILE_ATTRIBUTE_READONLY, 0) if read_only else (0, _FILE_ATTRIBUTE_READONLY)
        if not _change(path, add, remove):
            logger.warning(
                "Couldn't change read-only on %s (error %d)", path, ctypes.get_last_error()
            )
