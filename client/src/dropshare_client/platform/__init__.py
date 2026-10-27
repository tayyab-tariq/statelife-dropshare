"""Picks the OS implementation of each platform capability."""

import sys
from dataclasses import dataclass

from dropshare_client.platform.base import (
    CredentialStore,
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
    volumes: Volumes


def paths() -> Paths:
    if sys.platform == "win32":
        from dropshare_client.platform.windows.paths import WindowsPaths

        return WindowsPaths()
    if sys.platform == "darwin":
        from dropshare_client.platform.macos.paths import MacPaths

        return MacPaths()
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
    if sys.platform == "win32":
        from dropshare_client.platform.windows.recycle_bin import WindowsRecycleBin
        from dropshare_client.platform.windows.volumes import WindowsVolumes

        recycle_bin, volumes = WindowsRecycleBin(), WindowsVolumes()
    else:
        from dropshare_client.platform.macos.recycle_bin import MacTrash
        from dropshare_client.platform.macos.volumes import MacVolumes

        recycle_bin, volumes = MacTrash(), MacVolumes()
    return Platform(
        paths=selected,
        credentials=KeyringCredentialStore(service),
        recycle_bin=recycle_bin,
        file_identity=StatFileIdentity(),
        volumes=volumes,
    )
