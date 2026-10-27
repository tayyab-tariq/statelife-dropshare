"""Where the sync folder may live (FR-SYNC-01, audit D2): a new or empty folder on a local
NTFS drive that holds none of the user's other files."""

from pathlib import Path

import pytest
from client_fakes import FakeVolumes

from dropshare_client.platform.base import VolumeInfo, VolumeKind
from dropshare_client.services.sync_folder import problem_with, unsafe_to_remove


class _Volumes(FakeVolumes):
    def __init__(self, root: Path, info: VolumeInfo) -> None:
        super().__init__(root)
        self._info = info

    def describe(self, path: Path) -> VolumeInfo:
        return self._info


@pytest.fixture
def app_data(tmp_path: Path) -> Path:
    return tmp_path / "app-data"


def _problem(path: Path, root: Path, app_data: Path) -> str | None:
    return problem_with(path, FakeVolumes(root), app_data)


def test_a_new_folder_on_a_local_ntfs_drive_is_fine(tmp_path: Path, app_data: Path) -> None:
    assert _problem(tmp_path / "StateLife DropShare", tmp_path, app_data) is None


def test_an_empty_folder_is_fine_even_with_os_litter(tmp_path: Path, app_data: Path) -> None:
    folder = tmp_path / "Empty"
    folder.mkdir()
    (folder / ".DS_Store").write_text("")
    (folder / "desktop.ini").write_text("")

    assert _problem(folder, tmp_path, app_data) is None


def test_a_folder_with_files_is_refused(tmp_path: Path, app_data: Path) -> None:
    folder = tmp_path / "Projects"
    folder.mkdir()
    (folder / "plan.docx").write_text("x")

    problem = _problem(folder, tmp_path, app_data)

    assert problem is not None and "empty folder" in problem


@pytest.mark.parametrize(
    ("make_path", "reason"),
    [
        (lambda tmp: Path(Path.home().anchor), "A whole drive"),
        (lambda tmp: Path.home(), "Your user folder"),
        (lambda tmp: Path.home().parent, "Your user folder"),  # an ancestor of it
        (lambda tmp: Path.home() / "Documents", "Your Documents folder"),
        (lambda tmp: Path.home() / "Desktop" / ".." / "Downloads", "Your Downloads folder"),
        (lambda tmp: tmp / "app-data" / "Sync", "own data folder"),
        (lambda tmp: tmp, "own data folder"),  # contains the app-data folder
    ],
)
def test_folders_holding_other_files_are_never_allowed(
    tmp_path: Path, app_data: Path, make_path: object, reason: str
) -> None:
    assert callable(make_path)
    path = make_path(tmp_path)

    problem = unsafe_to_remove(path, app_data)

    assert problem is not None and reason in problem


@pytest.mark.parametrize(
    ("info", "reason"),
    [
        (VolumeInfo(VolumeKind.NETWORK, None), "Network drives"),
        (VolumeInfo(VolumeKind.REMOVABLE, "NTFS"), "Removable drives"),
        (VolumeInfo(VolumeKind.UNKNOWN, None), "isn't available"),
        (VolumeInfo(VolumeKind.FIXED, "FAT32"), "must be on an NTFS drive"),
        (VolumeInfo(VolumeKind.FIXED, None), "must be on an NTFS drive"),
    ],
)
def test_unsuitable_drives_are_refused_with_a_reason(
    tmp_path: Path, app_data: Path, info: VolumeInfo, reason: str
) -> None:
    problem = problem_with(tmp_path / "Sync", _Volumes(tmp_path, info), app_data)

    assert problem is not None and reason in problem


def test_folders_inside_another_sync_tool_are_refused(tmp_path: Path, app_data: Path) -> None:
    problem = _problem(tmp_path / "OneDrive" / "Work" / "Sync", tmp_path, app_data)

    assert problem is not None and "another sync app" in problem


def test_folders_containing_another_sync_tool_are_refused(tmp_path: Path) -> None:
    parent = tmp_path / "Parent"
    volumes = FakeVolumes(parent)  # its OneDrive folder is Parent/OneDrive

    problem = problem_with(parent, volumes, tmp_path / "elsewhere")

    assert problem is not None and "contains" in problem


def test_dot_dot_is_resolved_before_comparing(tmp_path: Path, app_data: Path) -> None:
    sneaky = tmp_path / "x" / ".." / "OneDrive" / "Sync"

    problem = _problem(sneaky, tmp_path, app_data)

    assert problem is not None and "another sync app" in problem


def test_a_sibling_with_a_similar_name_is_not_inside_it(tmp_path: Path, app_data: Path) -> None:
    assert _problem(tmp_path / "OneDrive Archive", tmp_path, app_data) is None


def test_files_and_relative_paths_are_refused(tmp_path: Path, app_data: Path) -> None:
    file = tmp_path / "notes.txt"
    file.write_text("x")

    assert _problem(file, tmp_path, app_data) == "There's a file with that name. Choose a folder."
    assert _problem(Path("relative"), tmp_path, app_data) == "Choose a full folder path."
