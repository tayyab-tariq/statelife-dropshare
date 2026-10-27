"""Where the sync folder may live (FR-SYNC-01).

Because signing out can move the whole sync folder to the Recycle Bin (FR-AUTH-04), the folder
must be one DropShare owns outright: new or empty when chosen, and never a folder that holds
the user's other files (home, Documents, a drive root) or another sync app's folder.
"""

import os
from pathlib import Path

from dropshare_client.platform.base import VolumeKind, Volumes

# Standard per-user folders that hold the user's own files.
_SHELL_FOLDERS = ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos")
# Left by the OS in otherwise empty folders; they don't make a folder "in use".
_OS_LITTER = frozenset({".ds_store", "desktop.ini", "thumbs.db"})


def _normal(path: Path) -> Path:
    # absolute + normpath, not resolve(): resolving a network path can block, and ".." must
    # collapse before comparing.
    return Path(os.path.normpath(path.absolute()))


def _key(path: Path) -> str:
    return os.path.normcase(str(_normal(path)))  # case-insensitive on Windows


def _is_inside(path: Path, folder: Path) -> bool:
    child, parent = _key(path), _key(folder)
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def unsafe_to_remove(path: Path, app_data: Path) -> str | None:
    """Why `path` must never be the sync folder (or be moved away at sign-out), or None."""
    path = _normal(path)
    if path == Path(path.anchor):
        return "A whole drive can't be the sync folder. Choose a folder on it."
    home = _normal(Path.home())
    if _is_inside(home, path):
        return "Your user folder can't be the sync folder. Choose a new folder inside it."
    for name in _SHELL_FOLDERS:
        if _key(path) == _key(home / name):
            return f"Your {name} folder can't be the sync folder. Choose a new folder."
    if _is_inside(path, app_data) or _is_inside(app_data, path):
        return "DropShare's own data folder can't be the sync folder."
    return None


def problem_with(path: Path, volumes: Volumes, app_data: Path) -> str | None:
    """Why `path` can't be the sync folder, or None if it can. Touches the disk: run it off the
    UI thread (a network path can take seconds to answer)."""
    if not path.is_absolute():
        return "Choose a full folder path."
    path = _normal(path)
    unsafe = unsafe_to_remove(path, app_data)
    if unsafe:
        return unsafe
    if path.exists() and not path.is_dir():
        return "There's a file with that name. Choose a folder."
    if path.is_dir() and any(entry.name.lower() not in _OS_LITTER for entry in path.iterdir()):
        return "Choose an empty folder or a new one: DropShare needs a folder of its own."
    volume = volumes.describe(path)
    if volume.kind is VolumeKind.NETWORK:
        return "Network drives can't be used for the sync folder. Choose a folder on this computer."
    if volume.kind is VolumeKind.REMOVABLE:
        return (
            "Removable drives can't be used for the sync folder. Choose a folder on this computer."
        )
    if volume.kind is VolumeKind.UNKNOWN:
        return "That drive isn't available. Choose another folder."
    supported = volumes.supported_file_systems()
    if volume.file_system is None or volume.file_system.lower() not in {
        name.lower() for name in supported
    }:
        return f"The sync folder must be on an {' or '.join(sorted(supported))} drive."
    for other in volumes.other_sync_roots():
        if _is_inside(path, other):
            return (
                f"This folder is inside {other}, which another sync app manages. "
                "Choose a folder outside it."
            )
        if _is_inside(other, path):
            return f"This folder contains {other}, which another sync app manages."
    return None


def create(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
