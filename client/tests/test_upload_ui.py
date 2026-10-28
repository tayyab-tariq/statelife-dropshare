"""Upload from the app (FR-FILE-02): by button and by dropping files from Explorer, into the
folder shown or the folder dropped on, with names already used, space checks and cancel."""

import threading
import uuid
from pathlib import Path

import pytest
from conftest import MakeClient, MakeShell
from PySide6.QtCore import QMimeData, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel
from pytestqt.qtbot import QtBot
from test_file_actions_ui import Setup, _setup

from dropshare_client.controllers.upload_controller import Resolution
from dropshare_client.ui.upload_dialogs import UploadProgressDialog
from dropshare_shared.schemas.users import UserOut


@pytest.fixture
def desktop(tmp_path: Path) -> Path:
    """What the user picks: notes.txt (a name already used), report.pdf, and a folder."""
    folder = tmp_path / "Desktop"
    (folder / "Album").mkdir(parents=True)
    (folder / "Album" / "a.jpg").write_bytes(b"jpeg")
    (folder / "notes.txt").write_text("new notes")
    (folder / "report.pdf").write_bytes(b"pdf")
    return folder


def _error(s: Setup) -> str:
    label = s.shell.browser_view.findChild(QLabel, "browser_error")
    assert label is not None
    return label.text() if label.isVisibleTo(s.shell.browser_view) else ""


def test_upload_files_and_a_folder_into_the_folder_shown(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, desktop: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    view = s.shell.browser_view
    view.uploads.choose_files = lambda: [desktop / "report.pdf"]
    view.uploads.choose_folder = lambda: desktop / "Album"

    view._upload_files_action.trigger()
    qtbot.waitUntil(lambda: (s.root / "report.pdf").exists())
    qtbot.waitUntil(lambda: not s.client.uploads.busy)
    view._upload_folder_action.trigger()

    qtbot.waitUntil(lambda: (s.root / "Album" / "a.jpg").exists())
    assert (s.root / "Album" / "a.jpg").read_bytes() == b"jpeg"
    qtbot.waitUntil(lambda: "Added 1 item" in s.status())
    assert {"changed report.pdf", "changed Album"} <= set(s.engine.calls)
    assert not list((s.root / ".dropshare-tmp").glob("import-*"))


def test_names_already_used_are_asked_about_keep_both_by_default(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, desktop: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    view = s.shell.browser_view
    asked: list[tuple[str, int]] = []

    def keep_both(name: str, others: int) -> tuple[Resolution, bool]:
        asked.append((name, others))
        return Resolution.KEEP_BOTH, False

    view.uploads.ask_clash = keep_both
    view.uploads.choose_files = lambda: [desktop / "notes.txt", desktop / "report.pdf"]

    view._upload_files_action.trigger()

    qtbot.waitUntil(lambda: (s.root / "notes (1).txt").exists())
    assert asked == [("notes.txt", 0)]
    assert (s.root / "notes.txt").read_text() == "notes"
    assert (s.root / "notes (1).txt").read_text() == "new notes"


@pytest.mark.parametrize(
    ("choice", "expected"), [(Resolution.REPLACE, "new notes"), (Resolution.SKIP, "notes")]
)
def test_replace_and_skip(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
    desktop: Path,
    choice: Resolution,
    expected: str,
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    view = s.shell.browser_view
    view.uploads.ask_clash = lambda name, others: (choice, False)
    view.uploads.choose_files = lambda: [desktop / "notes.txt"]

    view._upload_files_action.trigger()

    qtbot.waitUntil(lambda: not s.client.uploads.busy and bool(s.status()))
    assert (s.root / "notes.txt").read_text() == expected
    assert not (s.root / "notes (1).txt").exists()


def test_apply_to_all_answers_the_other_names_too(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, desktop: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    (desktop / "Docs").mkdir()
    (desktop / "Docs" / "d.txt").write_text("d")
    view = s.shell.browser_view
    asked: list[str] = []

    def skip_all(name: str, others: int) -> tuple[Resolution, bool]:
        asked.append(name)
        return Resolution.SKIP, True

    view.uploads.ask_clash = skip_all
    view.uploads.choose_files = lambda: [desktop / "Docs", desktop / "notes.txt"]

    view._upload_files_action.trigger()

    qtbot.waitUntil(lambda: "2 items were skipped" in s.status())
    assert asked == ["Docs"]
    assert not (s.root / "Docs" / "d.txt").exists()


def test_not_enough_dropshare_space_copies_nothing_and_says_how_much(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, desktop: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.client.server.accounts["alice"].quota_bytes = 10  # as GET /me says now
    s.client.server.accounts["alice"].used_bytes = 8
    s.shell.browser_view.uploads.choose_files = lambda: [desktop / "report.pdf", desktop / "Album"]

    s.shell.browser_view._upload_files_action.trigger()

    qtbot.waitUntil(lambda: "Nothing was copied" in _error(s))
    assert "This needs 7 bytes, but there's only 2 bytes left in your DropShare storage" in _error(
        s
    )
    assert not (s.root / "report.pdf").exists()


def test_cancelling_during_the_copy_takes_everything_back(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (64 * 1024 * 1024))
    view = s.shell.browser_view
    view.uploads.choose_files = lambda: [big]
    asked: list[bool] = []

    def confirm() -> bool:
        asked.append(True)
        return True

    view.uploads.confirm_cancel = confirm

    def press_cancel(_: int) -> None:
        dialog = view.uploads.progress
        assert dialog is not None  # shown when the copy starts
        dialog.cancel_requested.emit()

    s.client.uploads.started.connect(press_cancel)

    view._upload_files_action.trigger()

    qtbot.waitUntil(lambda: "Upload cancelled" in s.status(), timeout=10_000)
    assert asked == [True]
    assert not (s.root / "big.bin").exists()
    assert not list((s.root / ".dropshare-tmp").glob("import-*"))


def test_dropping_files_from_explorer_onto_a_folder_or_the_list(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, desktop: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(desktop / "report.pdf"))])
    docs = s.model.index(s.names().index("Docs"), 0)
    remote = s.model.index(s.names().index("remote.txt"), 0)

    assert s.model.canDropMimeData(data, Qt.DropAction.CopyAction, -1, -1, docs)
    s.model.dropMimeData(data, Qt.DropAction.CopyAction, -1, -1, docs)
    qtbot.waitUntil(lambda: (s.root / "Docs" / "report.pdf").exists())
    qtbot.waitUntil(lambda: not s.client.uploads.busy)

    s.model.dropMimeData(data, Qt.DropAction.CopyAction, -1, -1, remote)  # onto a file: here
    qtbot.waitUntil(lambda: (s.root / "report.pdf").exists())


def test_progress_over_2_gb_arrives_intact(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """A Qt int signal is 32-bit and wraps; byte counts travel as Python ints (audit B4)."""
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    received: list[object] = []
    s.client.uploads.progressed.connect(received.append)
    dialog = UploadProgressDialog()
    qtbot.addWidget(dialog)
    dialog.start(4 * 1024**3)

    s.client.uploads.progressed.emit(3 * 1024**3)
    dialog.set_done(3 * 1024**3)

    assert received == [3 * 1024**3]
    assert dialog.percent == 75


def test_escape_on_the_progress_window_asks_to_cancel_and_doesnt_just_hide_it(
    qtbot: QtBot,
) -> None:
    """Hiding it would leave the copy running with no way to stop it (audit B5)."""
    dialog = UploadProgressDialog()
    qtbot.addWidget(dialog)
    dialog.start(100)
    dialog.show()
    asked: list[bool] = []
    dialog.cancel_requested.connect(lambda: asked.append(True))

    QTest.keyClick(dialog, Qt.Key.Key_Escape)

    assert asked == [True]
    assert dialog.isVisible()


def test_files_on_a_network_share_are_refused_without_touching_them(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Opening \\\\server\\share connects to that server (audit S14)."""
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    failures: list[str] = []
    s.client.uploads.failed.connect(failures.append)

    s.client.uploads.upload([Path("//attacker.example/share/x.txt")], "")

    assert failures and "network location" in failures[0]
    assert not s.client.uploads.busy


def test_checking_a_big_upload_can_be_cancelled(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
    desktop: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the chosen folders are looked through (audit S21)."""
    from dropshare_client.services import importer

    s = _setup(qtbot, make_client, make_shell, tmp_path)
    walking = threading.Event()
    real_walk = importer._walk

    def slow_walk(path: Path, stop: threading.Event) -> int:
        walking.set()
        stop.wait(5)  # until cancelled
        return real_walk(path, stop)

    monkeypatch.setattr(importer, "_walk", slow_walk)
    cancelled: list[str] = []
    s.client.uploads.cancelled.connect(cancelled.append)

    s.client.uploads.upload([desktop / "Album"], "")
    assert walking.wait(5)
    s.client.uploads.cancel()

    qtbot.waitUntil(lambda: bool(cancelled))
    assert not (s.root / "Album").exists()


def test_without_the_server_the_quota_from_sign_in_is_used(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, desktop: Path
) -> None:
    """GET /me can't be read: the figure from sign-in, never "no limit" (audit S6)."""
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.client.server.me_offline = True
    s.client.uploads.set_user(
        UserOut(
            id=uuid.uuid4(),
            username="alice",
            email="alice@example.com",
            display_name="Alice",
            role="user",
            quota_bytes=10,
            used_bytes=8,
        )
    )
    s.shell.browser_view.uploads.choose_files = lambda: [desktop / "report.pdf"]

    s.shell.browser_view._upload_files_action.trigger()

    qtbot.waitUntil(lambda: "only 2 bytes left" in _error(s))
