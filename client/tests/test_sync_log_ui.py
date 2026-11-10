"""The Activity view (FR-CLIENT-03) and the Can't sync view (FR-SYNC-07): the four view states,
opening and showing items, Try again, and following the engine's reports. Widgets are found by
accessible name."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from client_fakes import USER_PASSWORD, Client
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QLabel, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.store.index import Index, read_activity, read_problems
from dropshare_client.store.records import Activity, Problem
from dropshare_client.ui.sync_log_views import ActivityView, CantSyncView
from dropshare_client.ui.theme import TrayState

_NAME_REASON = "The name can't be used on Windows (it contains “:”). Rename it."


@pytest.fixture
def client(make_client: MakeClient, tmp_path: Path) -> Client:
    made = make_client()
    folder = tmp_path / "Sync"
    folder.mkdir()
    made.config.update(sync_root=folder)
    return made


def _index(client: Client) -> Index:
    return Index(client.app_data / "index.db")


def _folder(client: Client) -> Path:
    root = client.config.config.sync_root
    assert root is not None
    return root


def _button(find_by_name: FindByName, root: QWidget, name: str) -> QPushButton:
    button = find_by_name(root, QPushButton, name)
    assert isinstance(button, QPushButton)
    return button


def _text(view: QWidget, name: str) -> str:
    label = view.findChild(QLabel, name)
    assert isinstance(label, QLabel)
    return label.text()


def _activity_view(qtbot: QtBot, client: Client) -> ActivityView:
    view = ActivityView(client.activity)
    qtbot.addWidget(view)
    view.show()
    return view


def _cant_sync_view(qtbot: QtBot, client: Client) -> CantSyncView:
    view = CantSyncView(client.cant_sync)
    qtbot.addWidget(view)
    view.show()
    return view


def _cell(view: ActivityView | CantSyncView, row: int, column: int) -> str:
    return str(view.table.model().index(row, column).data())


# --- reading the index --------------------------------------------------------------------


def test_reading_activity_and_problems_never_creates_the_index(tmp_path: Path) -> None:
    missing = tmp_path / "index.db"

    assert read_activity(missing, 10) == []
    assert read_problems(missing) == []
    assert not missing.exists()


# --- Activity -----------------------------------------------------------------------------


def test_activity_is_empty_before_anything_synced(qtbot: QtBot, client: Client) -> None:
    view = _activity_view(qtbot, client)

    qtbot.waitUntil(lambda: view.state == "empty")
    assert _text(view, "empty_message") == messages.ACTIVITY_EMPTY


def test_activity_lists_events_newest_first_and_follows_the_engine(
    qtbot: QtBot, client: Client
) -> None:
    index = _index(client)
    index.add_activity("uploaded", "Docs/plan.docx")
    index.add_activity("conflict", "Docs/notes.txt", "Docs/notes (conflicted copy).txt")
    index.close()
    view = _activity_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "content")

    assert view.table.model().rowCount() == 2
    assert [_cell(view, 0, column) for column in (1, 2, 3)] == [
        "Conflict",
        "Docs/notes.txt",
        "Your version was saved as “notes (conflicted copy).txt”.",
    ]
    assert _cell(view, 1, 1) == "Uploaded"

    client.sync.activity_added.emit(Activity(datetime.now(UTC), "downloaded", "new.txt", ""))

    qtbot.waitUntil(lambda: view.table.model().rowCount() == 3)
    assert (_cell(view, 0, 1), _cell(view, 0, 2)) == (
        "Downloaded",
        "new.txt",
    )


def test_unreadable_activity_shows_an_error_with_try_again(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    index_path = client.app_data / "index.db"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_bytes(b"not a database")
    view = _activity_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "error")

    index_path.unlink()
    _button(find_by_name, view, "Try again").click()

    qtbot.waitUntil(lambda: view.state == "empty")


def test_open_and_show_in_folder_act_on_the_item(
    qtbot: QtBot, client: Client, find_by_name: FindByName, monkeypatch: pytest.MonkeyPatch
) -> None:
    (_folder(client) / "Docs").mkdir()
    (_folder(client) / "Docs" / "plan.docx").write_bytes(b"plan")
    index = _index(client)
    index.add_activity("uploaded", "Docs/plan.docx")
    index.close()
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", opened.append)
    view = _activity_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "content")
    open_button = _button(find_by_name, view, "Open")
    assert not open_button.isEnabled()  # nothing chosen yet

    view.table.selectRow(0)
    open_button.click()
    qtbot.waitUntil(lambda: bool(opened))
    _button(find_by_name, view, "Show in folder").click()
    qtbot.waitUntil(lambda: bool(client.revealed))

    assert Path(opened[0].toLocalFile()) == _folder(client) / "Docs" / "plan.docx"
    assert client.revealed == [_folder(client) / "Docs" / "plan.docx"]


def test_an_item_no_longer_there_says_so(
    qtbot: QtBot, client: Client, find_by_name: FindByName, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _index(client)
    index.add_activity("downloaded", "gone.txt")
    index.add_activity("deleted_here", "deleted.txt")
    index.close()
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: None)
    view = _activity_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "content")

    view.table.selectRow(0)  # deleted: there is nothing to open
    assert not _button(find_by_name, view, "Open").isEnabled()
    view.table.selectRow(1)
    _button(find_by_name, view, "Open").click()

    qtbot.waitUntil(lambda: bool(_text(view, "ActivityError")))
    assert "isn't in your DropShare folder any more" in _text(view, "ActivityError")


# --- Can't sync ---------------------------------------------------------------------------


def test_cant_sync_lists_reasons_and_follows_the_engine(qtbot: QtBot, client: Client) -> None:
    index = _index(client)
    index.set_problem(Problem("a:b.txt", "a:b.txt", _NAME_REASON))
    index.close()
    view = _cant_sync_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "content")

    assert (_cell(view, 0, 0), _cell(view, 0, 1)) == (
        "a:b.txt",
        _NAME_REASON,
    )

    client.sync.problems_changed.emit([])  # fixed, and synced

    qtbot.waitUntil(lambda: view.state == "empty")
    assert _text(view, "empty_message") == messages.CANT_SYNC_EMPTY


def test_try_again_looks_at_the_chosen_items_again(
    qtbot: QtBot, client: Client, find_by_name: FindByName, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = _index(client)
    index.set_problem(Problem("a:b.txt", "a:b.txt", _NAME_REASON))
    index.set_problem(Problem("c?.txt", "c?.txt", _NAME_REASON))
    index.close()
    retried: list[str] = []
    monkeypatch.setattr(client.sync, "retry", retried.append)
    view = _cant_sync_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "content")
    try_again = _button(find_by_name, view, "Try again")
    assert not try_again.isEnabled()

    view.table.selectAll()
    try_again.click()

    assert retried == ["a:b.txt", "c?.txt"]
    assert _text(view, "CantsyncStatus") == messages.RETRYING


def test_show_in_folder_reveals_the_item(
    qtbot: QtBot, client: Client, find_by_name: FindByName
) -> None:
    (_folder(client) / "a:b.txt").write_bytes(b"x")
    index = _index(client)
    index.set_problem(Problem("a:b.txt", "a:b.txt", _NAME_REASON))
    index.close()
    view = _cant_sync_view(qtbot, client)
    qtbot.waitUntil(lambda: view.state == "content")

    view.table.selectRow(0)
    _button(find_by_name, view, "Show in folder").click()

    qtbot.waitUntil(lambda: bool(client.revealed))
    assert client.revealed == [_folder(client) / "a:b.txt"]


def test_recent_activity_in_the_tray_opens_the_activity_view(
    qtbot: QtBot, client: Client, make_shell: MakeShell
) -> None:
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    shell.window.show_page(messages.MY_FILES)

    shell.tray.recent_activity.trigger()

    assert shell.window.current_page_title() == messages.ACTIVITY
    assert shell.window.isVisible()


def test_activity_and_settings_in_the_tray_wait_for_sign_in(
    qtbot: QtBot, client: Client, make_shell: MakeShell
) -> None:
    shell = make_shell(client)
    assert shell.tray.state is TrayState.SIGNED_OUT
    assert not shell.tray.recent_activity.isEnabled()
    assert not shell.tray.settings.isEnabled()

    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)

    assert shell.tray.recent_activity.isEnabled() and shell.tray.settings.isEnabled()


# --- a failed reload while a list is shown ------------------------------------------------


def test_a_reload_that_works_clears_the_message_of_one_that_failed(
    qtbot: QtBot, client: Client
) -> None:
    index = _index(client)
    index.add_activity("uploaded", "plan.docx")
    index.set_problem(Problem("a:b.txt", "a:b.txt", _NAME_REASON))
    index.close()
    activity = _activity_view(qtbot, client)
    cant_sync = _cant_sync_view(qtbot, client)
    qtbot.waitUntil(lambda: activity.state == "content" and cant_sync.state == "content")
    index_path = client.app_data / "index.db"
    readable = index_path.read_bytes()
    index_path.write_bytes(b"not a database")

    client.activity.refresh()  # as when the engine reports, not the Refresh button
    client.cant_sync.refresh()

    qtbot.waitUntil(lambda: bool(_text(activity, "ActivityError")))
    qtbot.waitUntil(lambda: bool(_text(cant_sync, "CantsyncError")))
    assert activity.state == "content" and cant_sync.state == "content"

    index_path.write_bytes(readable)
    client.activity.refresh()
    client.cant_sync.refresh()

    qtbot.waitUntil(lambda: not _text(activity, "ActivityError"))
    qtbot.waitUntil(lambda: not _text(cant_sync, "CantsyncError"))
