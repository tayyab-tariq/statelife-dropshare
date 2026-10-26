import os
from pathlib import Path


class WindowsPaths:
    def app_data_dir(self) -> Path:
        # %LOCALAPPDATA%\StateLife\DropShare (FR-CLIENT-06)
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("LOCALAPPDATA is not set")
        return Path(local_app_data) / "StateLife" / "DropShare"

    def default_sync_root(self) -> Path:
        # %USERPROFILE%\StateLife DropShare (FR-SYNC-01)
        return Path.home() / "StateLife DropShare"
