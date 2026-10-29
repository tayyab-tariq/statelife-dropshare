"""The browser's actions (FR-FILE-03/04/05): by keyboard and menu, on the local copy (OQ-23),
off with the reason for items not on this computer yet (OQ-28), and the engine told at once."""

from pathlib import Path

from client_fakes import USER_PASSWORD, Client
from conftest import MakeClient, MakeShell
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QMenu
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.store.records import Entry
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.browser_model import BrowserModel
from dropshare_client.ui.theme import TrayState
from dropshare_shared.schemas.nodes import NodeKind


class Setup:
    def __init__(self, client: Client, shell: AppShell, root: Path) -> None:
        self.client, self.shell, self.root = client, shell, root
        self.table = shell.browser_view.table
        model = self.table.model()
        assert isinstance(model, BrowserModel)
        self.model = model
        self.engine = client.engines.last

    def names(self) -> list[str]:
        return [self.model.index(r, 0).data() for r in range(self.model.rowCount())]

    def here(self) -> list[str]:
        """Names of the rows the index says are on this computer."""
        rows = (self.model.row_at(r) for r in range(self.model.rowCount()))
        return [r.node.name or "" for r in rows if r is not None and r.local_path is not None]

    def select(self, name: str) -> None:
        self.table.setFocus()
        self.table.selectRow(self.names().index(name))

    def status(self) -> str:
        label = self.shell.browser_view.findChild(QLabel, "browser_status")
        assert label is not None
        return label.text()

    def error(self) -> str:
        label = self.shell.browser_view.findChild(QLabel, "browser_error")
        assert label is not None
        return label.text() if label.isVisibleTo(self.shell.browser_view) else ""


def _setup(qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path) -> Setup:
    """Synced: Docs/ and notes.txt (on disk and in the index); remote.txt only on the server."""
    client = make_client()
    server = client.server
    docs = server.add(server.root_id, "Docs")
    notes = server.add(server.root_id, "notes.txt", 5)
    server.add(server.root_id, "remote.txt", 7)
    root = tmp_path / "Sync"
    root.mkdir()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    client.config.update(sync_root=root)
    shell = make_shell(client)
    shell.start()
    qtbot.waitUntil(lambda: shell.tray.state is TrayState.UP_TO_DATE)
    # Made once syncing runs: a folder that already holds files must be this account's (OQ-20).
    (root / "Docs").mkdir()
    (root / "notes.txt").write_text("notes")
    index = client.engines.last.index
    index.put_entry(Entry("docs", "Docs", docs, NodeKind.FOLDER, 1, None, 0, 0, None))
    index.put_entry(Entry("notes.txt", "notes.txt", notes, NodeKind.FILE, 1, "0" * 64, 5, 0, None))
    shell.window.show()
    setup = Setup(client, shell, root)
    setup.table.setFocus()
    QTest.keyClick(setup.table, Qt.Key.Key_F5)
    qtbot.waitUntil(lambda: setup.names() == ["Docs", "notes.txt", "remote.txt"])
    qtbot.waitUntil(lambda: setup.here() == ["Docs", "notes.txt"])  # the reload with the index
    return setup


def test_f2_renames_on_this_computer_and_tells_the_engine(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    asked: list[tuple[str, str, str]] = []

    def ask(title: str, action: str, current: str) -> str:
        asked.append((title, action, current))
        return "summary.txt"

    s.shell.browser_view.ask_name = ask

    s.select("notes.txt")
    QTest.keyClick(s.table, Qt.Key.Key_F2)

    qtbot.waitUntil(lambda: s.status() == messages.RENAMED)
    assert asked == [("Rename", "Rename", "notes.txt")]
    assert (s.root / "summary.txt").read_text() == "notes"
    assert {"changed notes.txt", "changed summary.txt"} <= set(s.engine.calls)


def test_a_taken_name_says_so_and_changes_nothing(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.shell.browser_view.ask_name = lambda *_: "DOCS"

    s.select("notes.txt")
    QTest.keyClick(s.table, Qt.Key.Key_F2)

    qtbot.waitUntil(lambda: "already an item with this name" in s.error())
    assert (s.root / "notes.txt").exists()


def test_delete_asks_first_and_then_uses_the_recycle_bin(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    answers = [False, True]
    shown: list[list[str]] = []

    def confirm(names: list[str]) -> bool:
        shown.append(names)
        return answers.pop(0)

    s.shell.browser_view.confirm_recycle = confirm

    s.select("notes.txt")
    QTest.keyClick(s.table, Qt.Key.Key_Delete)  # declined
    assert (s.root / "notes.txt").exists()
    QTest.keyClick(s.table, Qt.Key.Key_Delete)

    qtbot.waitUntil(lambda: not (s.root / "notes.txt").exists())
    assert shown == [["notes.txt"], ["notes.txt"]]
    assert (s.client.trash / "notes.txt").read_text() == "notes"
    assert "changed notes.txt" in s.engine.calls


def test_new_folder_goes_into_the_folder_shown(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.shell.browser_view.ask_name = lambda *_: "Reports"

    QTest.keyClick(
        s.table,
        Qt.Key.Key_N,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )

    qtbot.waitUntil(lambda: (s.root / "Reports").is_dir())
    assert s.status() == messages.FOLDER_CREATED


def test_actions_on_an_item_not_here_yet_are_off_and_say_why(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.shell.browser_view.ask_name = lambda *_: "x.txt"

    s.select("remote.txt")
    QTest.keyClick(s.table, Qt.Key.Key_F2)
    QTest.keyClick(s.table, Qt.Key.Key_Delete)

    actions = {a.text().replace("&", ""): a for a in s.table.actions()}
    for name in (
        "Rename…",
        "Move to…",
        "Move to Recycle Bin…",
        "Show in folder",
        "Save a copy to…",
    ):
        assert not actions[name].isEnabled(), name
        assert actions[name].toolTip() == messages.NOT_HERE_YET
    assert not any(c.startswith("changed") for c in s.engine.calls)


def test_move_to_moves_it_into_the_chosen_folder(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.shell.browser_view.choose_folder = lambda title, start: (start or s.root) / "Docs"
    actions = {a.text().replace("&", ""): a for a in s.table.actions()}

    s.select("notes.txt")
    actions["Move to…"].trigger()

    qtbot.waitUntil(lambda: (s.root / "Docs" / "notes.txt").exists())
    assert {"changed notes.txt", "changed Docs/notes.txt"} <= set(s.engine.calls)


def test_dragging_onto_a_folder_moves_it_but_not_onto_a_file(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    notes = s.model.index(s.names().index("notes.txt"), 0)
    docs = s.model.index(s.names().index("Docs"), 0)
    remote = s.model.index(s.names().index("remote.txt"), 0)
    data = s.model.mimeData([notes])

    assert not s.model.flags(remote) & Qt.ItemFlag.ItemIsDragEnabled  # not here yet
    assert not s.model.canDropMimeData(data, Qt.DropAction.MoveAction, -1, -1, notes)
    assert s.model.canDropMimeData(data, Qt.DropAction.MoveAction, -1, -1, docs)
    s.model.dropMimeData(data, Qt.DropAction.MoveAction, -1, -1, docs)

    qtbot.waitUntil(lambda: (s.root / "Docs" / "notes.txt").exists())


def test_show_in_folder_and_save_a_copy(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    out = tmp_path / "Desktop"
    out.mkdir()
    s.shell.browser_view.choose_folder = lambda title, start: out
    actions = {a.text().replace("&", ""): a for a in s.table.actions()}

    s.select("notes.txt")
    actions["Show in folder"].trigger()
    qtbot.waitUntil(lambda: s.client.revealed == [s.root / "notes.txt"])
    qtbot.waitUntil(lambda: actions["Save a copy to…"].isEnabled())  # one action at a time
    actions["Save a copy to…"].trigger()

    qtbot.waitUntil(lambda: (out / "notes.txt").exists())
    assert (out / "notes.txt").read_text() == "notes"


def test_the_menu_key_lists_every_action(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.select("notes.txt")

    s.table.customContextMenuRequested.emit(QPoint(10, 10))

    menu = next(m for m in s.table.findChildren(QMenu) if m.isVisible())
    texts = [a.text().replace("&", "") for a in menu.actions() if not a.isSeparator()]
    assert texts == [
        "Open",
        "Rename…",
        "Move to…",
        "Move to Recycle Bin…",
        "Show in folder",
        "Save a copy to…",
        "Share…",
        "New folder…",
        "Upload files…",
        "Upload a folder…",
        "Refresh",
    ]
    menu.close()


def test_an_action_on_an_item_that_changed_meanwhile_is_refused_and_the_list_refreshes(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Another device renamed notes.txt and made a new one at that name: renaming what the
    list shows must not rename the new item (audit S4)."""
    s = _setup(qtbot, make_client, make_shell, tmp_path)
    s.shell.browser_view.ask_name = lambda *_: "renamed.txt"
    s.select("notes.txt")
    notes = s.model.row_at(s.names().index("notes.txt"))
    assert notes is not None
    s.engine.index.move_entries(  # the engine applied the remote rename meanwhile
        Entry("notes.txt", "notes.txt", notes.node.id, NodeKind.FILE, 1, "0" * 64, 5, 0, None),
        "notes-2025.txt",
        "notes-2025.txt",
    )
    calls = len(s.client.server.calls)

    QTest.keyClick(s.table, Qt.Key.Key_F2)

    qtbot.waitUntil(lambda: "changed since the list was shown" in s.error())
    assert (s.root / "notes.txt").read_text() == "notes"
    assert not (s.root / "renamed.txt").exists()
    qtbot.waitUntil(lambda: len(s.client.server.calls) > calls)  # refreshed


def test_a_file_that_just_downloaded_can_be_acted_on_without_a_refresh(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Its index entry is read again once its download is done (audit S20)."""
    from dropshare_client.controllers.transfers_controller import (
        Direction,
        TransferEvent,
        TransferStatus,
    )

    s = _setup(qtbot, make_client, make_shell, tmp_path)
    remote = s.model.row_at(s.names().index("remote.txt"))
    assert remote is not None and remote.local_path is None
    (s.root / "remote.txt").write_text("remote!")
    s.engine.index.put_entry(
        Entry("remote.txt", "remote.txt", remote.node.id, NodeKind.FILE, 1, "0" * 64, 7, 0, None)
    )

    s.engine.listener.transfer(
        TransferEvent("remote.txt", "remote.txt", Direction.DOWNLOAD, TransferStatus.DONE, 7, 7)
    )

    qtbot.waitUntil(lambda: "remote.txt" in s.here())
