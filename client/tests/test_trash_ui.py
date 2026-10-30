"""The Trash in the app (FR-TRASH-01 … 03): listing, restoring, deleting for good and emptying,
in all four view states. Widgets are found by accessible name."""

import threading
from pathlib import Path

from client_fakes import USER_PASSWORD, Client, _error
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QLabel, QPushButton, QTableView, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.services.trash import Trash
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import TrayState


def _signed_in(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, folder: Path, client: Client
) -> AppShell:
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    folder.mkdir(exist_ok=True)
    client.config.update(sync_root=folder)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    return shell


def _open_trash(qtbot: QtBot, shell: AppShell, rows: int) -> QTableView:
    shell.window.show_page(messages.TRASH)
    table = shell.trash_view.table
    qtbot.waitUntil(lambda: table.model().rowCount() == rows)
    return table


def _button(find_by_name: FindByName, root: QWidget, name: str) -> QPushButton:
    button = find_by_name(root, QPushButton, name)
    assert isinstance(button, QPushButton)
    return button


def _status(shell: AppShell) -> str:
    label = shell.trash_view.findChild(QLabel, "trashStatus")
    assert isinstance(label, QLabel)
    return label.text()


def _error_text(shell: AppShell) -> str:
    label = shell.trash_view.findChild(QLabel, "trashError")
    assert isinstance(label, QLabel)
    return label.text()


def test_the_trash_lists_what_was_deleted_and_restores_it(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.trash_item("report.docx", size=2048)
    client.server.trash_item("plan.txt", can_delete=False)
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)

    table = _open_trash(qtbot, shell, 2)
    model = table.model()

    assert [model.index(0, c).data() for c in range(5)][:2] == ["report.docx", "My files / Docs"]
    assert model.index(1, 1).data() == "Budget / Docs"
    assert model.index(1, 3).data() == "Bob"
    assert model.index(0, 4).data() == "2.0 KB"
    table.selectRow(0)
    _button(find_by_name, shell.trash_view, "Restore").click()

    qtbot.waitUntil(lambda: model.rowCount() == 1)
    assert _status(shell) == messages.restored(1)
    assert [n.name for n in client.server.children[client.server.root_id]] == ["report.docx"]


def test_the_trash_is_empty_until_something_is_deleted(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", make_client())

    shell.window.show_page(messages.TRASH)

    qtbot.waitUntil(lambda: shell.trash_view.state == "empty")


def test_loading_then_an_error_with_retry(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.trash_item("report.docx")
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    gate = client.server.gates["/api/v1/trash"] = threading.Event()

    shell.window.show_page(messages.TRASH)
    qtbot.waitUntil(lambda: shell.trash_view.state == "loading")
    client.server.offline = True
    gate.set()
    qtbot.waitUntil(lambda: shell.trash_view.state == "error")
    client.server.offline = False
    del client.server.gates["/api/v1/trash"]
    _button(find_by_name, shell.trash_view, "Try again").click()

    qtbot.waitUntil(lambda: shell.trash_view.table.model().rowCount() == 1)
    assert shell.trash_view.state == "content"


def test_deleting_permanently_asks_first_and_only_owners_can(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.trash_item("report.docx")
    client.server.trash_item("plan.txt", can_delete=False)
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    view = shell.trash_view
    table = _open_trash(qtbot, shell, 2)
    delete = _button(find_by_name, view, "Delete permanently…")
    answers = [False, True]
    asked: list[str] = []

    def answer(title: str, text: str, action: str) -> bool:
        asked.append(text)
        return answers.pop(0)

    view.confirm = answer

    [shortcut] = [
        a for a in table.actions() if a.shortcut() == QKeySequence(QKeySequence.StandardKey.Delete)
    ]
    table.selectRow(1)
    assert not delete.isEnabled()
    assert delete.toolTip() == delete.accessibleDescription() == messages.ONLY_OWNER_DELETES
    shortcut.trigger()  # the Delete key on someone else's item says why, rather than nothing
    assert _status(shell) == messages.ONLY_OWNER_DELETES_SELECTED
    table.selectRow(0)
    delete.click()  # declined
    assert table.model().rowCount() == 2
    shortcut.trigger()  # the Delete key

    qtbot.waitUntil(lambda: table.model().rowCount() == 1)
    assert asked == [messages.confirm_delete_forever(["report.docx"])] * 2
    assert _status(shell) == messages.deleted_forever(1)
    assert [i.name for i in client.server.trash] == ["plan.txt"]


def test_emptying_the_trash_keeps_what_others_own(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.trash_item("report.docx", size=1500)
    client.server.trash_item("plan.txt", can_delete=False)
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    view = shell.trash_view
    table = _open_trash(qtbot, shell, 2)
    view.confirm = lambda title, text, action: action == "Empty trash"

    _button(find_by_name, view, "Empty trash…").click()

    qtbot.waitUntil(lambda: table.model().rowCount() == 1)
    assert _status(shell) == messages.trash_emptied(1, 1500)
    assert not _button(find_by_name, view, "Empty trash…").isEnabled()  # nothing of yours left


def test_restoring_several_says_which_couldnt_be_done(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.trash_item("a.txt")
    gone = client.server.trash_item("b.txt")
    client.server.trash_errors[gone.id] = _error(404, "NOT_FOUND", "Item not found.")
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    table = _open_trash(qtbot, shell, 2)

    table.selectAll()
    _button(find_by_name, shell.trash_view, "Restore").click()

    qtbot.waitUntil(lambda: bool(_error_text(shell)))
    assert _error_text(shell) == messages.some_failed(1, messages.NOT_IN_TRASH)
    assert _status(shell) == messages.restored(1)


def test_enter_or_double_click_restores_that_row_only(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.server.trash_item("a.txt")
    client.server.trash_item("b.txt")
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    table = _open_trash(qtbot, shell, 2)

    table.selectAll()
    table.activated.emit(table.model().index(1, 0))

    qtbot.waitUntil(lambda: table.model().rowCount() == 1)
    assert [i.name for i in client.server.trash] == ["a.txt"]
    assert table.hasFocus() and table.selectionModel().selectedRows()[0].row() == 0


def test_a_restore_that_came_back_renamed_says_its_new_name(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    item = client.server.trash_item("report.docx")
    client.server.restore_names[item.id] = "report (restored).docx"
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    table = _open_trash(qtbot, shell, 1)

    table.selectRow(0)
    _button(find_by_name, shell.trash_view, "Restore").click()

    qtbot.waitUntil(lambda: table.model().rowCount() == 0)
    assert _status(shell) == messages.restored_as("report (restored).docx")


def test_deleting_several_names_them_and_says_what_stays(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    for name in ("a.txt", "b.txt", "c.txt", "d.txt"):
        client.server.trash_item(name)
    client.server.trash_item("theirs.txt", can_delete=False)
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    table = _open_trash(qtbot, shell, 5)
    asked: list[str] = []

    def decline(title: str, text: str, action: str) -> bool:
        asked.append(text)
        return False

    shell.trash_view.confirm = decline

    table.selectAll()
    [shortcut] = [
        a for a in table.actions() if a.shortcut() == QKeySequence(QKeySequence.StandardKey.Delete)
    ]
    shortcut.trigger()

    assert asked == [
        "“a.txt”, “b.txt”, “c.txt” and 1 more will be deleted permanently. This "
        "can't be undone. 1 item you don't own stays in the trash."
    ]


def test_a_failed_reload_keeps_the_list(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.server.trash_item("report.docx")
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    table = _open_trash(qtbot, shell, 1)

    client.server.offline = True
    client.trashed.refresh()

    qtbot.waitUntil(lambda: bool(_error_text(shell)))
    assert shell.trash_view.state == "content" and table.model().rowCount() == 1
    assert _error_text(shell) == messages.OFFLINE


def test_a_large_trash_empties_in_several_calls_and_can_be_stopped(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    for n in range(3):
        client.server.trash_item(f"{n}.txt")
    client.server.empty_limit = 1
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    view = shell.trash_view
    _open_trash(qtbot, shell, 3)
    view.confirm = lambda title, text, action: True
    gate = client.server.gates["/api/v1/trash"] = threading.Event()  # holds each call
    cancel = _button(find_by_name, view, "Cancel")

    _button(find_by_name, view, "Empty trash…").click()
    assert cancel.isVisible() and not _button(find_by_name, view, "Restore").isEnabled()
    _button(find_by_name, view, "Empty trash…").click()  # disabled: nothing happens
    qtbot.waitUntil(lambda: "DELETE /api/v1/trash" in client.server.calls)
    cancel.click()
    gate.set()

    qtbot.waitUntil(lambda: not cancel.isVisible())
    del client.server.gates["/api/v1/trash"]
    assert len(client.server.trash) == 2  # the call in progress finished; then it stopped
    assert _status(shell) == messages.trash_action_stopped(1)


def test_the_trash_of_whoever_was_signed_in_before_is_gone(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.server.trash_item("theirs.docx")
    shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync", client)
    table = _open_trash(qtbot, shell, 1)
    error = shell.trash_view.findChild(QLabel, "trashError")
    assert isinstance(error, QLabel)
    error.setText("A message for the previous user")

    shell._needs_sign_in("")

    assert table.model().rowCount() == 0
    assert _error_text(shell) == ""


def test_deleting_for_good_again_after_it_succeeded_is_done(make_client: MakeClient) -> None:
    client = make_client()
    item = client.server.trash_item("report.docx")
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    trash = Trash(client.api)

    trash.delete_forever(item.id)
    trash.delete_forever(item.id)  # the retry of a request whose answer was lost

    assert client.server.trash == []
