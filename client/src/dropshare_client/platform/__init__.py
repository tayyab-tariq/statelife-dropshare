"""Picks the OS implementation of each platform capability."""

import sys
from dataclasses import dataclass
from pathlib import Path

from dropshare_client.platform.base import (
    CredentialStore,
    FileAttributes,
    FileIdentity,
    Paths,
    RecycleBin,
    Volumes,
)
from dropshare_client.platform.common import (
    KeyringCredentialStore,
    ProfilePaths,
    StatFileIdentity,
)

_CREDENTIAL_SERVICE = "StateLife DropShare"


@dataclass(frozen=True)
class Platform:
    paths: Paths
    credentials: CredentialStore
    recycle_bin: RecycleBin
    file_identity: FileIdentity
    file_attributes: FileAttributes
    volumes: Volumes


def paths() -> Paths:
    if sys.platform == "win32":
        from dropshare_client.platform.windows.paths import WindowsPaths

        return WindowsPaths()
    if sys.platform == "darwin":
        from dropshare_client.platform.macos.paths import MacPaths

        return MacPaths()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def max_path_length() -> int | None:
    """The longest full path the client may use, or None for no limit. Windows without
    long-path support stops at MAX_PATH (260 including the terminator); paths beyond it are
    listed as "can't sync" until long paths are supported (NFR-COMPAT-03, decision D4)."""
    return 259 if sys.platform == "win32" else None


def rename_no_replace(source: Path, target: Path) -> None:
    """Rename or move within one volume; FileExistsError if `target` exists. Never replaces
    anything, so a file created at the target meanwhile can't be lost (FR-SYNC-10)."""
    if sys.platform == "win32":
        source.rename(target)  # Windows refuses an existing target (no REPLACE_EXISTING)
    elif sys.platform == "darwin":
        from dropshare_client.platform.macos.rename import rename_no_replace as mac_rename

        mac_rename(source, target)
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


def allow_foreground_handover() -> None:
    """Called by a second launch before it wakes the running instance (FR-CLIENT-07)."""
    if sys.platform == "win32":
        from dropshare_client.platform.windows.foreground import (
            allow_any_process_to_take_foreground,
        )

        allow_any_process_to_take_foreground()


def current(profile: str | None = None) -> Platform:
    """Every capability for this OS. `profile` (development only, OQ-13) keeps a second client's
    data, sync folder and stored sign-in apart from the first."""
    base = paths()
    service = _CREDENTIAL_SERVICE if profile is None else f"{_CREDENTIAL_SERVICE} ({profile})"
    selected = (
        base
        if profile is None
        else ProfilePaths(base.app_data_dir(), base.default_sync_root(), profile)
    )
    recycle_bin: RecycleBin
    volumes: Volumes
    attributes: FileAttributes
    if sys.platform == "win32":
        from dropshare_client.platform.windows.attributes import WindowsFileAttributes
        from dropshare_client.platform.windows.recycle_bin import WindowsRecycleBin
        from dropshare_client.platform.windows.volumes import WindowsVolumes

        recycle_bin, volumes = WindowsRecycleBin(), WindowsVolumes()
        attributes = WindowsFileAttributes()
    else:
        from dropshare_client.platform.macos.attributes import MacFileAttributes
        from dropshare_client.platform.macos.recycle_bin import MacTrash
        from dropshare_client.platform.macos.volumes import MacVolumes

        recycle_bin, volumes = MacTrash(), MacVolumes()
        attributes = MacFileAttributes()
    return Platform(
        paths=selected,
        credentials=KeyringCredentialStore(service),
        recycle_bin=recycle_bin,
        file_identity=StatFileIdentity(),
        file_attributes=attributes,
        volumes=volumes,
    )
