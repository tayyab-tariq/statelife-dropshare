"""Upload by copying into the sync folder (FR-FILE-02, OQ-23, OQ-26, OQ-28): names, clashes,
space checks, and cancel taking back exactly what the upload did — and nothing else."""

import os
import shutil
import threading
import unicodedata
from collections.abc import Callable
from pathlib import Path

import pytest

from dropshare_client.platform.macos.recycle_bin import MacTrash
from dropshare_client.services.errors import FileActionError, FileProblem, TransferCancelledError
from dropshare_client.services.importer import (
    Importer,
    ImportTakenBackError,
    NotEnoughSpaceError,
    Resolution,
)

KEEP, REPLACE, SKIP = Resolution.KEEP_BOTH, Resolution.REPLACE, Resolution.SKIP


def tree(folder: Path) -> dict[str, str | None]:
    """Everything below `folder` except DropShare's own temp folder."""
    result: dict[str, str | None] = {}
    for path in sorted(folder.rglob("*")):
        relative = path.relative_to(folder).as_posix()
        if relative.startswith(".dropshare-tmp"):
            continue
        result[relative] = None if path.is_dir() else path.read_text()
    return result


def leftovers(root: Path) -> list[str]:
    """What an import left in DropShare's temp folder (should be nothing)."""
    temp = root / ".dropshare-tmp"
    return sorted(p.relative_to(temp).as_posix() for p in temp.rglob("*")) if temp.exists() else []


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "DropShare"
    (folder / "Reports").mkdir(parents=True)
    (folder / "Reports" / "q1.txt").write_text("old q1")
    (folder / "plan.txt").write_text("old plan")
    return folder


@pytest.fixture
def trash(tmp_path: Path) -> Path:
    folder = tmp_path / "trash"
    folder.mkdir()
    return folder


@pytest.fixture
def importer(root: Path, trash: Path) -> Importer:
    return Importer(root, MacTrash(trash))


@pytest.fixture
def chosen(tmp_path: Path) -> Path:
    """What the user picked in Explorer."""
    folder = tmp_path / "Desktop"
    (folder / "Reports" / "deep").mkdir(parents=True)
    (folder / "Reports" / "q1.txt").write_text("new q1")
    (folder / "Reports" / "q2.txt").write_text("new q2")
    (folder / "Reports" / "deep" / "d.txt").write_text("deep")
    (folder / "plan.txt").write_text("new plan")
    (folder / "fresh.txt").write_text("fresh")
    return folder


def _run(
    importer: Importer,
    sources: list[Path],
    resolutions: dict[str, Resolution],
    progress: Callable[[int], None] = lambda _: None,
    stop: threading.Event | None = None,
) -> list[str]:
    plan = importer.plan(sources, "")
    return importer.run(plan, resolutions, stop or threading.Event(), progress).changed


def test_new_items_are_copied_and_clashes_listed(importer: Importer, chosen: Path) -> None:
    plan = importer.plan([chosen / "fresh.txt", chosen / "plan.txt", chosen / "Reports"], "")

    assert plan.clashes == ["plan.txt", "Reports"]
    assert plan.total == len("fresh") + len("new plan") + len("new q1new q2deep")


def test_keep_both_adds_a_number_to_the_new_copy(
    importer: Importer, chosen: Path, root: Path
) -> None:
    changed = _run(importer, [chosen / "plan.txt", chosen / "Reports"], {})

    assert changed == ["plan (1).txt", "Reports (1)"]
    assert tree(root)["plan.txt"] == "old plan"
    assert tree(root)["plan (1).txt"] == "new plan"
    assert tree(root)["Reports (1)/deep/d.txt"] == "deep"
    assert tree(root)["Reports/q1.txt"] == "old q1"

    _run(importer, [chosen / "plan.txt"], {})
    assert tree(root)["plan (2).txt"] == "new plan"
    assert leftovers(root) == []


def test_replace_replaces_files_and_merges_folders(
    importer: Importer, chosen: Path, root: Path, trash: Path
) -> None:
    (root / "Reports" / "mine.txt").write_text("only here")

    _run(
        importer,
        [chosen / "plan.txt", chosen / "Reports"],
        {"plan.txt": REPLACE, "Reports": REPLACE},
    )

    assert tree(root) == {
        "Reports": None,
        "Reports/deep": None,
        "Reports/deep/d.txt": "deep",
        "Reports/mine.txt": "only here",  # merged, not replaced (OQ-26)
        "Reports/q1.txt": "new q1",
        "Reports/q2.txt": "new q2",
        "plan.txt": "new plan",
    }
    # The replaced versions can be restored, and are recognisable by name (audit S3).
    recycled = sorted((p.name, p.read_text()) for p in trash.rglob("*") if p.is_file())
    assert recycled == [("plan.txt", "old plan"), ("q1.txt", "old q1")]
    assert leftovers(root) == []


def test_replace_never_leaves_the_path_empty(
    importer: Importer, chosen: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine could scan at any moment: the original stays in place until the new
    version takes its place in one step (audit S2)."""
    seen: list[str] = []
    real = Path.replace

    def watched(self: Path, target: str | os.PathLike[str]) -> Path:
        seen.append((root / "plan.txt").read_text())  # still there, right before the swap
        return real(self, target)

    monkeypatch.setattr(Path, "replace", watched)

    _run(importer, [chosen / "plan.txt"], {"plan.txt": REPLACE})

    assert seen == ["old plan"]
    assert (root / "plan.txt").read_text() == "new plan"


def test_skip_leaves_it_alone(importer: Importer, chosen: Path, root: Path) -> None:
    before = tree(root)

    assert _run(importer, [chosen / "plan.txt"], {"plan.txt": SKIP}) == []
    assert tree(root) == before


def test_merging_matches_names_ignoring_case_and_unicode_form(
    importer: Importer, root: Path, tmp_path: Path
) -> None:
    """NFD "é" from one place meets NFC "é" in the sync folder: one file, replaced (N1)."""
    (root / "Reports" / unicodedata.normalize("NFC", "café.txt")).write_text("old")
    source = tmp_path / "In" / "Reports"
    source.mkdir(parents=True)
    (source / unicodedata.normalize("NFD", "CAFÉ.txt")).write_text("new")

    _run(importer, [source], {"Reports": REPLACE})

    names = [p.name for p in (root / "Reports").iterdir() if p.name.casefold().startswith("caf")]
    assert len(names) == 1
    assert (root / "Reports" / names[0]).read_text() == "new"


def test_cancel_takes_back_everything_and_restores_replaced_files(
    importer: Importer, chosen: Path, root: Path, trash: Path
) -> None:
    (root / "Reports" / "mine.txt").write_text("only here")
    before = tree(root)
    stop = threading.Event()
    copied: list[int] = []

    def progress(done: int) -> None:
        copied.append(done)
        if len(copied) == 3:  # part way through the merged folder
            stop.set()

    with pytest.raises(ImportTakenBackError) as raised:
        _run(
            importer,
            [chosen / "fresh.txt", chosen / "Reports"],
            {"Reports": REPLACE},
            progress,
            stop,
        )

    assert raised.value.cancelled and raised.value.kept == []
    assert tree(root) == before
    assert not list(trash.iterdir())
    assert leftovers(root) == []


def test_a_failure_part_way_takes_everything_back_too(
    importer: Importer, chosen: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = tree(root)
    real = Path.open

    def failing_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.name == "plan.txt" and self.parent == chosen:
            raise PermissionError(13, "in use")
        return real(self, *args, **kwargs)  # type: ignore[call-overload]  # passes through

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(ImportTakenBackError) as raised:
        _run(importer, [chosen / "fresh.txt", chosen / "plan.txt"], {"plan.txt": REPLACE})

    assert not raised.value.cancelled and isinstance(raised.value.__cause__, PermissionError)
    assert tree(root) == before


def test_a_restore_that_fails_keeps_the_original_never_deletes_it(
    importer: Importer, chosen: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows: the engine has the new copy open when Cancel comes, so neither removing it
    nor putting the original over it works. The original must survive (audit B1)."""
    stop = threading.Event()
    real_replace = Path.replace
    swaps = [0]

    def replace_once_then_locked(self: Path, target: str | os.PathLike[str]) -> Path:
        swaps[0] += 1
        if swaps[0] > 1:  # the take-back's attempt: the copy is open elsewhere
            raise PermissionError(32, "in use")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace_once_then_locked)

    with pytest.raises(ImportTakenBackError) as raised:
        _run(
            importer,
            [chosen / "plan.txt", chosen / "fresh.txt"],
            {"plan.txt": REPLACE},
            lambda done: stop.set() if done >= len("new plan") else None,
            stop,
        )

    contents = {p.read_text() for p in root.rglob("*") if p.is_file()}
    assert "old plan" in contents  # kept, not deleted
    assert raised.value.kept  # and reported
    assert "fresh" not in contents  # the rest was taken back


def test_cancel_never_removes_a_file_that_changed_after_it_was_placed(
    importer: Importer, chosen: Path, root: Path
) -> None:
    """Another device's file synced in at that name, or the user's own edit: it stays
    (audit S1)."""
    stop = threading.Event()

    def edit_then_cancel(done: int) -> None:
        placed = root / "fresh.txt"
        if placed.exists() and not stop.is_set():
            placed.write_text("edited meanwhile")
            stop.set()

    with pytest.raises(ImportTakenBackError) as raised:
        _run(
            importer,
            [chosen / "fresh.txt", chosen / "Reports"],
            {"Reports": REPLACE},
            edit_then_cancel,
            stop,
        )

    assert (root / "fresh.txt").read_text() == "edited meanwhile"
    assert root / "fresh.txt" in raised.value.kept


def test_the_same_name_twice_is_refused_before_anything_is_copied(
    importer: Importer, tmp_path: Path, root: Path
) -> None:
    """Two "report.pdf" from different places would replace each other (audit B1)."""
    for folder in ("a", "b"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "report.pdf").write_text(folder)
    before = tree(root)

    with pytest.raises(FileActionError) as raised:
        importer.plan([tmp_path / "a" / "report.pdf", tmp_path / "b" / "REPORT.pdf"], "")

    assert raised.value.problem is FileProblem.DUPLICATE_SOURCE
    assert tree(root) == before


def test_a_bad_name_inside_a_folder_is_refused_when_planning(
    importer: Importer, chosen: Path, root: Path
) -> None:
    """Not after gigabytes were copied (audit S7)."""
    (chosen / "Reports" / "deep" / "CON.txt").write_text("x")

    with pytest.raises(FileActionError) as raised:
        importer.plan([chosen / "Reports"], "")

    assert (raised.value.problem, raised.value.name) == (FileProblem.BAD_NAME, "CON.txt")


def test_links_inside_a_chosen_folder_are_left_out(
    importer: Importer, chosen: Path, root: Path, tmp_path: Path
) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "s.txt").write_text("secret")
    (chosen / "Reports" / "shortcut").symlink_to(secret)

    plan = importer.plan([chosen / "Reports"], "")
    importer.run(plan, {"Reports": KEEP}, threading.Event(), lambda _: None)

    assert plan.total == len("new q1new q2deep")
    assert not (root / "Reports (1)" / "shortcut").exists()


def test_planning_can_be_cancelled(importer: Importer, chosen: Path) -> None:
    stop = threading.Event()
    stop.set()

    with pytest.raises(TransferCancelledError):
        importer.plan([chosen / "Reports"], "", stop)


def test_a_half_copied_file_is_never_in_the_sync_folder(
    importer: Importer, chosen: Path, root: Path
) -> None:
    big = chosen / "big.bin"
    big.write_bytes(b"x" * (3 * 1024 * 1024))
    seen: list[bool] = []

    _run(importer, [big], {}, lambda _: seen.append((root / "big.bin").exists()))

    assert seen and not any(seen)
    assert (root / "big.bin").stat().st_size == 3 * 1024 * 1024


def test_not_enough_room_copies_nothing_and_says_how_much(importer: Importer, chosen: Path) -> None:
    plan = importer.plan([chosen / "fresh.txt"], "")

    with pytest.raises(NotEnoughSpaceError) as raised:
        importer.check_space(plan, quota_left=2)

    assert (raised.value.needed, raised.value.available, raised.value.in_dropshare) == (5, 2, True)
    importer.check_space(plan, quota_left=None)  # disk has room; quota unknown


def test_not_enough_disk_space(
    importer: Importer, chosen: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = importer.plan([chosen / "fresh.txt"], "")
    monkeypatch.setattr(shutil, "disk_usage", lambda _: type("Usage", (), {"free": 3})())

    with pytest.raises(NotEnoughSpaceError) as raised:
        importer.check_space(plan, quota_left=None)

    assert (raised.value.available, raised.value.in_dropshare) == (3, False)


def test_names_the_server_refuses_and_uploads_into_itself_are_refused(
    importer: Importer, root: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "CON.txt"
    bad.write_text("x")

    with pytest.raises(FileActionError) as bad_name:
        importer.plan([bad], "")
    with pytest.raises(FileActionError) as into_itself:
        importer.plan([root / "Reports"], "Reports")

    assert bad_name.value.problem is FileProblem.BAD_NAME
    assert into_itself.value.problem is FileProblem.INTO_ITSELF


def test_upload_into_a_subfolder(importer: Importer, chosen: Path, root: Path) -> None:
    plan = importer.plan([chosen / "fresh.txt"], "Reports")

    result = importer.run(plan, {}, threading.Event(), lambda _: None)

    assert result.changed == ["Reports/fresh.txt"]
    assert (root / "Reports" / "fresh.txt").read_text() == "fresh"
