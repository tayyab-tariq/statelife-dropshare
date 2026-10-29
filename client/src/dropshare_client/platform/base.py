"""OS capabilities the client needs, one Protocol each (NFR-COMPAT-04).

Implementations live in `windows/` (shipped) and `macos/` (development-grade only).
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class Paths(Protocol):
    def app_data_dir(self) -> Path:
        """Per-user folder for config, the local index and logs."""
        ...

    def default_sync_root(self) -> Path:
        """Suggested sync folder on first run (FR-SYNC-01)."""
        ...


class CredentialStoreError(OSError):
    """The OS credential store (Credential Manager, Keychain) couldn't be used."""


class CredentialStore(Protocol):
    """Where the refresh token lives: Windows Credential Manager, never a file (FR-AUTH-02)."""

    def get(self) -> str | None:
        """The stored secret or None; CredentialStoreError if the store can't be read."""
        ...

    def set(self, secret: str) -> None:
        """Store or replace the secret; CredentialStoreError if the store can't be written."""
        ...

    def delete(self) -> None:
        """Remove the secret; nothing stored is not an error. CredentialStoreError otherwise."""
        ...


class RecycleBin(Protocol):
    def move_to_recycle_bin(self, path: Path) -> None:
        """Move a file or folder where the user can restore it (FR-SYNC-09); raise OSError if
        that isn't possible. User files are never deleted any other way."""
        ...


class FileAttributes(Protocol):
    def hide(self, path: Path) -> None:
        """Hide DropShare's own folder inside the sync folder from Explorer; best effort."""
        ...

    def set_read_only(self, path: Path, read_only: bool) -> None:
        """Mark a file read-only, or not (view-only shared folders, FR-SHARE-05); best effort."""
        ...


@dataclass(frozen=True)
class FileId:
    """Stable identity of a file across renames on one volume, for move detection."""

    device: int
    inode: int


class FileIdentity(Protocol):
    def file_id(self, path: Path) -> FileId: ...


class VolumeKind(StrEnum):
    FIXED = "fixed"
    REMOVABLE = "removable"
    NETWORK = "network"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VolumeInfo:
    kind: VolumeKind
    file_system: str | None  # e.g. "NTFS"; None when it can't be determined


class Volumes(Protocol):
    def describe(self, path: Path) -> VolumeInfo:
        """The volume holding `path` (which may not exist yet: its nearest existing parent)."""
        ...

    def supported_file_systems(self) -> frozenset[str]:
        """File systems a sync folder may live on (FR-SYNC-01: NTFS on Windows)."""
        ...

    def other_sync_roots(self) -> list[Path]:
        """Folders owned by other sync tools (OneDrive, Dropbox, ...), to refuse nesting in."""
        ...
