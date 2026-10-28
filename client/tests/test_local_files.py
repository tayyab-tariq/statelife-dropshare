"""The browser's actions on the sync folder (FR-FILE-03/04/05, OQ-23): names as the server
checks them, unique ignoring case, nothing overwritten, nothing outside the sync folder."""

from collections.abc import Callable
from functools import partial
from pathlib import Path

import pytest

from dropshare_client.platform.macos.recycle_bin import MacTrash
from dropshare_client.services.errors import FileActionError, FileProblem
from dropshare_client.services.local_files import LocalFiles


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "DropShare"
    (folder / "Docs").mkdir(parents=True)
    (folder / "Docs" / "plan.txt").write_text("plan")
    (folder / "notes.txt").write_text("notes")
    return folder


@pytest.fixture
def files(root: Path, tmp_path: Path) -> LocalFiles:
    trash = tmp_path / "trash"
    trash.mkdir()
    return LocalFiles(root, MacTrash(trash), lambda _: None)


def problem(action: Callable[[], object]) -> FileProblem:
    with pytest.raises(FileActionError) as raised:
        action()
    return raised.value.problem


def test_new_folder(files: LocalFiles, root: Path) -> None:
    assert files.new_folder("Docs", "2026") == "Docs/2026"
    assert files.new_folder("", "Top") == "Top"
    assert (root / "Docs" / "2026").is_dir() and (root / "Top").is_dir()


@pytest.mark.parametrize("name", ["docs", "DOCS", "Notes.TXT"])
def test_names_collide_ignoring_case(files: LocalFiles, name: str) -> None:
    assert problem(lambda: files.new_folder("", name)) is FileProblem.NAME_TAKEN


@pytest.mark.parametrize("name", ["CON", "a:b", "trailing.", "", "   "])
def test_names_the_server_would_refuse_are_refused_with_the_reason(
    files: LocalFiles, name: str
) -> None:
    with pytest.raises(FileActionError) as raised:
        files.new_folder("", name)
    assert raised.value.problem is FileProblem.BAD_NAME and raised.value.detail


def test_rename_including_a_case_only_change(files: LocalFiles, root: Path) -> None:
    assert files.rename("notes.txt", "Notes.txt") == "Notes.txt"
    assert [p.name for p in root.iterdir() if p.is_file()] == ["Notes.txt"]
    assert files.rename("Notes.txt", "summary.txt") == "summary.txt"
    assert (root / "summary.txt").read_text() == "notes"


def test_rename_never_replaces_another_item(files: LocalFiles, root: Path) -> None:
    (root / "other.txt").write_text("other")

    assert problem(lambda: files.rename("notes.txt", "OTHER.txt")) is FileProblem.NAME_TAKEN
    assert (root / "other.txt").read_text() == "other"
    assert (root / "notes.txt").read_text() == "notes"


def test_move_into_a_folder_and_not_into_itself(files: LocalFiles, root: Path) -> None:
    (root / "Docs" / "Inner").mkdir()

    assert files.move(["notes.txt"], "Docs") == ["Docs/notes.txt"]
    assert (root / "Docs" / "notes.txt").read_text() == "notes"
    for target in ("Docs", "docs/inner"):
        assert problem(partial(files.move, ["Docs"], target)) is FileProblem.INTO_ITSELF


def test_move_never_replaces_an_item_of_the_same_name(files: LocalFiles, root: Path) -> None:
    (root / "plan.txt").write_text("other plan")

    assert problem(lambda: files.move(["plan.txt"], "Docs")) is FileProblem.NAME_TAKEN
    assert (root / "Docs" / "plan.txt").read_text() == "plan"


def test_delete_goes_to_the_recycle_bin(files: LocalFiles, root: Path, tmp_path: Path) -> None:
    files.delete(["Docs"])

    assert not (root / "Docs").exists()
    assert (tmp_path / "trash" / "Docs" / "plan.txt").read_text() == "plan"


def test_save_a_copy_of_a_file_or_folder_but_never_over_one(
    files: LocalFiles, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()

    files.save_copy("notes.txt", out)
    files.save_copy("Docs", out)

    assert (out / "notes.txt").read_text() == "notes"
    assert (out / "Docs" / "plan.txt").read_text() == "plan"
    assert problem(lambda: files.save_copy("notes.txt", out)) is FileProblem.COPY_EXISTS


def test_save_a_copy_never_into_itself_or_the_sync_folder(files: LocalFiles, root: Path) -> None:
    """Into itself it would copy forever, filling the disk and the sync folder (audit B2)."""
    (root / "Docs" / "sub").mkdir()

    assert problem(lambda: files.save_copy("Docs", root / "Docs" / "sub")) is (
        FileProblem.INTO_ITSELF
    )
    assert problem(lambda: files.save_copy("notes.txt", root / "Docs")) is FileProblem.OUTSIDE
    assert sorted(p.name for p in (root / "Docs").iterdir()) == ["plan.txt", "sub"]


@pytest.mark.parametrize(
    "path", ["../outside.txt", "Docs/../../outside.txt", "", ".dropshare-tmp/x", "Docs//x"]
)
def test_paths_never_lead_out_of_the_sync_folder(files: LocalFiles, path: str) -> None:
    """Nor to the sync folder itself, nor into DropShare's own hidden folder (audit S13)."""
    assert problem(lambda: files.delete([path])) is FileProblem.OUTSIDE


def test_a_link_along_the_path_is_never_followed(
    files: LocalFiles, root: Path, tmp_path: Path
) -> None:
    """A linked folder in the sync folder leads somewhere else (audit S13)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "secret.txt").write_text("secret")
    (root / "Linked").symlink_to(elsewhere)

    assert problem(lambda: files.delete(["Linked/secret.txt"])) is FileProblem.OUTSIDE
    assert problem(lambda: files.relative(root / "Linked")) is FileProblem.OUTSIDE
    assert (elsewhere / "secret.txt").exists()


def test_move_to_must_be_a_folder_inside_dropshare(
    files: LocalFiles, root: Path, tmp_path: Path
) -> None:
    assert files.relative(root / "Docs") == "Docs"
    assert files.relative(root) == ""
    for outside in (tmp_path, root / ".dropshare-tmp", root / ".dropshare-tmp" / "x"):
        assert problem(partial(files.relative, outside)) is FileProblem.OUTSIDE


def test_an_item_gone_meanwhile_says_so(files: LocalFiles) -> None:
    assert problem(lambda: files.rename("gone.txt", "x.txt")) is FileProblem.GONE
