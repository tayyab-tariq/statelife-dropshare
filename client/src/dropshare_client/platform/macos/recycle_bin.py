import shutil
from datetime import UTC, datetime
from pathlib import Path


class MacTrash:
    """Development-grade: move into ~/.Trash under a unique name (BL-08 is the real client)."""

    def __init__(self, trash_dir: Path | None = None) -> None:
        self._trash = trash_dir or Path.home() / ".Trash"

    def move_to_recycle_bin(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(str(path))
        target = self._trash / path.name
        if target.exists():
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            target = self._trash / f"{path.stem} {stamp}{path.suffix}"
        shutil.move(path, target)
