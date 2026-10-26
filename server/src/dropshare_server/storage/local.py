"""Filesystem backend for local development and NAS/SAN mounts (requirements §11)."""

import tempfile
from pathlib import Path


class LocalFilesystemBackend:
    def __init__(self, root: Path) -> None:
        # Never create the root: a missing NAS/SAN mount must stop startup, not fill local disk.
        if not root.is_dir():
            raise NotADirectoryError(f"Storage root does not exist or is not a directory: {root}")
        self._root = root.resolve()

    def health_check(self) -> None:
        with tempfile.NamedTemporaryFile(dir=self._root, prefix=".health-"):
            pass
