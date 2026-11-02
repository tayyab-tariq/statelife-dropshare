"""Removing DropShare's local data when it is uninstalled (FR-UPD-01, OQ-49).

The uninstaller asks, and if the user agrees it runs the app with `--remove-app-data` before
removing the app. Only what DropShare itself creates is deleted, file by file, and a folder only
once it is empty: whatever else is there (a sync folder chosen inside it, or anything the user
put there) stays. The sync folder itself is never touched.
"""

import contextlib
import logging
import re
from pathlib import Path

from dropshare_client.platform.base import CredentialStore, CredentialStoreError

logger = logging.getLogger(__name__)

_FILES = (
    "config.json",
    "index.db",
    "index.db-wal",
    "index.db-shm",
    "index.db-journal",
    "instance.lock",
    "folder-move.json",
)
# Each folder of ours, with the names of the files we write there.
_FOLDERS = {
    "logs": re.compile(r"^dropshare\.log(\.\d+)?$"),
    "updates": re.compile(r"^(DropShare-\d+\.\d+\.\d+-setup\.exe(\.part)?|pending\.json)$"),
}


def remove_app_data(app_data: Path, credentials: CredentialStore) -> None:
    """Forget the saved sign-in and delete DropShare's own files in `app_data`."""
    try:
        credentials.delete()
    except CredentialStoreError:
        logger.warning("Couldn't remove the saved sign-in", exc_info=True)
    for name in _FILES:
        _unlink(app_data / name)
    for folder, ours in _FOLDERS.items():
        path = app_data / folder
        if path.is_dir() and not path.is_symlink():
            for item in path.iterdir():
                if ours.fullmatch(item.name) and item.is_file() and not item.is_symlink():
                    _unlink(item)
            _remove_if_empty(path)
    _remove_if_empty(app_data)
    _remove_if_empty(app_data.parent)  # "StateLife", if nothing else of the company's is there


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Couldn't remove %s", path.name, exc_info=True)


def _remove_if_empty(path: Path) -> None:
    with contextlib.suppress(OSError):  # not empty, or gone: either way, leave it
        path.rmdir()
