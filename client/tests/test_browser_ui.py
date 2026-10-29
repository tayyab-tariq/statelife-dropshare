"""My files (FR-FILE-01, NFR-UX-01/02): the server's folders page by page, breadcrumbs and
keyboard navigation, the four view states, and each item's sync state."""

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from client_fakes import USER_PASSWORD, Client
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.controllers.browser_controller import PAGE_SIZE
from dropshare_client.store.index import Index, read_entries
from dropshare_client.store.records import Entry
from dropshare_client.sync.transfers import Direction, TransferEvent, TransferStatus
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.format import modified_text
from dropshare_client.ui.theme import TrayState
from dropshare_shared.schemas.nodes import NodeKind

NAME, SYNC = 0, 5


def _signed_in(
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


def _names(shell: AppShell) -> list[str]:
    model = shell.browser_view.table.model()
    return [model.index(row, NAME).data() for row in range(model.rowCount())]


def _state(shell: AppShell) -> str:
    return shell.browser_view.state


def test_my_files_lists_folders_first_then_files_by_name(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    server = client.server
    server.add(server.root_id, "notes.txt", 1200)
    server.add(server.root_id, "Reports")
    server.add(server.root_id, "archive")
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)

    shell.start()

    qtbot.waitUntil(lambda: _state(shell) == "content")
    assert _names(shell) == ["archive", "Reports", "notes.txt"]
    model = shell.browser_view.table.model()
    assert [model.index(2, c).data() for c in range(1, 5)] == [
        modified_text(datetime(2026, 10, 28, 9, 30, tzinfo=UTC)),
        "You",
        "1.2 KB",
        "Only you",
    ]
    assert model.index(0, NAME).data(Qt.ItemDataRole.AccessibleTextRole) == "archive, folder"


def test_an_empty_folder_says_so_and_offers_to_upload(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    _, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")

    qtbot.waitUntil(lambda: _state(shell) == "empty")
    find_by_name(shell.browser_view, QPushButton, "Upload files…")


def test_a_failed_load_offers_try_again(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    qtbot.waitUntil(lambda: _state(shell) == "empty")
    client.server.add(client.server.root_id, "back.txt", 3)
    client.server.offline = True

    shell.window.show()
    shell.browser_view.setFocus()  # the list itself is hidden while the folder is empty
    QTest.keyClick(shell.browser_view, Qt.Key.Key_F5)

    qtbot.waitUntil(lambda: _state(shell) == "error")
    client.server.offline = False
    retry = find_by_name(shell.browser_view, QPushButton, "Try again")
    assert isinstance(retry, QPushButton)
    QTest.mouseClick(retry, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: _names(shell) == ["back.txt"])


def test_large_folders_load_page_by_page(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    for number in range(PAGE_SIZE * 2 + 50):
        client.server.add(client.server.root_id, f"file {number:04}.txt", number)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)
    shell.start()
    table = shell.browser_view.table
    model = table.model()
    qtbot.waitUntil(lambda: model.rowCount() == PAGE_SIZE)
    shell.window.show()

    def scrolled_to(count: int) -> bool:
        """Scroll to the end, as a user would; the view asks for more while it can."""
        table.scrollToBottom()
        if model.canFetchMore(table.rootIndex()):
            model.fetchMore(table.rootIndex())
        return model.rowCount() == count

    qtbot.waitUntil(lambda: scrolled_to(PAGE_SIZE * 2))
    qtbot.waitUntil(lambda: scrolled_to(PAGE_SIZE * 2 + 50))

    assert not model.canFetchMore(table.rootIndex())
    assert _names(shell)[-1] == f"file {PAGE_SIZE * 2 + 49:04}.txt"


def test_folders_open_with_enter_and_breadcrumbs_and_backspace_lead_back(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    server = client.server
    projects = server.add(server.root_id, "Projects")
    year = server.add(projects, "2026")
    server.add(year, "plan.docx", 10)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)
    shell.start()
    shell.window.show()
    table = shell.browser_view.table
    qtbot.waitUntil(lambda: _names(shell) == ["Projects"])

    table.setFocus()
    QTest.keyClick(table, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: _names(shell) == ["2026"])
    QTest.keyClick(table, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: _names(shell) == ["plan.docx"])
    find_by_name(shell.window, QLabel, "Current folder: 2026")

    QTest.keyClick(table, Qt.Key.Key_Backspace)
    qtbot.waitUntil(lambda: _names(shell) == ["2026"])
    home = find_by_name(shell.window, QPushButton, "Go to My files")
    assert isinstance(home, QPushButton)
    QTest.mouseClick(home, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: _names(shell) == ["Projects"])
    up = find_by_name(shell.window, QPushButton, "Up one folder")
    assert not up.isEnabled()  # at the top


def test_the_sync_column_says_what_the_index_and_transfers_say(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    server = client.server
    synced = server.add(server.root_id, "synced.txt", 5)
    server.add(server.root_id, "coming.txt", 5)
    server.add(server.root_id, "going.txt", 5)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    engine = client.engines.last
    engine.index.put_entry(
        Entry("synced.txt", "synced.txt", synced, NodeKind.FILE, 1, "0" * 64, 5, 0, None)
    )
    shell.window.show()
    shell.browser_view.setFocus()  # the list itself is hidden while the folder is empty
    QTest.keyClick(shell.browser_view, Qt.Key.Key_F5)
    model = shell.browser_view.table.model()
    qtbot.waitUntil(lambda: model.rowCount() == 3 and model.index(2, SYNC).data() == "Synced")

    engine.listener.transfer(
        TransferEvent("going.txt", "going.txt", Direction.UPLOAD, TransferStatus.ACTIVE, 2, 5, "")
    )

    qtbot.waitUntil(lambda: model.index(1, SYNC).data() == "Uploading")
    assert model.index(0, SYNC).data() == "Not on this computer yet"


def test_signing_out_clears_the_list(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    client.server.add(client.server.root_id, "private.txt", 1)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: _names(shell) == ["private.txt"])

    client.session.sign_out(remove_files=False)

    qtbot.waitUntil(lambda: _names(shell) == [])


def test_without_a_sync_folder_the_overview_asks_for_one(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    client = make_client()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    shell = make_shell(client)

    shell.start()

    qtbot.waitUntil(lambda: shell.window.view.current_state() == "empty")
    assert shell.window._pages.currentWidget() is shell.window.view


# --- the read-only index reader -----------------------------------------------------------


def test_reading_the_index_never_creates_or_changes_it(tmp_path: Path) -> None:
    missing = tmp_path / "none.db"
    assert read_entries(missing, [uuid.uuid4()]) == {}
    assert not missing.exists()

    index = Index(tmp_path / "index.db")
    node = uuid.uuid4()
    entry = Entry("a.txt", "a.txt", node, NodeKind.FILE, 3, "1" * 64, 9, 1, "f")
    index.put_entry(entry)

    assert read_entries(tmp_path / "index.db", [node, uuid.uuid4()]) == {node: entry}
    index.close()


def _signed_in_with(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> tuple[Client, AppShell]:
    """My files with a folder Docs (holding plan.txt) and a file top.txt, shown."""
    client = make_client()
    server = client.server
    docs = server.add(server.root_id, "Docs")
    server.add(docs, "plan.txt", 4)
    server.add(server.root_id, "top.txt", 3)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)
    shell.start()
    shell.window.show()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)  # syncing has started
    qtbot.waitUntil(lambda: _names(shell) == ["Docs", "top.txt"])
    return client, shell


def test_opening_a_folder_never_shows_the_previous_folders_items(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Until the server answers, the list is empty and loading (audit B3)."""
    client, shell = _signed_in_with(qtbot, make_client, make_shell, tmp_path)
    docs = next(r for r in client.browser.rows() if r.node.name == "Docs")
    gate = threading.Event()
    client.server.gates[f"/api/v1/nodes/{docs.node.id}/children"] = gate

    client.browser.open(docs)

    assert _names(shell) == []
    assert _state(shell) == "loading"
    gate.set()
    qtbot.waitUntil(lambda: _names(shell) == ["plan.txt"])


def test_an_open_folder_trashed_elsewhere_steps_up_and_says_so(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Not an error loop on 404 (audit S8)."""
    client, shell = _signed_in_with(qtbot, make_client, make_shell, tmp_path)
    docs = next(r for r in client.browser.rows() if r.node.name == "Docs")
    client.browser.open(docs)
    qtbot.waitUntil(lambda: _names(shell) == ["plan.txt"])
    del client.server.children[docs.node.id]  # trashed on another computer

    client.browser.refresh()

    qtbot.waitUntil(lambda: [c.name for c in client.browser.crumbs()] == ["My files"])
    status = shell.browser_view.findChild(QLabel, "browser_status")
    assert status is not None and status.text() == messages.FOLDER_TRASHED


def test_items_never_appear_twice_when_the_folder_changes_between_pages(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """An item added before page 1's end shifts page 2 by one (audit S9)."""
    client = make_client()
    for number in range(PAGE_SIZE + 10):
        client.server.add(client.server.root_id, f"file {number:04}.txt", 1)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=tmp_path / "Sync")
    (tmp_path / "Sync").mkdir()
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: len(client.browser.rows()) == PAGE_SIZE)
    client.server.add(client.server.root_id, "a first.txt", 1)

    client.browser.load_more()

    qtbot.waitUntil(lambda: not client.browser.can_load_more)
    ids = [row.node.id for row in client.browser.rows()]
    assert len(ids) == len(set(ids))


def test_local_paths_follow_a_rename_made_elsewhere(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """The index, by node ID, says where the folder is now (audit S10)."""
    client, _ = _signed_in_with(qtbot, make_client, make_shell, tmp_path)
    docs = next(r for r in client.browser.rows() if r.node.name == "Docs")
    index = client.engines.last.index
    entry = Entry("docs", "Docs", docs.node.id, NodeKind.FOLDER, 1, None, 0, 0, None)
    index.put_entry(entry)
    client.browser.open(docs)
    qtbot.waitUntil(lambda: client.browser.current_local_path == "Docs")

    index.move_entries(entry, "archive", "Archive")  # the engine applied a rename
    client.browser.refresh()

    qtbot.waitUntil(lambda: client.browser.current_local_path == "Archive")


def test_a_refresh_keeps_the_selected_row(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Rows are brought up to date by node ID, not reset (audit S18)."""
    client, shell = _signed_in_with(qtbot, make_client, make_shell, tmp_path)
    table = shell.browser_view.table
    table.selectRow(1)
    client.server.add(client.server.root_id, "a new one.txt", 1)

    client.browser.refresh()

    qtbot.waitUntil(lambda: len(_names(shell)) == 3)
    selected = [index.row() for index in table.selectionModel().selectedRows()]
    assert [_names(shell)[row] for row in selected] == ["top.txt"]


def test_in_an_empty_folder_the_keys_work_and_files_can_be_dropped(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    """Ctrl+Shift+N with the list hidden behind the empty page (audit S17)."""
    _, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    shell.window.show()
    qtbot.waitUntil(lambda: _state(shell) == "empty")
    view = shell.browser_view
    view.ask_name = lambda *_: "Reports"
    view.setFocus()

    QTest.keyClick(
        view, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    )

    qtbot.waitUntil(lambda: (tmp_path / "Sync" / "Reports").is_dir())
    assert view.acceptDrops()


def test_a_folder_not_on_this_computer_says_why_nothing_can_be_added(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    tmp_path: Path,
) -> None:
    """No dead "Upload files…" button, and the reason in words (audit S17, S19)."""
    client, shell = _signed_in_with(qtbot, make_client, make_shell, tmp_path)
    empty = client.server.add(client.server.root_id, "Empty")
    client.browser.refresh()
    qtbot.waitUntil(lambda: "Empty" in _names(shell))
    row = next(r for r in client.browser.rows() if r.node.id == empty)

    client.browser.open(row)

    qtbot.waitUntil(lambda: _state(shell) == "empty")
    texts = [b.text() for b in shell.browser_view.findChildren(QPushButton) if b.isVisible()]
    assert "Upload files…" not in texts
    reason = shell.browser_view.findChild(QLabel, "browser_reason")
    assert reason is not None and reason.isVisible()
    assert reason.text() == messages.NOT_HERE_YET


def test_going_offline_while_browsing_keeps_the_user_where_they_are(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Offline (and its retries) shows in the tray and the overview, without switching
    sections (audit S16)."""
    client, shell = _signed_in_with(qtbot, make_client, make_shell, tmp_path)
    client.server.offline = True

    client.session.resume()

    qtbot.waitUntil(lambda: shell.tray.state is TrayState.OFFLINE)
    assert shell.window._pages.currentWidget() is shell.browser_view
