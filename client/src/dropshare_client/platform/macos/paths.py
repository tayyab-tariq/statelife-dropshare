from pathlib import Path


class MacPaths:
    """Development-grade only; not a shipped macOS client (BL-08)."""

    def app_data_dir(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "StateLife" / "DropShare"

    def default_sync_root(self) -> Path:
        return Path.home() / "StateLife DropShare"
