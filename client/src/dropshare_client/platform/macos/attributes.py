import logging
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

_WRITE = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


class MacFileAttributes:
    """Development-grade: Finder already hides names starting with a dot."""

    def hide(self, path: Path) -> None:
        return None

    def set_read_only(self, path: Path, read_only: bool) -> None:
        """Without write permission, as close as macOS gets to the read-only attribute."""
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                return
            path.chmod((mode & ~_WRITE) if read_only else (mode | stat.S_IWUSR))
        except OSError:
            logger.warning("Couldn't change read-only on %s", path, exc_info=True)
