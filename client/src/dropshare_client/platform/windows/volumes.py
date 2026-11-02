"""Drive type, file system and other sync tools' folders (FR-SYNC-01).

Type-checked only when mypy runs on Windows (the bodies are unreachable for mypy elsewhere).
"""

import ctypes
import json
import os
import sys
from collections.abc import Mapping
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
        return roots + dropbox_roots(os.environ)


def dropbox_roots(environ: Mapping[str, str]) -> list[Path]:
    """Dropbox folders wherever the user put them: Dropbox lists its personal and business
    folders in info.json, in roaming or local app data. Unreadable files are skipped."""
    roots: list[Path] = []
    for variable in ("APPDATA", "LOCALAPPDATA"):
        base = environ.get(variable)
        if not base:
            continue
        try:
            info = json.loads((Path(base) / "Dropbox" / "info.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        for account in info.values():
            path = account.get("path") if isinstance(account, dict) else None
            if isinstance(path, str) and path and Path(path) not in roots:
                roots.append(Path(path))
    return roots
