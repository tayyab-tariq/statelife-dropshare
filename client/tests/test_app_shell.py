"""The sign-in flow end to end through real controllers and services against a fake server
(FR-AUTH-01/02/04/05, FR-SYNC-01, FR-CLIENT-01). Widgets are found by accessible name."""

import logging
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from client_fakes import NEW_PASSWORD, USER_PASSWORD, Client
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QWidget,
)
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import TrayState

type FindByName = Callable[[QWidget, type[QWidget], str], QWidget]
type MakeClient = Callable[[], Client]
type MakeShell = Callable[..., AppShell]


@pytest.fixture
def make_shell(qtbot: QtBot) -> Iterator[MakeShell]:
    shells: list[AppShell] = []

    def make(client: Client, *, tray_available: bool = True) -> AppShell:
        shell = AppShell(client.session, client.folders, tray_available=tray_available)
        qtbot.addWidget(shell.window)
        qtbot.addWidget(shell.sign_in_dialog)
        shells.append(shell)
        return shell

    yield make
    for shell in shells:
        shell.tray.hide()


def _type(find: FindByName, root: QWidget, name: str, text: str) -> None:
    field = find(root, QLineEdit, name)
    assert isinstance(field, QLineEdit)
    field.setText(text)


def _click(qtbot: QtBot, find: FindByName, root: QWidget, name: str) -> None:
    button = find(root, QPushButton, name)
    assert isinstance(button, QPushButton)
    button.click()


def _sign_in(qtbot: QtBot, find: FindByName, shell: AppShell, login: str, password: str) -> None:
    dialog = shell.sign_in_dialog
    qtbot.waitUntil(dialog.isVisible)
    _type(find, dialog, "Username or email", login)
    _type(find, dialog, "Password", password)
    _click(qtbot, find, dialog, "Sign in")


def _message(find: FindByName, root: QWidget, object_name: str) -> str:
    """A message label's visible text (labels are found by object name: they deliberately
    have no accessible name, so screen readers read the text itself)."""
    label = root.findChild(QLabel, object_name)
    assert isinstance(label, QLabel) and label.accessibleName() == ""
    return label.text() if label.isVisible() else ""


def test_first_run_asks_to_sign_in(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    shell = make_shell(make_client())

    shell.start()

    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    assert shell.tray.state is TrayState.SIGNED_OUT


def test_wrong_password_shows_the_generic_message(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    shell = make_shell(make_client())
    shell.start()

    _sign_in(qtbot, find_by_name, shell, "alice", "wrong-password")

    dialog = shell.sign_in_dialog
    qtbot.waitUntil(lambda: bool(_message(find_by_name, dialog, "sign_in_message")))
    assert _message(find_by_name, dialog, "sign_in_message") == "Invalid username or password."
    password = find_by_name(dialog, QLineEdit, "Password")
    assert isinstance(password, QLineEdit) and password.text() == ""
    # An unknown user gets exactly the same words (FR-AUTH-01).
    _sign_in(qtbot, find_by_name, shell, "nobody", "whatever-password")
    qtbot.waitUntil(lambda: not _message(find_by_name, dialog, "sign_in_message"))
    qtbot.waitUntil(
        lambda: _message(find_by_name, dialog, "sign_in_message") == messages.INVALID_CREDENTIALS
    )


def test_disabled_account_is_told_to_contact_the_administrator(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    client.server.accounts["alice"].disabled = True
    shell = make_shell(client)
    shell.start()

    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)

    qtbot.waitUntil(
        lambda: (
            _message(find_by_name, shell.sign_in_dialog, "sign_in_message")
            == "Your account is disabled. Contact your administrator."
        )
    )
    assert client.credentials.secret is None


def test_temporary_password_must_be_changed_before_anything_else(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    client.server.accounts["alice"].must_change = True
    shell = make_shell(client)
    shell.start()

    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: shell.password_dialog is not None and shell.password_dialog.isVisible())
    dialog = shell.password_dialog
    assert dialog is not None
    _type(find_by_name, dialog, "Current password", USER_PASSWORD)
    _type(find_by_name, dialog, "New password", NEW_PASSWORD)
    _type(find_by_name, dialog, "Confirm new password", "does-not-match-at-all")
    _click(qtbot, find_by_name, dialog, "Change password")
    assert _message(find_by_name, dialog, "password_message") == "The new passwords don't match."
    _type(find_by_name, dialog, "Confirm new password", NEW_PASSWORD)
    _click(qtbot, find_by_name, dialog, "Change password")

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    assert shell.password_dialog is None
    assert client.server.accounts["alice"].password == NEW_PASSWORD
    find_by_name(shell.window, QLabel, "Signed in as: Alice (alice)")


def test_leaving_the_forced_change_signs_out(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    client.server.accounts["alice"].must_change = True
    shell = make_shell(client)
    shell.start()
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: shell.password_dialog is not None and shell.password_dialog.isVisible())
    assert shell.password_dialog is not None

    _click(qtbot, find_by_name, shell.password_dialog, "Sign out")

    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    qtbot.waitUntil(lambda: client.credentials.secret is None)
    assert client.server.refresh == {}


def test_first_sign_in_asks_for_the_sync_folder(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    shell = make_shell(client)
    shell.start()
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: shell.folder_dialog is not None and shell.folder_dialog.isVisible())
    dialog = shell.folder_dialog
    assert dialog is not None
    folder = find_by_name(dialog, QLineEdit, "Folder")
    assert isinstance(folder, QLineEdit)
    assert folder.text() == str(tmp_path / "StateLife DropShare")  # the platform default

    folder.setText(str(tmp_path / "network" / "share"))
    folder.editingFinished.emit()
    qtbot.waitUntil(lambda: "Network drives" in _message(find_by_name, dialog, "folder_message"))
    use = find_by_name(dialog, QPushButton, "Use this folder")
    assert not use.isEnabled()

    folder.setText(str(tmp_path / "My DropShare"))
    folder.editingFinished.emit()
    qtbot.waitUntil(use.isEnabled)
    _click(qtbot, find_by_name, dialog, "Use this folder")

    qtbot.waitUntil(lambda: shell.folder_dialog is None)
    assert (tmp_path / "My DropShare").is_dir()
    assert client.config.config.sync_root == tmp_path / "My DropShare"
    find_by_name(shell.window, QLabel, f"Sync folder: {tmp_path / 'My DropShare'}")


def test_relaunch_resumes_without_asking_for_the_password(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    first = make_client()
    first.auth.sign_in(first.config.config.server_url, "alice", USER_PASSWORD)
    first.config.update(sync_root=tmp_path / "Sync")

    shell = make_shell(make_client())  # a fresh process: only the credential store remains
    shell.start()

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    assert not shell.sign_in_dialog.isVisible()
    assert shell.window.view.current_state() == "content"


def test_starting_offline_keeps_the_sign_in_and_offers_retry(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    first = make_client()
    first.auth.sign_in(first.config.config.server_url, "alice", USER_PASSWORD)
    first.config.update(sync_root=Path.home())
    client = make_client()
    client.server.offline = True
    shell = make_shell(client)

    shell.start()

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.OFFLINE)
    assert shell.window.view.current_state() == "error"
    assert client.credentials.secret is not None  # not thrown away for a network problem
    client.server.offline = False
    _click(qtbot, find_by_name, shell.window, "Try again")
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)


def test_sign_out_forgets_the_credential_and_can_recycle_the_files(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client()
    sync_root = tmp_path / "Sync"
    (sync_root / "notes.txt").parent.mkdir()
    (sync_root / "notes.txt").write_text("keep me safe")
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=sync_root)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)

    # Choose "Move … to the Recycle Bin" in the confirmation, as a user would.
    def confirm(dialog: QDialog) -> int:
        move = find_by_name(dialog, QRadioButton, f"Move {sync_root} to the Recycle Bin")
        assert isinstance(move, QRadioButton)
        move.setChecked(True)
        find_by_name(dialog, QPushButton, "Sign out and move files")  # the button says so
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr("dropshare_client.ui.sign_out_dialog.SignOutDialog.exec", confirm)
    shell.window.sign_out_requested.emit()

    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    assert client.credentials.secret is None
    assert client.server.refresh == {}
    assert not sync_root.exists()
    assert [p.name for p in client.trash.iterdir()] == ["Sync"]  # recoverable, not deleted
    assert client.config.config.sync_root is None
    assert shell.tray.state is TrayState.SIGNED_OUT


def test_an_over_long_password_never_reaches_the_logs(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    shell = make_shell(make_client())
    shell.start()
    secret = "S3cretPassw0rd-" * 10

    _sign_in(qtbot, find_by_name, shell, "alice", secret)

    assert _message(find_by_name, shell.sign_in_dialog, "sign_in_message") == (
        messages.INVALID_CREDENTIALS
    )
    assert "S3cret" not in caplog.text


def test_the_forced_change_cannot_be_left_while_it_is_being_sent(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    client.server.accounts["alice"].must_change = True
    gate = client.server.gates["/api/v1/me/password"] = threading.Event()
    shell = make_shell(client)
    shell.start()
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: shell.password_dialog is not None and shell.password_dialog.isVisible())
    dialog = shell.password_dialog
    assert dialog is not None
    _type(find_by_name, dialog, "Current password", USER_PASSWORD)
    _type(find_by_name, dialog, "New password", NEW_PASSWORD)
    _type(find_by_name, dialog, "Confirm new password", NEW_PASSWORD)
    _click(qtbot, find_by_name, dialog, "Change password")
    qtbot.waitUntil(lambda: client.session.busy)

    dialog.reject()  # Escape while the change is in flight
    leave = find_by_name(dialog, QPushButton, "Sign out")

    assert dialog.isVisible() and not leave.isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    assert client.credentials.secret in client.server.refresh  # still signed in


def test_sign_in_cannot_be_cancelled_half_way(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    gate = client.server.gates["/api/v1/auth/login"] = threading.Event()
    shell = make_shell(client)
    shell.start()
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: client.session.busy)

    shell.sign_in_dialog.reject()

    assert shell.sign_in_dialog.isVisible()
    assert not find_by_name(shell.sign_in_dialog, QPushButton, "Cancel").isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)


def test_a_failed_account_load_after_a_password_change_keeps_the_change(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    client.server.accounts["alice"].must_change = True
    shell = make_shell(client)
    shell.start()
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: shell.password_dialog is not None and shell.password_dialog.isVisible())
    dialog = shell.password_dialog
    assert dialog is not None
    _type(find_by_name, dialog, "Current password", USER_PASSWORD)
    _type(find_by_name, dialog, "New password", NEW_PASSWORD)
    _type(find_by_name, dialog, "Confirm new password", NEW_PASSWORD)
    client.server.me_offline = True

    _click(qtbot, find_by_name, dialog, "Change password")

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.OFFLINE)
    assert shell.password_dialog is None  # the change is done; it isn't asked for again
    assert client.server.accounts["alice"].password == NEW_PASSWORD
    assert client.credentials.secret in client.server.refresh
    client.server.me_offline = False
    _click(qtbot, find_by_name, shell.window, "Try again")
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)


def test_unticking_the_other_server_signs_in_to_the_built_in_one(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
) -> None:
    client = make_client()
    client.config.update(server_url="http://localhost:9000")
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(shell.sign_in_dialog.isVisible)
    other = find_by_name(shell.sign_in_dialog, QCheckBox, "Use a different server")
    assert isinstance(other, QCheckBox) and other.isChecked()  # the saved override shows

    other.setChecked(False)
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    assert client.server.hosts[-1] == "127.0.0.1:8000"
    assert client.config.config.server_url == client.session.default_server_url


def test_without_a_tray_closing_the_window_quits_the_app(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quits: list[bool] = []
    monkeypatch.setattr(
        "dropshare_client.ui.app_shell.QApplication.quit", lambda: quits.append(True)
    )
    shell = make_shell(make_client(), tray_available=False)
    shell.start()

    shell.window.close()

    assert quits == [True]


def test_pause_never_hides_being_offline(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)

    shell.tray.pause.trigger()
    paused = shell.tray.state
    client.session.offline.emit(messages.OFFLINE)  # the connection drops while paused
    offline = shell.tray.state
    shell.tray.resume.trigger()

    assert (paused, offline) == (TrayState.PAUSED, TrayState.OFFLINE)
    assert shell.tray.state is TrayState.OFFLINE  # resuming doesn't claim "Up to date"


def test_sign_out_never_recycles_a_folder_dropshare_does_not_own(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    unsafe = tmp_path / "app-data"  # DropShare's own data folder, as if mis-configured
    client.config.update(sync_root=unsafe)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    notes: list[str] = []
    client.session.signed_out.connect(notes.append)

    client.session.sign_out(remove_files=True)

    qtbot.waitUntil(lambda: bool(notes))
    assert unsafe.exists()
    assert list(client.trash.iterdir()) == []
    assert "still in" in notes[0]


def test_reopening_brings_the_open_dialog_forward(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    client = make_client()
    client.server.accounts["alice"].must_change = True
    shell = make_shell(client)
    shell.start()
    _sign_in(qtbot, find_by_name, shell, "alice", USER_PASSWORD)
    qtbot.waitUntil(lambda: shell.password_dialog is not None and shell.password_dialog.isVisible())

    shell.open_app()  # tray click or a second launch

    assert not shell.sign_in_dialog.isVisible()
    assert shell.password_dialog is not None and shell.password_dialog.isVisible()
