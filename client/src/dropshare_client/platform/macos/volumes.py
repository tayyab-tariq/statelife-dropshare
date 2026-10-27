from pathlib import Path

from dropshare_client.platform.base import VolumeInfo, VolumeKind


class MacVolumes:
    """Development-grade: anything under /Volumes counts as removable or network."""

    def describe(self, path: Path) -> VolumeInfo:
        absolute = path.absolute()
        if absolute.parts[1:2] == ("Volumes",):
            return VolumeInfo(VolumeKind.REMOVABLE, None)
        return VolumeInfo(VolumeKind.FIXED, "apfs")

    def supported_file_systems(self) -> frozenset[str]:
        return frozenset({"apfs"})

    def other_sync_roots(self) -> list[Path]:
        home = Path.home()
        return [
            home / "Dropbox",
            home / "Library" / "CloudStorage",
            home / "Library" / "Mobile Documents",
        ]
