import subprocess
from pathlib import Path


def reveal(path: Path) -> None:
    """Development-grade: Finder with the item selected."""
    subprocess.Popen(["/usr/bin/open", "-R", str(path)])  # noqa: S603 - fixed program, no shell
