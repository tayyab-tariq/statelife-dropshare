"""Syncing as the user sees it: started once signed in with a folder, paused from the tray,
mass deletes asked about, conflicts announced, stopped before sign-out moves the folder
(FR-SYNC-05, FR-SYNC-08, FR-CONF-01, FR-AUTH-04). The engine is a fake; its own behaviour
is covered in tests/sync."""

import threading
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from client_fakes import USER_PASSWORD, Client
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QRadioButton, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.store.index import Index
from dropshare_client.store.records import Binding
from dropshare_client.sync.engine import SyncProblem, SyncState
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import TrayState

type FindByName = Callable[[QWidget, type[QWidget], str], QWidget]
type MakeClient = Callable[[], Client]
type MakeShell = Callable[..., AppShell]


def _signed_in_with_folder(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, folder: Path
) -> tuple[Client, AppShell]:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    folder.mkdir(exist_ok=True)
    client.config.update(sync_root=folder)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    return client, shell


def test_syncing_starts_once_signed_in_with_a_folder(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, _ = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")

    (engine,) = client.engines.made
    assert engine.calls == ["start"]
    assert engine.root == tmp_path / "Sync"
    assert engine.display_name == "Alice"  # used in conflicted-copy names


def test_the_tray_pauses_and_resumes_the_engine(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")

    shell.tray.pause.trigger()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.PAUSED)
    shell.tray.resume.trigger()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)

    assert client.engines.last.calls == ["start", "pause", "resume"]


def _deletes_dialog(qtbot: QtBot, shell: AppShell) -> QDialog:
    qtbot.waitUntil(lambda: shell.delete_dialog is not None and shell.delete_dialog.isVisible())
    assert shell.delete_dialog is not None
    return shell.delete_dialog


def test_a_mass_delete_waits_for_the_user(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    engine = client.engines.last

    engine.listener.confirm_deletes(612, 0)
    engine.listener.state_changed(SyncState.NEEDS_CONFIRMATION, None)
    dialog = _deletes_dialog(qtbot, shell)

    assert "612 files deleted on the server" in dialog.accessibleDescription()
    assert "Recycle Bin" in dialog.accessibleDescription()
    assert shell.tray.state is TrayState.PAUSED  # nothing is deleted while it waits
    pause = find_by_name(dialog, QPushButton, "Pause syncing")
    assert isinstance(pause, QPushButton) and pause.isDefault()  # Enter never deletes
    delete = find_by_name(dialog, QPushButton, "Delete files")
    assert isinstance(delete, QPushButton)
    delete.click()

    qtbot.waitUntil(lambda: "confirm_deletes(True)" in engine.calls)
    assert shell.delete_dialog is None


def test_pausing_instead_of_deleting(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    engine = client.engines.last
    engine.listener.confirm_deletes(0, 3)
    dialog = _deletes_dialog(qtbot, shell)
    assert "deleted on this computer" in dialog.accessibleDescription()

    pause = find_by_name(dialog, QPushButton, "Pause syncing")
    assert isinstance(pause, QPushButton)
    pause.click()

    qtbot.waitUntil(lambda: "confirm_deletes(False)" in engine.calls)
    assert shell.tray.state is TrayState.PAUSED
    assert shell.tray.resume.isVisible()  # the way back is offered


def test_conflicts_are_announced(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(shell.tray, "notify", lambda title, text: notes.append((title, text)))

    client.engines.last.listener.conflict("Docs/plan.docx", "Docs/plan (conflicted copy).docx")

    qtbot.waitUntil(lambda: bool(notes))
    title, text = notes[0]
    assert title == "Conflicting changes"
    assert '"plan.docx"' in text and '"plan (conflicted copy).docx"' in text


def test_sign_out_stops_syncing_before_the_folder_moves(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Sync"
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, folder)
    engine = client.engines.last
    index_path = client.app_data / "index.db"
    qtbot.waitUntil(index_path.exists)
    moved_while_running: list[bool] = []
    original = client.session.sign_out

    def sign_out(remove_files: bool) -> bool:
        moved_while_running.append(engine.calls[-1] != "stop")
        return original(remove_files)

    monkeypatch.setattr(client.session, "sign_out", sign_out)

    def confirm(dialog: QDialog) -> int:
        move = find_by_name(dialog, QRadioButton, f"Move {folder} to the Recycle Bin")
        assert isinstance(move, QRadioButton)
        move.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("dropshare_client.ui.sign_out_dialog.SignOutDialog.exec", confirm)
    shell.window.sign_out_requested.emit()

    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    assert moved_while_running == [False]
    assert not folder.exists()
    assert not index_path.exists()  # the index went with the files


def test_another_accounts_files_are_never_synced(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Signed out keeping the files, then someone else signs in: their folder isn't taken."""
    folder = tmp_path / "Sync"
    folder.mkdir()
    (folder / "bob's notes.txt").write_text("not Alice's")
    client = make_client()
    index = Index(client.app_data / "index.db")
    index.reset(Binding(client.config.config.server_url, uuid.uuid4(), str(folder)))
    index.close()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=folder)
    shell = make_shell(client)

    shell.start()

    qtbot.waitUntil(lambda: shell.folder_dialog is not None and shell.folder_dialog.isVisible())
    assert client.engines.made == []
    assert client.config.config.sync_root is None
    assert (folder / "bob's notes.txt").exists()
    assert shell.tray.state is TrayState.PAUSED  # nothing syncs until a folder is chosen


def test_engine_problems_show_in_words(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")

    client.engines.last.listener.state_changed(SyncState.ERROR, SyncProblem.ROOT_MISSING)

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.ERROR)
    assert "can't be found" in shell.tray.tooltip()


def test_offline_from_the_engine_shows(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")

    client.engines.last.listener.state_changed(SyncState.OFFLINE, None)

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.OFFLINE)


def test_an_expired_session_during_sync_asks_to_sign_in(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")

    client.engines.last.listener.session_expired()

    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    assert shell.tray.state is TrayState.SIGNED_OUT
    assert client.engines.last.calls[-1] == "stop"


def test_a_second_question_never_answers_the_first(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Pause and resume while the question is open make the engine ask again; replacing
    the dialog must not approve anything (audit B2)."""
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    engine = client.engines.last
    engine.listener.confirm_deletes(30, 0)
    first = _deletes_dialog(qtbot, shell)

    engine.listener.confirm_deletes(40, 0)
    qtbot.waitUntil(lambda: shell.delete_dialog is not None and shell.delete_dialog is not first)

    assert not any(call.startswith("confirm_deletes") for call in engine.calls)


def test_return_pauses_instead_of_deleting(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    engine = client.engines.last
    engine.listener.confirm_deletes(30, 0)
    dialog = _deletes_dialog(qtbot, shell)

    QTest.keyClick(dialog, Qt.Key.Key_Return)

    qtbot.waitUntil(lambda: "confirm_deletes(False)" in engine.calls)


def test_pausing_while_syncing_starts_is_kept(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    (tmp_path / "Sync").mkdir()
    client.config.update(sync_root=tmp_path / "Sync")
    client.engines.gate = threading.Event()
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: client.sync.running)

    shell.tray.pause.trigger()  # while the engine is still being set up
    client.engines.gate.set()

    qtbot.waitUntil(lambda: bool(client.engines.made) and "pause" in client.engines.last.calls)
    assert shell.tray.state is TrayState.PAUSED


def test_reports_from_before_sign_out_are_ignored(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    old = client.engines.last
    monkeypatch.setattr(
        "dropshare_client.ui.sign_out_dialog.SignOutDialog.exec",
        lambda _: QDialog.DialogCode.Accepted,
    )
    shell.window.sign_out_requested.emit()
    qtbot.waitUntil(shell.sign_in_dialog.isVisible)

    old.listener.confirm_deletes(30, 0)  # the stopping engine's last cycle
    old.listener.state_changed(SyncState.UPGRADE_REQUIRED, None)
    qtbot.wait(50)

    assert shell.delete_dialog is None
    assert shell.tray.state is TrayState.SIGNED_OUT


def test_without_a_tray_conflicts_and_pausing_stay_reachable(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    (tmp_path / "Sync").mkdir()
    client.config.update(sync_root=tmp_path / "Sync")
    shell = make_shell(client, tray_available=False)
    shell.start()
    qtbot.waitUntil(lambda: bool(client.engines.made))
    engine = client.engines.last

    engine.listener.conflict("plan.docx", "plan (conflicted copy).docx")
    qtbot.waitUntil(
        lambda: any(
            "conflicted copy" in label.text() for label in shell.window.findChildren(QLabel)
        ),
        timeout=6000,
    )
    pause = find_by_name(shell.window, QPushButton, "Pause syncing")
    assert isinstance(pause, QPushButton)
    pause.click()

    qtbot.waitUntil(lambda: "pause" in engine.calls)
    find_by_name(shell.window, QPushButton, "Resume syncing")


def test_several_conflicts_are_one_notification(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(shell.tray, "notify", lambda title, text: notes.append((title, text)))

    for n in range(3):
        client.engines.last.listener.conflict(f"{n}.txt", f"{n} (conflicted copy).txt")

    qtbot.waitUntil(lambda: bool(notes), timeout=6000)
    qtbot.wait(50)
    assert len(notes) == 1
    assert notes[0][1].startswith("3 files were changed")


def test_a_failed_start_offers_to_try_again(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    (tmp_path / "Sync").mkdir()
    client.config.update(sync_root=tmp_path / "Sync")
    client.engines.fail_with = PermissionError("index locked")
    shell = make_shell(client)
    shell.start()

    qtbot.waitUntil(lambda: shell.window.view.current_state() == "error")
    assert shell.tray.state is TrayState.ERROR
    client.engines.fail_with = None
    try_again = find_by_name(shell.window, QPushButton, "Try again")
    assert isinstance(try_again, QPushButton)
    try_again.click()

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)


def test_the_question_is_announced_when_the_app_is_in_the_tray(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    shell.window.hide()
    notes: list[str] = []
    monkeypatch.setattr(shell.tray, "notify", lambda title, _: notes.append(title))

    client.engines.last.listener.confirm_deletes(30, 0)
    dialog = _deletes_dialog(qtbot, shell)
    dialog.hide()
    shell.open_app()  # a tray click brings the question back, not just the window

    assert notes == ["Confirm deleting files"]
    assert dialog.isVisible()


def test_a_failed_recycle_at_sign_out_keeps_the_record_of_whose_files_these_are(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D3: if the files stay, so does the index that says they are alice's."""
    folder = tmp_path / "Sync"
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, folder)
    (folder / "alice.txt").write_text("hers")
    index_path = client.app_data / "index.db"

    def refuse(self: object, path: Path) -> None:
        raise OSError("a file is open in Word")

    monkeypatch.setattr(
        "dropshare_client.platform.macos.recycle_bin.MacTrash.move_to_recycle_bin", refuse
    )

    def confirm(dialog: QDialog) -> int:
        move = find_by_name(dialog, QRadioButton, f"Move {folder} to the Recycle Bin")
        assert isinstance(move, QRadioButton)
        move.setChecked(True)
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("dropshare_client.ui.sign_out_dialog.SignOutDialog.exec", confirm)
    shell.window.sign_out_requested.emit()
    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    qtbot.wait(50)

    assert (folder / "alice.txt").exists()
    assert index_path.exists()


def test_syncing_shows_how_many_items_are_left(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """FR-CLIENT-01: "Syncing (n items)"."""
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    listener = client.engines.last.listener

    listener.state_changed(SyncState.SYNCING, None)
    listener.progress(12)

    qtbot.waitUntil(lambda: "Syncing (12 items)" in shell.tray.tooltip())
