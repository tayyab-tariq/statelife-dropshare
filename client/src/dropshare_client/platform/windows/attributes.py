"""File attributes on NTFS (windows-fs.md "Sync root")."""

import ctypes
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

_FILE_ATTRIBUTE_HIDDEN = 0x2
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF


class WindowsFileAttributes:
    def hide(self, path: Path) -> None:
        if sys.platform != "win32":
            return
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_attributes = kernel32.GetFileAttributesW
        get_attributes.argtypes = [wintypes.LPCWSTR]
        get_attributes.restype = wintypes.DWORD  # unsigned: INVALID_FILE_ATTRIBUTES is 2^32-1
        set_attributes = kernel32.SetFileAttributesW
        set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
        set_attributes.restype = wintypes.BOOL
        current = get_attributes(str(path))
        if current == _INVALID_FILE_ATTRIBUTES or not set_attributes(
            str(path), current | _FILE_ATTRIBUTE_HIDDEN
        ):
            logger.warning("Couldn't hide %s (error %d)", path, ctypes.get_last_error())
