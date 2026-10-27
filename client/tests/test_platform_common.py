"""Credential store, file identity, development profiles and the Recycle Bin (NFR-COMPAT-04)."""

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend

from dropshare_client import platform
from dropshare_client.platform.common import (
    KeyringCredentialStore,
    ProfilePaths,
    StatFileIdentity,
)
from dropshare_client.platform.macos.recycle_bin import MacTrash
from dropshare_client.platform.windows.recycle_bin import will_recycle


class _MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.items.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError("not found")


@pytest.fixture
def memory_keyring() -> Iterator[_MemoryKeyring]:
    previous = keyring.get_keyring()
    backend = _MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_credential_store_round_trip(memory_keyring: _MemoryKeyring) -> None:
    store = KeyringCredentialStore("StateLife DropShare (test)")

    store.set("token-1")
    first = store.get()
    store.set("token-2")
    second = store.get()
    store.delete()
    store.delete()  # deleting nothing is fine

    assert (first, second, store.get()) == ("token-1", "token-2", None)


def test_profiles_keep_their_credentials_apart(memory_keyring: _MemoryKeyring) -> None:
    platform.current().credentials.set("default")
    platform.current("second").credentials.set("second")

    assert platform.current().credentials.get() == "default"
    assert platform.current("second").credentials.get() == "second"


def test_profile_paths_are_separate(tmp_path: Path) -> None:
    paths = ProfilePaths(tmp_path / "AppData", tmp_path / "StateLife DropShare", "bob")

    assert paths.app_data_dir() == tmp_path / "AppData" / "profiles" / "bob"
    assert paths.default_sync_root() == tmp_path / "StateLife DropShare (bob)"


def test_file_identity_survives_a_rename(tmp_path: Path) -> None:
    original = tmp_path / "a.txt"
    original.write_text("x")
    identity = StatFileIdentity()
    before = identity.file_id(original)

    renamed = original.rename(tmp_path / "b.txt")

    assert identity.file_id(renamed) == before


def test_development_trash_keeps_both_copies_of_a_name(tmp_path: Path) -> None:
    trash = tmp_path / "trash"
    trash.mkdir()
    bin_ = MacTrash(trash)
    for content in ("first", "second"):
        folder = tmp_path / "Sync"
        folder.mkdir()
        (folder / "f.txt").write_text(content)
        bin_.move_to_recycle_bin(folder)

    kept = sorted((path / "f.txt").read_text() for path in trash.iterdir())
    assert kept == ["first", "second"]
    assert not (tmp_path / "Sync").exists()


@pytest.mark.windows_only
def test_windows_credential_manager_round_trip() -> None:
    store = KeyringCredentialStore(f"StateLife DropShare test {uuid.uuid4()}")
    try:
        store.set("secret-value")
        assert store.get() == "secret-value"
    finally:
        store.delete()
    assert store.get() is None


@pytest.mark.parametrize(("flags", "recycles"), [(0x80, True), (0x80 | 0x1, True), (0, False)])
def test_only_items_the_shell_will_recycle_may_be_deleted(flags: int, recycles: bool) -> None:
    """The progress sink's decision: anything else would be a permanent delete (audit D1)."""
    assert will_recycle(flags) is recycles


def _recycle_bin_item_count(path: Path) -> int:
    """Items in the Recycle Bin of `path`'s drive (SHQueryRecycleBinW), Windows only."""
    import ctypes

    class _Info(ctypes.Structure):
        _fields_ = (
            ("cbSize", ctypes.c_uint32),
            ("i64Size", ctypes.c_int64),
            ("i64NumItems", ctypes.c_int64),
        )

    info = _Info(cbSize=ctypes.sizeof(_Info))
    result = ctypes.windll.shell32.SHQueryRecycleBinW(str(path.anchor), ctypes.byref(info))  # type: ignore[attr-defined]  # Windows-only API
    assert result == 0
    return int(info.i64NumItems)


@pytest.mark.windows_only
def test_windows_recycle_bin_recycles_rather_than_deletes(tmp_path: Path) -> None:
    file = tmp_path / f"recycle me {uuid.uuid4().hex}.txt"
    file.write_text("x")
    folder = tmp_path / f"Folder {uuid.uuid4().hex}"
    (folder / "inner").mkdir(parents=True)
    (folder / "inner" / "f.txt").write_text("y")
    recycle_bin = platform.current().recycle_bin
    before = _recycle_bin_item_count(tmp_path)

    recycle_bin.move_to_recycle_bin(file)
    recycle_bin.move_to_recycle_bin(folder)

    assert not file.exists() and not folder.exists()
    # Both are now in the bin: recycled, not destroyed.
    assert _recycle_bin_item_count(tmp_path) == before + 2


@pytest.mark.windows_only
def test_windows_system_drive_is_fixed_ntfs() -> None:
    volumes = platform.current().volumes
    info = volumes.describe(Path(os.environ["SYSTEMDRIVE"] + "\\"))

    assert (info.kind.value, info.file_system) == ("fixed", "NTFS")
