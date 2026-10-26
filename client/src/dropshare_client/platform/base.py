"""OS capabilities the client needs, one Protocol each (NFR-COMPAT-04).

Implementations live in `windows/` (shipped) and `macos/` (development-grade only).
"""

from pathlib import Path
from typing import Protocol


class Paths(Protocol):
    def app_data_dir(self) -> Path:
        """Per-user folder for config, the local index and logs."""
        ...

    def default_sync_root(self) -> Path:
        """Suggested sync folder on first run (FR-SYNC-01)."""
        ...
