"""The Administration section (FR-ADMIN-02/06/07): shown to admins only, the user list in its
four states, creating users, the one-time password, enabling and disabling, resetting
passwords, roles and storage limits. Widgets are found by accessible name."""

import threading
from pathlib import Path

from client_fakes import USER_PASSWORD, Account, Client
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QComboBox, QLabel, QLineEdit, QPushButton, QSpinBox, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import TrayState

GIB = 1024**3


def _signed_in(
    qtbot: QtBot, make_shell: MakeShell, folder: Path, client: Client, *, admin: bool = True
) -> AppShell:
    if admin:
        client.server.accounts["alice"].role = "admin"
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    folder.mkdir(exist_ok=True)
    client.config.update(sync_root=folder)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    return shell


def _open(qtbot: QtBot, shell: AppShell, rows: int) -> None:
    shell.window.show_page(messages.ADMINISTRATION)
    qtbot.waitUntil(lambda: shell.admin_view.table.model().rowCount() == rows)


def _button(find_by_name: FindByName, root: QWidget, name: str) -> QPushButton:
    button = find_by_name(root, QPushButton, name)
    assert isinstance(button, QPushButton)
    return button


def _select(shell: AppShell, username: str) -> None:
    table = shell.admin_view.table
    model = table.model()
    row = next(r for r in range(model.rowCount()) if model.index(r, 0).data() == username)
    table.selectRow(row)


def _label(shell: AppShell, name: str) -> str:
    label = shell.admin_view.findChild(QLabel, name)
    assert isinstance(label, QLabel)
    return label.text()


def _yes(title: str, text: str, action: str) -> bool:
    return True


def test_only_admins_see_administration(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    user = _signed_in(qtbot, make_shell, tmp_path / "Sync", make_client(), admin=False)

    assert not user.window.page_visible(messages.ADMINISTRATION)


def test_the_list_shows_every_account(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.server.accounts["bob"] = Account(
        password="x", disabled=True, quota_bytes=50 * GIB, used_bytes=GIB // 2
    )
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)

    assert shell.window.page_visible(messages.ADMINISTRATION)
    _open(qtbot, shell, 2)

    model = shell.admin_view.table.model()
    alice = [model.index(0, column).data() for column in range(6)]
    bob = [model.index(1, column).data() for column in range(6)]
    assert alice[:5] == ["alice", "Alice", "alice@example.com", "Administrator", "Active"]
    assert bob[3:] == ["User", "Disabled", "0.50 GB of 50 GB"]
    assert shell.admin_view.state == "content"


def test_search_filters_by_the_start_of_a_name_or_email(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.accounts["bob"] = Account(password="x")
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    _open(qtbot, shell, 2)
    search = find_by_name(shell.admin_view, QLineEdit, "Search users")
    assert isinstance(search, QLineEdit)

    search.setText("BO")
    assert shell.admin_view.table.model().rowCount() == 1
    search.setText("ob")  # not the start of anything
    assert shell.admin_view.state == "empty"
    search.clear()
    assert shell.admin_view.state == "content"


def test_loading_then_an_error_with_retry(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    gate = client.server.gates["/api/v1/admin/users"] = threading.Event()

    shell.window.show_page(messages.ADMINISTRATION)
    qtbot.waitUntil(lambda: shell.admin_view.state == "loading")
    client.server.offline = True
    gate.set()
    qtbot.waitUntil(lambda: shell.admin_view.state == "error")
    client.server.offline = False
    del client.server.gates["/api/v1/admin/users"]
    _button(find_by_name, shell.admin_view, "Try again").click()

    qtbot.waitUntil(lambda: shell.admin_view.state == "content")


def test_creating_a_user_shows_the_password_once_and_says_it_was_emailed(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    view = shell.admin_view
    _open(qtbot, shell, 1)

    _button(find_by_name, view, "New user…").click()
    dialog = view.new_user_dialog
    assert dialog is not None
    for name, text in (("Username", "bob"), ("Email", "bob@example.com"), ("Display name", "Bob")):
        field = find_by_name(dialog, QLineEdit, name)
        assert isinstance(field, QLineEdit)
        field.setText(text)
    role = find_by_name(dialog, QComboBox, "Role")
    quota = find_by_name(dialog, QSpinBox, "Storage limit")
    assert isinstance(role, QComboBox) and isinstance(quota, QSpinBox)
    assert quota.value() == 50  # NFR-SCALE-03
    role.setCurrentIndex(1)
    quota.setValue(20)
    _button(find_by_name, dialog, "Create user").click()

    qtbot.waitUntil(lambda: view.password_dialog is not None)
    password_dialog = view.password_dialog
    assert password_dialog is not None
    shown = find_by_name(password_dialog, QLineEdit, "Temporary password")
    assert isinstance(shown, QLineEdit)
    assert shown.text() == client.server.issued[0]
    assert shown.isReadOnly()
    outcome = password_dialog.findChild(QLabel, "emailOutcome")
    assert isinstance(outcome, QLabel)
    assert outcome.text() == messages.email_outcome("sent", "bob@example.com")
    assert view.new_user_dialog is None  # closed once the user exists
    bob = client.server.accounts["bob"]
    assert (bob.role, bob.quota_bytes, bob.must_change) == ("admin", 20 * GIB, True)
    qtbot.waitUntil(lambda: view.table.model().rowCount() == 2)
    assert _label(shell, "adminStatus") == messages.user_created("bob")

    _button(find_by_name, password_dialog, "Copy password").click()
    assert QGuiApplication.clipboard().text() == client.server.issued[0]
    password_dialog.reject()
    assert shown.text() == ""  # nothing of it is kept
    assert view.password_dialog is None


def test_a_failed_email_says_to_pass_the_password_on(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.accounts["bob"] = Account(password="x")
    client.server.email_outcome = "failed"
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    view = shell.admin_view
    view.confirm = _yes
    _open(qtbot, shell, 2)
    _select(shell, "bob")

    _button(find_by_name, view, "Reset password…").click()

    qtbot.waitUntil(lambda: view.password_dialog is not None)
    assert view.password_dialog is not None
    outcome = view.password_dialog.findChild(QLabel, "emailOutcome")
    assert isinstance(outcome, QLabel)
    assert "couldn't be sent" in outcome.text()
    assert client.server.accounts["bob"].password == client.server.issued[0]


def test_a_taken_username_is_shown_in_the_dialog_keeping_what_was_typed(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", make_client())
    view = shell.admin_view
    _open(qtbot, shell, 1)
    _button(find_by_name, view, "New user…").click()
    dialog = view.new_user_dialog
    assert dialog is not None
    _button(find_by_name, dialog, "Create user").click()  # nothing typed
    message = dialog.findChild(QLabel, "newUserMessage")
    assert isinstance(message, QLabel)
    assert message.text() == "Enter the username."

    for name, text in (("Username", "ALICE"), ("Email", "a2@example.com"), ("Display name", "A")):
        field = find_by_name(dialog, QLineEdit, name)
        assert isinstance(field, QLineEdit)
        field.setText(text)
    _button(find_by_name, dialog, "Create user").click()

    qtbot.waitUntil(lambda: "already taken" in message.text())
    assert view.new_user_dialog is dialog and dialog.isVisible()
    username = find_by_name(dialog, QLineEdit, "Username")
    assert isinstance(username, QLineEdit)
    assert username.text() == "ALICE"
    assert view.password_dialog is None


def test_disabling_asks_first_and_enabling_does_not(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.accounts["bob"] = Account(password="x")
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    view = shell.admin_view
    answers = [False, True]
    asked: list[str] = []

    def answer(title: str, text: str, action: str) -> bool:
        asked.append(text)
        return answers.pop(0)

    view.confirm = answer
    _open(qtbot, shell, 2)
    _select(shell, "bob")

    _button(find_by_name, view, "Disable…").click()
    assert not client.server.accounts["bob"].disabled
    _button(find_by_name, view, "Disable…").click()

    qtbot.waitUntil(lambda: client.server.accounts["bob"].disabled)
    assert asked == [messages.confirm_disable("bob")] * 2
    qtbot.waitUntil(lambda: _label(shell, "adminStatus") == messages.user_disabled("bob"))
    toggle = _button(find_by_name, view, "Disable…")
    qtbot.waitUntil(lambda: toggle.text() == "&Enable" and toggle.isEnabled())  # list reloaded
    toggle.click()

    qtbot.waitUntil(lambda: not client.server.accounts["bob"].disabled)
    assert len(asked) == 2


def test_role_and_storage_limit_changes(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.accounts["bob"] = Account(password="x", quota_bytes=50 * GIB)
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    view = shell.admin_view
    view.confirm = _yes
    view.ask_quota = lambda user: 80 * GIB
    _open(qtbot, shell, 2)
    _select(shell, "bob")

    _button(find_by_name, view, "Make administrator…").click()
    qtbot.waitUntil(lambda: client.server.accounts["bob"].role == "admin")
    qtbot.waitUntil(lambda: view.table.model().index(1, 3).data() != "User")
    change = _button(find_by_name, view, "Change storage limit…")
    qtbot.waitUntil(change.isEnabled)
    change.click()

    qtbot.waitUntil(lambda: client.server.accounts["bob"].quota_bytes == 80 * GIB)
    qtbot.waitUntil(lambda: _label(shell, "adminStatus") == messages.quota_changed("bob", 80 * GIB))


def test_changing_your_own_role_is_refused_with_the_reason(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    view = shell.admin_view
    view.confirm = _yes
    _open(qtbot, shell, 1)
    _select(shell, "alice")

    _button(find_by_name, view, "Make regular user…").click()

    qtbot.waitUntil(lambda: "your own status or role" in _label(shell, "adminError"))
    assert client.server.accounts["alice"].role == "admin"


def test_losing_the_admin_role_closes_the_section(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    _open(qtbot, shell, 1)
    client.server.accounts["alice"].role = "user"  # another admin demoted her

    _button(find_by_name, shell.admin_view, "Refresh").click()

    qtbot.waitUntil(lambda: not shell.window.page_visible(messages.ADMINISTRATION))
    assert shell.window.current_page_title() == messages.MY_FILES
    assert shell.admin_notice is not None
    assert shell.admin_notice.text() == messages.NOT_ADMIN_ANY_MORE
    assert shell.admin_view.table.model().rowCount() == 0


def test_an_answer_arriving_after_sign_out_is_dropped(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.server.accounts["bob"] = Account(password="x")
    shell = _signed_in(qtbot, make_shell, tmp_path / "Sync", client)
    gate = client.server.gates["/api/v1/admin/users"] = threading.Event()
    shell.window.show_page(messages.ADMINISTRATION)
    qtbot.waitUntil(lambda: shell.admin_view.state == "loading")

    client.admin.clear()  # signed out while the list is still on its way
    gate.set()

    qtbot.wait(200)
    assert shell.admin_view.table.model().rowCount() == 0
    assert shell.admin_view.state != "content"
