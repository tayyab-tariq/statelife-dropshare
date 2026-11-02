"""Settings (FR-CLIENT-04): notifications, starting at Windows sign-in, files transferred at once,
about, and the signed-in computers (FR-AUTH-07) in all four states. Widgets are found by
accessible name."""

import threading
import zipfile
from pathlib import Path

import pytest
from client_fakes import USER_PASSWORD, Client, _error
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)
from pytestqt.qtbot import QtBot
from sync_fakes import NoAttributes

from dropshare_client.controllers import messages
from dropshare_client.platform.macos.recycle_bin import MacTrash
from dropshare_client.services.config import NotificationSettings
from dropshare_client.store.index import read_activity
from dropshare_client.sync import binding
from dropshare_client.sync.folder_move import FolderMover, MoveAttentionError, MoveOutcome
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.move_folder_dialog import MoveFolderDialog
from dropshare_client.ui.settings_view import SettingsView
from dropshare_client.ui.theme import TrayState


@pytest.fixture
def client(make_client: MakeClient) -> Client:
    made = make_client()
    made.auth.sign_in(made.config.config.server_url, "alice", USER_PASSWORD)
    return made


def _view(qtbot: QtBot, client: Client) -> SettingsView:
    view = SettingsView(client.settings, client.devices, client.diagnostics, client.updates)
    view.devices_card.confirm = lambda title, text, action: True
    qtbot.addWidget(view)
    view.show()
    return view


def _find[W: QWidget](find_by_name: FindByName, root: QWidget, kind: type[W], name: str) -> W:
    widget = find_by_name(root, kind, name)
    assert isinstance(widget, kind)
    return widget


def _text(view: QWidget, name: str) -> str:
    label = view.findChild(QLabel, name)
    assert isinstance(label, QLabel)
    return label.text()


# --- notifications, startup, transfers, about ---------------------------------------------


def test_notification_choices_are_shown_and_saved(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    client.config.update(notifications=NotificationSettings(quota=False))
    view = _view(qtbot, client)
    conflicts = _find(find_by_name, view, QCheckBox, "A file was changed in two places (conflicts)")
    quota = _find(find_by_name, view, QCheckBox, "My storage is almost full or full")
    assert conflicts.isChecked() and not quota.isChecked()

    conflicts.click()
    quota.click()

    assert client.config.config.notifications == NotificationSettings(conflicts=False)


def test_starting_at_sign_in_is_on_after_the_first_sign_in_and_can_be_turned_off(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    view = _view(qtbot, client)
    client.settings.apply_autostart()  # what signing in does
    qtbot.waitUntil(lambda: client.autostart.registered)
    box = _find(find_by_name, view, QCheckBox, "Start DropShare when I sign in to Windows")
    assert box.isChecked() and client.config.config.autostart is True

    box.click()

    qtbot.waitUntil(lambda: not client.autostart.registered)
    assert client.config.config.autostart is False
    client.settings.apply_autostart()  # the next sign-in keeps the choice
    qtbot.wait(50)
    assert not client.autostart.registered


def test_starting_at_sign_in_is_offered_only_in_the_installed_app(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    client.autostart.is_available = False
    view = _view(qtbot, client)
    client.settings.apply_autostart()

    box = _find(find_by_name, view, QCheckBox, "Start DropShare when I sign in to Windows")
    assert not box.isEnabled() and not box.isChecked()
    assert any(
        label.text() == messages.AUTOSTART_UNAVAILABLE and label.isVisible()
        for label in view.findChildren(QLabel)
    )
    assert client.config.config.autostart is None  # decided once it is installed


def test_a_failure_to_register_says_so(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    client.config.update(autostart=False)
    client.autostart.fail = True
    view = _view(qtbot, client)

    _find(find_by_name, view, QCheckBox, "Start DropShare when I sign in to Windows").click()

    qtbot.waitUntil(lambda: _text(view, "settingsError") == messages.AUTOSTART_FAILED)


def test_files_transferred_at_once_is_saved_and_applied(
    qtbot: QtBot, client: Client, find_by_name: FindByName, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: list[int] = []
    monkeypatch.setattr(client.sync, "set_max_transfers", applied.append)
    view = _view(qtbot, client)
    spin = _find(find_by_name, view, QSpinBox, "Files transferred at once")
    assert (spin.value(), spin.minimum(), spin.maximum()) == (4, 1, 8)

    spin.setValue(2)

    assert client.config.config.max_transfers == 2
    assert applied == [2]


def test_about_shows_the_version_and_server(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    view = _view(qtbot, client)

    assert find_by_name(view, QLabel, "Version: 0.3.0") is not None
    assert find_by_name(view, QLabel, f"Server: {client.config.config.server_url}") is not None


# --- signed-in computers (FR-AUTH-07) -----------------------------------------------------


def test_computers_are_listed_with_this_one_marked(qtbot: QtBot, client: Client) -> None:
    client.server.device("Office PC", current=True)
    client.server.device("Home laptop")
    view = _view(qtbot, client)

    qtbot.waitUntil(lambda: view.devices_card.state == "content")
    model = view.devices_card.table.model()
    assert [model.index(row, 0).data() for row in range(model.rowCount())] == [
        "Office PC (Windows 11) · This computer",
        "Home laptop (Windows 11)",
    ]


def test_computers_that_cant_be_listed_offer_try_again(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    client.server.devices_error = _error(500, "INTERNAL_ERROR", "Something went wrong.")
    client.server.device("Office PC", current=True)
    view = _view(qtbot, client)
    qtbot.waitUntil(lambda: view.devices_card.state == "error")

    client.server.devices_error = None
    _find(find_by_name, view, QPushButton, "Try again").click()

    qtbot.waitUntil(lambda: view.devices_card.state == "content")


def test_another_computer_can_be_signed_out_but_not_this_one(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    client.server.device("Office PC", current=True)
    laptop = client.server.device("Home laptop")
    view = _view(qtbot, client)
    qtbot.waitUntil(lambda: view.devices_card.state == "content")
    sign_out = _find(find_by_name, view, QPushButton, "Sign out selected computer…")

    view.devices_card.table.selectRow(0)
    assert not sign_out.isEnabled()  # Sign out (with its question about the files) does that
    view.devices_card.table.selectRow(1)
    sign_out.click()

    qtbot.waitUntil(lambda: view.devices_card.table.model().rowCount() == 1)
    assert laptop not in client.server.devices
    assert _text(view, "settingsStatus") == messages.device_signed_out("Home laptop")


def test_signing_a_computer_out_asks_first(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    client.server.device("Office PC", current=True)
    client.server.device("Home laptop")
    view = _view(qtbot, client)
    asked: list[str] = []

    def decline(title: str, text: str, action: str) -> bool:
        asked.append(text)
        return False

    view.devices_card.confirm = decline
    qtbot.waitUntil(lambda: view.devices_card.state == "content")

    view.devices_card.table.selectRow(1)
    _find(find_by_name, view, QPushButton, "Sign out selected computer…").click()
    qtbot.wait(50)

    assert asked == [messages.confirm_device_sign_out("Home laptop")]
    assert len(client.server.devices) == 2


# --- in the app ---------------------------------------------------------------------------


def test_settings_in_the_tray_opens_the_page_and_signing_in_turns_on_startup(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is not TrayState.SIGNED_OUT)
    qtbot.waitUntil(lambda: client.autostart.registered)

    shell.tray.settings.trigger()

    assert shell.window.current_page_title() == messages.SETTINGS
    assert find_account(shell.settings_view) == "Signed in as: Alice (alice@example.com)"


def find_account(view: QWidget) -> str:
    names = [label.accessibleName() for label in view.findChildren(QLabel)]
    return next(name for name in names if name.startswith("Signed in as"))


# --- moving the sync folder (FR-CLIENT-04) ------------------------------------------------


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


def _move_dialog(
    qtbot: QtBot, shell: AppShell, find_by_name: FindByName, target: Path
) -> tuple[MoveFolderDialog, QPushButton]:
    shell.window.show_page(messages.SETTINGS)
    _find(find_by_name, shell.settings_view, QPushButton, "Move…").click()
    dialog = shell.move_dialog
    assert dialog is not None
    path = _find(find_by_name, dialog, QLineEdit, "New folder")
    path.setText(str(target))
    path.editingFinished.emit()
    return dialog, _find(find_by_name, dialog, QPushButton, "Move")


def test_moving_the_folder_moves_the_files_and_syncs_from_the_new_place(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    old = tmp_path / "Sync"
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, old)
    (old / "Docs").mkdir()
    (old / "Docs" / "plan.docx").write_bytes(b"plan")
    client.engines.last.index.add_activity("uploaded", "Docs/plan.docx")
    new = tmp_path / "Elsewhere" / "DropShare"
    _, move = _move_dialog(qtbot, shell, find_by_name, new)
    qtbot.waitUntil(move.isEnabled)

    move.click()

    qtbot.waitUntil(lambda: shell.move_dialog is None)
    assert (new / "Docs" / "plan.docx").read_bytes() == b"plan"
    assert client.config.config.sync_root == new
    qtbot.waitUntil(lambda: len(client.engines.made) == 2)
    assert client.engines.last.root == new
    assert "stop" in client.engines.made[0].calls  # stopped before anything moved
    # The index came along (a reset would have forgotten everything).
    assert [a.path for a in read_activity(client.app_data / "index.db", 10)] == ["Docs/plan.docx"]
    assert _text(shell.settings_view, "settingsStatus").startswith(
        f"Your DropShare folder is now {new}."
    )
    assert find_by_name(shell.settings_view, QLabel, f"Location: {new}") is not None


def test_a_folder_that_isnt_empty_cant_be_chosen(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    _, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    busy = tmp_path / "Busy"
    busy.mkdir()
    (busy / "taxes.xlsx").write_bytes(b"theirs")
    dialog, move = _move_dialog(qtbot, shell, find_by_name, busy)

    qtbot.waitUntil(lambda: bool(_text(dialog, "moveMessage")))
    assert not move.isEnabled()
    assert "empty folder" in _text(dialog, "moveMessage")


def test_the_current_folder_or_one_inside_it_cant_be_chosen(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    old = tmp_path / "Sync"
    _, shell = _signed_in_with_folder(qtbot, make_client, make_shell, old)

    dialog, move = _move_dialog(qtbot, shell, find_by_name, old / "Inside")

    qtbot.waitUntil(lambda: bool(_text(dialog, "moveMessage")))
    assert not move.isEnabled()


# --- export diagnostics (FR-CLIENT-06) ----------------------------------------------------

_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhbGljZSJ9.c2lnbmF0dXJlLXNlY3JldA"


def test_export_diagnostics_saves_logs_and_settings_without_secrets(
    qtbot: QtBot, client: Client, find_by_name: FindByName, tmp_path: Path
) -> None:
    logs = client.app_data / "logs"
    logs.mkdir(parents=True)
    (logs / "dropshare.log").write_text(f"INFO sent Authorization: Bearer {_TOKEN}\n")
    (logs / "dropshare.log.1").write_text(f'INFO body {{"refresh_token": "{_TOKEN}"}}\n')
    (client.app_data / "index.db").write_bytes(b"names of every file")
    destination = tmp_path / "out" / "diag.zip"
    destination.parent.mkdir()
    view = _view(qtbot, client)
    view.choose_file = lambda name: str(destination)

    _find(find_by_name, view, QPushButton, "Export diagnostics…").click()

    qtbot.waitUntil(lambda: _text(view, "settingsStatus").startswith("Diagnostics saved"))
    with zipfile.ZipFile(destination) as archive:
        names = sorted(archive.namelist())
        content = b"".join(archive.read(name) for name in names)
    assert names == ["about.txt", "config.json", "logs/dropshare.log", "logs/dropshare.log.1"]
    assert _TOKEN.encode() not in content and b"[redacted]" in content
    assert b"0.3.0" in content  # the version, in about.txt
    assert [p.name for p in destination.parent.iterdir()] == ["diag.zip"]  # no temp files


def test_export_diagnostics_says_when_it_cant_save(
    qtbot: QtBot, client: Client, find_by_name: FindByName, tmp_path: Path
) -> None:
    view = _view(qtbot, client)
    view.choose_file = lambda name: str(tmp_path / "missing folder" / "diag.zip")

    _find(find_by_name, view, QPushButton, "Export diagnostics…").click()

    qtbot.waitUntil(lambda: _text(view, "settingsError") == messages.DIAGNOSTICS_FAILED)


def test_cancelling_the_save_dialog_exports_nothing(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    view = _view(qtbot, client)
    view.choose_file = lambda name: ""

    _find(find_by_name, view, QPushButton, "Export diagnostics…").click()
    qtbot.wait(50)

    assert _text(view, "settingsStatus") == "" and _text(view, "settingsError") == ""


def test_nothing_starts_syncing_while_the_folder_is_held(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, _ = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    client.sync.hold()
    client.sync.stop()
    qtbot.waitUntil(lambda: not client.sync.running)
    user = client.auth.current_user()

    client.sync.start(user)  # e.g. signed in again after a password change, mid-move
    qtbot.wait(50)
    assert len(client.engines.made) == 1

    client.sync.release()
    qtbot.waitUntil(lambda: len(client.engines.made) == 2)


def test_a_move_waits_for_the_old_engine_to_let_go(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    old = tmp_path / "Sync"
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, old)
    (old / "a.txt").write_bytes(b"a")
    client.engines.last.stops_in_time = False  # busy with a large file
    dialog, move = _move_dialog(qtbot, shell, find_by_name, tmp_path / "New")
    qtbot.waitUntil(move.isEnabled)

    move.click()

    qtbot.waitUntil(lambda: _text(dialog, "moveMessage") == messages.move_problem("busy"))
    assert (old / "a.txt").exists() and not (tmp_path / "New").exists()
    assert client.config.config.sync_root == old
    qtbot.wait(100)
    assert len(client.engines.made) == 1  # no second engine beside the one still at work

    client.engines.last.stops_in_time = True  # it finishes its step and stops

    qtbot.waitUntil(lambda: len(client.engines.made) == 2, timeout=5000)


def test_a_move_that_needs_attention_keeps_syncing_paused(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / "Sync"
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, old)

    def stuck(*args: object, **kwargs: object) -> MoveOutcome:
        (client.app_data / "folder-move.json").write_text("{}")  # the real one keeps its journal
        raise MoveAttentionError(old)

    monkeypatch.setattr(FolderMover, "move", stuck)
    _, move = _move_dialog(qtbot, shell, find_by_name, tmp_path / "New")
    qtbot.waitUntil(move.isEnabled)

    move.click()

    expected = messages.move_attention(str(old))
    qtbot.waitUntil(lambda: _text(shell.settings_view, "settingsError") == expected)
    assert shell.move_dialog is None
    client.sync.start(client.auth.current_user())
    qtbot.wait(50)
    assert len(client.engines.made) == 1  # held: the engine would take missing files as deleted

    # Sign out (which may move the folder) and another move wait, and say why.
    blocked = [
        button
        for button in shell.window.findChildren(QPushButton)
        if button.text().replace("&", "") in ("Sign out…", "Move…")
    ]
    assert len(blocked) == 3  # the header's and Settings' Sign out, and Move…
    assert all(not b.isEnabled() and b.accessibleDescription() for b in blocked)
    # An unrelated problem doesn't hide the warning; it comes after it.
    shell.settings_view.choose_file = lambda name: str(tmp_path / "missing" / "diag.zip")
    _find(find_by_name, shell.settings_view, QPushButton, "Export diagnostics…").click()
    qtbot.waitUntil(
        lambda: messages.DIAGNOSTICS_FAILED in _text(shell.settings_view, "settingsError")
    )
    assert _text(shell.settings_view, "settingsError").startswith(expected)

    client.sync.session_expired.emit("")  # the session ends: nothing of it is left for the next
    assert _text(shell.settings_view, "settingsError") == ""


def test_an_interrupted_move_is_finished_before_syncing_starts(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    old, new = tmp_path / "Sync", tmp_path / "New" / "DropShare"
    old.mkdir()
    client.config.update(sync_root=old)
    user = client.auth.current_user()
    binding.open_index(
        client.app_data / "index.db", old, client.config.config.server_url, user.id, NoAttributes()
    ).close()  # synced here before: the index knows the folder is Alice's
    (old / "Docs").mkdir()
    (old / "Docs" / "plan.docx").write_bytes(b"plan")

    def power_cut(name: str) -> None:
        if name == "move_folder.verified":
            raise RuntimeError("power cut")  # the app never got to switch

    with pytest.raises(RuntimeError):
        FolderMover(
            client.app_data / "folder-move.json",
            MacTrash(client.trash),
            lambda root: None,
            crash_point=power_cut,
        ).move(old, new, threading.Event())
    shell = make_shell(client)

    shell.start()

    qtbot.waitUntil(lambda: len(client.engines.made) == 1, timeout=2000)
    assert client.engines.last.root == new
    assert client.config.config.sync_root == new
    assert (new / "Docs" / "plan.docx").read_bytes() == b"plan"


def _mnemonics(window: QWidget) -> list[str]:
    """The Alt+letter of every shown button and buddy label in the window."""
    letters: list[str] = []
    widgets: list[QWidget] = [*window.findChildren(QAbstractButton), *window.findChildren(QLabel)]
    for widget in widgets:
        if not widget.isVisibleTo(window):
            continue
        if isinstance(widget, QLabel) and widget.buddy() is None:
            continue
        text = widget.text() if isinstance(widget, (QAbstractButton, QLabel)) else ""
        marker = text.replace("&&", "").find("&")
        if marker >= 0 and marker + 1 < len(text):
            letters.append(text.replace("&&", "")[marker + 1].lower())
    return letters


@pytest.mark.parametrize("page", [messages.SETTINGS, messages.ACTIVITY, messages.CANT_SYNC])
def test_alt_shortcuts_are_unique_on_each_page(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path, page: str
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    client.server.devices_error = _error(500, "INTERNAL_ERROR", "Something went wrong.")
    shell.window.show_page(page)
    if page == messages.SETTINGS:
        qtbot.waitUntil(
            lambda: shell.settings_view.devices_card.state == "error"
        )  # Try again shown

    letters = _mnemonics(shell.window)

    assert len(letters) == len(set(letters)), sorted(letters)


def test_enter_in_the_path_checks_it_then_moves(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in_with_folder(qtbot, make_client, make_shell, tmp_path / "Sync")
    new = tmp_path / "Elsewhere"
    shell.window.show_page(messages.SETTINGS)
    _find(find_by_name, shell.settings_view, QPushButton, "Move…").click()
    dialog = shell.move_dialog
    assert dialog is not None
    path = _find(find_by_name, dialog, QLineEdit, "New folder")
    path.setText(str(new))

    QTest.keyClick(path, Qt.Key.Key_Return)  # before any check has come back

    qtbot.waitUntil(lambda: client.config.config.sync_root == new)
    qtbot.waitUntil(lambda: shell.move_dialog is None)


def test_export_diagnostics_runs_one_at_a_time(
    qtbot: QtBot, client: Client, find_by_name: FindByName, tmp_path: Path
) -> None:
    view = _view(qtbot, client)
    view.choose_file = lambda name: str(tmp_path / "diag.zip")
    export = _find(find_by_name, view, QPushButton, "Export diagnostics…")

    export.click()

    assert not export.isEnabled()
    assert _text(view, "settingsStatus") == messages.EXPORTING_DIAGNOSTICS
    qtbot.waitUntil(export.isEnabled)
    assert _text(view, "settingsStatus").startswith("Diagnostics saved")


def test_a_preference_that_cant_be_saved_goes_back_and_says_so(
    qtbot: QtBot, client: Client, find_by_name: FindByName, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = _view(qtbot, client)
    conflicts = _find(find_by_name, view, QCheckBox, "A file was changed in two places (conflicts)")

    def disk_full(**changes: object) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(client.config, "update", disk_full)
    conflicts.click()

    assert conflicts.isChecked()  # what is saved, not what was clicked
    assert _text(view, "settingsError") == messages.SETTINGS_NOT_SAVED


def test_with_no_computers_listed_the_list_says_so(qtbot: QtBot, client: Client) -> None:
    view = _view(qtbot, client)  # the fake server lists none

    qtbot.waitUntil(lambda: view.devices_card.state == "empty")
    assert _text(view.devices_card, "empty_message") == messages.DEVICES_EMPTY
