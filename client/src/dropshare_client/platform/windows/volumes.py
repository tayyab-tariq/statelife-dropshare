"""Drive type, file system and other sync tools' folders (FR-SYNC-01).

Type-checked only when mypy runs on Windows (the bodies are unreachable for mypy elsewhere).
"""

import ctypes
import os
import sys
from pathlib import Path

from dropshare_client.platform.base import VolumeInfo, VolumeKind

_DRIVE_KINDS = {
    2: VolumeKind.REMOVABLE,  # DRIVE_REMOVABLE
    3: VolumeKind.FIXED,  # DRIVE_FIXED
    4: VolumeKind.NETWORK,  # DRIVE_REMOTE
    5: VolumeKind.REMOVABLE,  # DRIVE_CDROM
    6: VolumeKind.FIXED,  # DRIVE_RAMDISK
}
_ONEDRIVE_VARIABLES = ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
_OTHER_TOOL_FOLDERS = ("Dropbox", "Google Drive", "Box", "iCloudDrive")


class WindowsVolumes:
    def describe(self, path: Path) -> VolumeInfo:
        if sys.platform != "win32":
            raise OSError("Windows volumes can only be inspected on Windows")
        # absolute(), not resolve(): resolving a network path can block or follow links elsewhere.
        anchor = path.absolute().anchor
        if anchor.startswith("\\\\"):  # \\server\share
            return VolumeInfo(VolumeKind.NETWORK, None)
        root = anchor if anchor.endswith("\\") else anchor + "\\"
        kind = _DRIVE_KINDS.get(ctypes.windll.kernel32.GetDriveTypeW(root), VolumeKind.UNKNOWN)
        name = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            root, None, 0, None, None, None, name, len(name)
        )
        return VolumeInfo(kind, name.value if ok else None)

    def supported_file_systems(self) -> frozenset[str]:
        return frozenset({"NTFS"})

    def other_sync_roots(self) -> list[Path]:
        roots = [Path(value) for name in _ONEDRIVE_VARIABLES if (value := os.environ.get(name))]
        roots += [Path.home() / folder for folder in _OTHER_TOOL_FOLDERS]
        return roots
