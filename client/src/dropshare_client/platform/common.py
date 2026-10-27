"""Capabilities whose implementation is the same on every OS."""

from pathlib import Path

import keyring
import keyring.errors

from dropshare_client.platform.base import CredentialStoreError, FileId

_ACCOUNT = "refresh-token"


class KeyringCredentialStore:
    """Credential Manager on Windows, Keychain on the development Mac (both via keyring)."""

    def __init__(self, service: str) -> None:
        self._service = service

    def get(self) -> str | None:
        try:
            return keyring.get_password(self._service, _ACCOUNT)
        except keyring.errors.KeyringError as exc:
            raise CredentialStoreError(
                f"Couldn't read the sign-in from the credential store: {exc}"
            ) from exc

    def set(self, secret: str) -> None:
        try:
            keyring.set_password(self._service, _ACCOUNT, secret)
        except keyring.errors.KeyringError as exc:
            raise CredentialStoreError(
                f"Couldn't save the sign-in to the credential store: {exc}"
            ) from exc

    def delete(self) -> None:
        try:
            keyring.delete_password(self._service, _ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            pass  # nothing stored
        except keyring.errors.KeyringError as exc:
            raise CredentialStoreError(
                f"Couldn't remove the sign-in from the credential store: {exc}"
            ) from exc


class StatFileIdentity:
    """(st_dev, st_ino): on NTFS st_ino is the 64-bit file index, stable across renames."""

    def file_id(self, path: Path) -> FileId:
        stat = path.stat()
        return FileId(device=stat.st_dev, inode=stat.st_ino)


class ProfilePaths:
    """Development profiles (OQ-13): a separate app-data folder and default sync root, so two
    clients can run side by side on one machine."""

    def __init__(self, base_app_data: Path, base_sync_root: Path, profile: str) -> None:
        self._app_data = base_app_data / "profiles" / profile
        self._sync_root = base_sync_root.with_name(f"{base_sync_root.name} ({profile})")

    def app_data_dir(self) -> Path:
        return self._app_data

    def default_sync_root(self) -> Path:
        return self._sync_root
