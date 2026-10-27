"""Whose files the sync folder holds (audit B4, decision D3): a folder with files is adopted
only when the index or the owner marker inside it says it is this account's."""

import uuid
from pathlib import Path

import pytest

from dropshare_client.platform.macos.attributes import MacFileAttributes
from dropshare_client.store.index import Index
from dropshare_client.sync import binding

SERVER = "https://dropshare.example.com"
ALICE = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
BOB = uuid.UUID("00000000-0000-0000-0000-0000000000b0")


def _open(index_path: Path, root: Path, user: uuid.UUID) -> Index:
    return binding.open_index(index_path, root, SERVER, user, MacFileAttributes())


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "DropShare"
    folder.mkdir()
    return folder


def test_an_empty_folder_is_adopted_and_marked(tmp_path: Path, root: Path) -> None:
    index = _open(tmp_path / "index.db", root, ALICE)
    index.close()

    assert (root / ".dropshare-tmp" / "owner.json").exists()


def test_another_accounts_files_are_refused(tmp_path: Path, root: Path) -> None:
    _open(tmp_path / "index.db", root, ALICE).close()
    (root / "alice's notes.txt").write_text("private")

    with pytest.raises(binding.ForeignFolderError):
        _open(tmp_path / "index.db", root, BOB)


def test_files_nobody_vouches_for_are_refused(tmp_path: Path, root: Path) -> None:
    """No index binding and no marker: e.g. the index was removed at a sign-out whose
    Recycle Bin move then failed."""
    (root / "someone's notes.txt").write_text("private")

    with pytest.raises(binding.ForeignFolderError):
        _open(tmp_path / "index.db", root, BOB)


def test_the_marker_survives_a_rebuilt_index(tmp_path: Path, root: Path) -> None:
    _open(tmp_path / "index.db", root, ALICE).close()
    (root / "notes.txt").write_text("mine")
    binding.forget(tmp_path / "index.db")  # as after a schema rebuild or a lost index

    index = _open(tmp_path / "index.db", root, ALICE)

    assert index.binding() is not None
    index.close()
    with pytest.raises(binding.ForeignFolderError):
        _open(tmp_path / "other.db", root, BOB)


def test_os_litter_doesnt_count_as_files(tmp_path: Path, root: Path) -> None:
    (root / ".DS_Store").write_bytes(b"")
    (root / "desktop.ini").write_bytes(b"")

    _open(tmp_path / "index.db", root, BOB).close()
