from pathlib import Path


class MacFileAttributes:
    """Development-grade: Finder already hides names starting with a dot."""

    def hide(self, path: Path) -> None:
        return None
