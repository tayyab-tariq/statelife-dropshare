"""Sharing in the app (FR-SHARE-01/03/04/07/08, FR-FILE-01, FR-CLIENT-02): the Share dialog,
Shared with me, links and the new-share notification. Widgets are found by accessible name."""

import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from client_fakes import USER_PASSWORD, Client
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtGui import QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QTableView,
    QWidget,
)
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.services.links import link_to, node_of
from dropshare_client.single_instance import SingleInstance
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import TrayState
from dropshare_shared.schemas.sharing import GroupHit, MemberOut, SharedItemOut, UserHit

_WHEN = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
_ALICE = UserHit(id=uuid.UUID(int=1), display_name="Alice", email="alice@example.com")


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


def _open_share(qtbot: QtBot, shell: AppShell, row: int = 0) -> None:
    table = shell.browser_view.table
    qtbot.waitUntil(lambda: table.model().rowCount() > row)
    table.selectRow(row)
    share = next(a for a in table.actions() if a.text().replace("&", "") == "Share…")
    share.trigger()
    qtbot.waitUntil(shell.share_dialog.isVisible)


def _button(find_by_name: FindByName, root: QWidget, name: str) -> QPushButton:
    button = find_by_name(root, QPushButton, name)
    assert isinstance(button, QPushButton)
    return button


def _rows(widget: QListWidget) -> list[str]:
    return [widget.item(row).text() for row in range(widget.count())]


def test_sharing_a_folder_with_someone_found_by_name(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    folder = client.server.add(client.server.root_id, "Budget")
    client.server.people = [
        UserHit(id=uuid.uuid4(), display_name="Bob Smith", email="bob@example.com")
    ]
    client.server.groups = [GroupHit(id=uuid.uuid4(), name="Board", member_count=3)]
    client.browser.refresh()
    _open_share(qtbot, shell)
    dialog = shell.share_dialog
    search = find_by_name(dialog, QLineEdit, "Find people or groups")
    found = find_by_name(dialog, QListWidget, "People and groups found")
    members = find_by_name(dialog, QListWidget, "People with access")
    role = find_by_name(dialog, QComboBox, "What they can do")
    assert isinstance(members, QListWidget)
    qtbot.waitUntil(lambda: _rows(members) == ["Alice (alice@example.com) · Owner"])

    assert isinstance(search, QLineEdit) and isinstance(found, QListWidget)
    search.setText("bo")
    qtbot.waitUntil(lambda: _rows(found) == ["Bob Smith · bob@example.com", "Board · 3 people"])
    assert isinstance(role, QComboBox)
    role.setCurrentIndex(role.findText("Can view"))
    _button(find_by_name, dialog, "Share").click()

    assert isinstance(members, QListWidget)
    qtbot.waitUntil(lambda: len(_rows(members)) == 2)
    assert _rows(members)[1] == "Bob Smith (bob@example.com) · Can view"
    assert client.server.members[folder][0].role == "viewer"
    status = dialog.findChild(QLabel, "shareStatus")
    assert isinstance(status, QLabel) and status.text() == messages.shared_with("Bob Smith")


def test_changing_a_role_and_removing_access(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    folder = client.server.add(client.server.root_id, "Budget")
    bob = UserHit(id=uuid.uuid4(), display_name="Bob", email="bob@example.com")
    client.server.people = [bob]
    client.browser.refresh()
    _open_share(qtbot, shell)
    dialog = shell.share_dialog
    search = find_by_name(dialog, QLineEdit, "Find people or groups")
    assert isinstance(search, QLineEdit)
    search.setText("bob")
    found = find_by_name(dialog, QListWidget, "People and groups found")
    assert isinstance(found, QListWidget)
    qtbot.waitUntil(lambda: found.count() == 1)
    _button(find_by_name, dialog, "Share").click()
    members = find_by_name(dialog, QListWidget, "People with access")
    assert isinstance(members, QListWidget)
    qtbot.waitUntil(lambda: members.count() == 2)

    members.setCurrentRow(1)
    change = find_by_name(dialog, QComboBox, "Change what they can do")
    assert isinstance(change, QComboBox)
    change.setCurrentIndex(change.findText("Can view"))
    change.activated.emit(change.currentIndex())
    qtbot.waitUntil(lambda: _rows(members)[-1].endswith("Can view"))
    members.setCurrentRow(1)
    asked: list[str] = []

    def agree(title: str, text: str, action: str) -> bool:
        asked.append(action)
        return True

    dialog.confirm = agree
    _button(find_by_name, dialog, "Remove access…").click()

    qtbot.waitUntil(lambda: members.count() == 1)
    assert client.server.members[folder] == []
    assert asked == ["Remove access"]  # asked first (ui.md §5)


def test_someone_who_isnt_the_owner_sees_who_has_access_but_cant_change_it(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    client.server.add(client.server.root_id, "Team")
    client.server.can_manage = False
    client.browser.refresh()

    _open_share(qtbot, shell)
    dialog = shell.share_dialog
    members = find_by_name(dialog, QListWidget, "People with access")
    assert isinstance(members, QListWidget)
    qtbot.waitUntil(lambda: members.count() == 1)

    assert not find_by_name(dialog, QLineEdit, "Find people or groups").isVisibleTo(dialog)
    assert not find_by_name(dialog, QPushButton, "Remove access…").isVisibleTo(dialog)
    note = [w for w in dialog.findChildren(QLabel) if "Only the owner" in w.text()]
    assert note and note[0].isVisibleTo(dialog)


def test_copy_link_holds_the_items_id_and_nothing_else(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    folder = client.server.add(client.server.root_id, "Budget")
    client.browser.refresh()
    _open_share(qtbot, shell)
    members = find_by_name(shell.share_dialog, QListWidget, "People with access")
    assert isinstance(members, QListWidget)
    qtbot.waitUntil(lambda: members.count() == 1)

    _button(find_by_name, shell.share_dialog, "Copy link").click()

    assert QGuiApplication.clipboard().text() == f"statelife-dropshare://open?node={folder}"


@pytest.mark.parametrize(
    "text",
    [
        "statelife-dropshare://open?node=not-a-uuid",
        "statelife-dropshare://delete?node=00000000-0000-0000-0000-000000000001",
        "https://open?node=00000000-0000-0000-0000-000000000001",
        "statelife-dropshare://open?node=00000000-0000-0000-0000-000000000001&token=x",
        "statelife-dropshare://open?node=00000000-0000-0000-0000-000000000001#x",
        "statelife-dropshare://open/../x?node=00000000-0000-0000-0000-000000000001",
        "statelife-dropshare://open?node={00000000-0000-0000-0000-000000000001}",
        "",
    ],
)
def test_anything_but_an_exact_link_is_ignored(text: str) -> None:
    assert node_of(text) is None


def test_a_link_round_trips() -> None:
    node_id = uuid.uuid4()
    assert node_of(link_to(node_id)) == node_id
    assert node_of(link_to(node_id).upper().replace("NODE=", "node=")) == node_id


def test_opening_a_link_shows_the_folder_and_an_unknown_one_says_so(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    reports = client.server.add(client.server.root_id, "Reports")
    client.server.add(reports, "2026")
    table = shell.browser_view.table

    shell.open_link(link_to(reports))
    qtbot.waitUntil(lambda: [c.name for c in client.browser.crumbs()] == ["My files", "Reports"])
    qtbot.waitUntil(lambda: table.model().rowCount() == 1)
    shell.open_link(link_to(uuid.uuid4()))

    qtbot.waitUntil(
        lambda: any(
            w.text() == messages.LINK_UNAVAILABLE for w in shell.browser_view.findChildren(QLabel)
        )
    )
    assert [c.name for c in client.browser.crumbs()] == ["My files", "Reports"]  # stays put


def test_a_second_start_hands_its_link_to_the_running_app(qtbot: QtBot, tmp_path: Path) -> None:
    running = SingleInstance("dropshare-link-test", tmp_path / "instance.lock")
    assert running.claim()
    try:
        second = SingleInstance("dropshare-link-test", tmp_path / "instance.lock")
        link = link_to(uuid.uuid4())
        with qtbot.waitSignal(running.link_received, timeout=5000) as received:
            assert not second.claim(link)
        assert received.args == [link]
    finally:
        running.release()


def _shared_items() -> list[SharedItemOut]:
    folder = SharedItemOut(
        node_id=uuid.uuid4(),
        namespace_id=uuid.uuid4(),
        kind="folder",
        name="Budget",
        owner=_ALICE,
        role="editor",
        shared_at=_WHEN,
        size_bytes=0,
        modified_at=_WHEN,
        mount_node_id=None,
    )
    file = folder.model_copy(
        update={
            "node_id": uuid.uuid4(),
            "namespace_id": None,
            "kind": "file",
            "name": "plan.txt",
            "role": "viewer",
            "size_bytes": 4,
        }
    )
    return [folder, file]


def test_shared_with_me_lists_items_and_adds_a_folder_to_my_dropshare(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.shared = _shared_items()
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    (tmp_path / "Sync").mkdir()
    client.config.update(sync_root=tmp_path / "Sync")
    shell = make_shell(client)
    shell.start()
    view = shell.shared_view
    table = find_by_name(view, QTableView, "Shared with me")
    assert isinstance(table, QTableView)
    qtbot.waitUntil(lambda: table.model().rowCount() == 2)
    model = table.model()

    assert [model.index(0, c).data() for c in (0, 1, 2, 4)] == ["Budget", "Alice", "Edit", "No"]
    assert model.index(1, 4).data() == "Not synced (a single file)"
    table.selectRow(1)
    assert not find_by_name(view, QPushButton, "Upload a new version…").isEnabled()  # viewer
    assert find_by_name(view, QPushButton, "Save a copy…").isEnabled()
    table.selectRow(0)
    _button(find_by_name, view, "Add to my DropShare").click()

    qtbot.waitUntil(lambda: model.index(0, 4).data() == "Yes")
    table.selectRow(0)
    assert find_by_name(view, QPushButton, "Leave…").isEnabled()


def test_shared_with_me_is_empty_until_something_is_shared(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    _, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")

    qtbot.waitUntil(lambda: shell.shared_view.state == "empty")


def test_a_new_share_is_announced(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    qtbot.waitUntil(lambda: shell.shared_view.state == "empty")
    notes: list[tuple[str, str]] = []
    shell.tray.notify = lambda title, message: notes.append((title, message))  # type: ignore[method-assign]  # record instead of showing

    client.server.shared = _shared_items()[:1]
    client.shared.refresh()

    qtbot.waitUntil(lambda: bool(notes))
    assert notes == [("Shared with you", messages.new_shares(["Budget"]))]


def test_reopening_the_dialog_for_another_item_shows_nothing_of_the_last_one(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    """Its list, its buttons and its link are gone before the new item loads (audit B1)."""
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    first = client.server.add(client.server.root_id, "A")
    client.server.add(client.server.root_id, "B")
    bob = UserHit(id=uuid.uuid4(), display_name="Bob", email="bob@example.com")
    client.server.members[first] = [
        MemberOut(
            id=uuid.uuid4(),
            kind="user",
            user_id=bob.id,
            group_id=None,
            name="Bob",
            email=bob.email,
            role="editor",
        )
    ]
    client.browser.refresh()
    _open_share(qtbot, shell, 0)
    dialog = shell.share_dialog
    members = find_by_name(dialog, QListWidget, "People with access")
    assert isinstance(members, QListWidget)
    qtbot.waitUntil(lambda: members.count() == 2)
    second = client.server.children[client.server.root_id][1].id
    gate = client.server.gates[f"/api/v1/nodes/{second}/members"] = threading.Event()

    _open_share(qtbot, shell, 1)

    assert members.count() == 0
    assert not _button(find_by_name, dialog, "Copy link").isEnabled()
    gate.set()
    qtbot.waitUntil(lambda: members.count() == 1)
    assert _button(find_by_name, dialog, "Copy link").isEnabled()


def test_a_link_to_a_shared_folders_top_isnt_taken_for_my_own_folder(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """New folder and uploads there must not land at the top of the user's own DropShare
    (audit B2): without a synced copy here, they're off with the reason."""
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    shared_root = uuid.uuid4()
    client.server.children[shared_root] = []
    client.server.add(shared_root, "inside.txt", 4)
    root = client.server.node(client.server.root_id)
    assert root is not None
    client.server.extra_nodes[shared_root] = root.model_copy(
        update={"id": shared_root, "namespace_id": uuid.uuid4()}
    )

    shell.open_link(link_to(shared_root))

    qtbot.waitUntil(lambda: [c.name for c in client.browser.crumbs()] == [messages.SHARED_FOLDER])
    assert client.browser.current_folder is None


def test_a_link_to_a_file_shared_on_its_own_opens_shared_with_me(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    folder = uuid.uuid4()  # the owner's folder: not shared with Alice
    file_id = client.server.add(folder, "plan.txt", 4)
    file = client.server.node(file_id)
    assert file is not None
    del client.server.children[folder]
    client.server.extra_nodes[file_id] = file  # Alice may read the file, not its folder
    client.server.shared = [_shared_items()[1].model_copy(update={"node_id": file_id})]

    shell.open_link(link_to(file_id))

    table = shell.shared_view.table
    qtbot.waitUntil(lambda: table.selectionModel().hasSelection())
    assert shell.window.current_page_title() == "Shared with me"


def test_shared_with_me_reads_every_page(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client = make_client()
    item = _shared_items()[0]
    client.server.shared = [
        item.model_copy(update={"node_id": uuid.uuid4(), "name": f"Folder {n:03}"})
        for n in range(205)
    ]
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    (tmp_path / "Sync").mkdir()
    client.config.update(sync_root=tmp_path / "Sync")
    shell = make_shell(client)
    shell.start()

    qtbot.waitUntil(lambda: shell.shared_view.table.model().rowCount() == 205)


def test_leaving_asks_first_and_f5_refreshes(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client = make_client()
    client.server.shared = [_shared_items()[0].model_copy(update={"mount_node_id": uuid.uuid4()})]
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    (tmp_path / "Sync").mkdir()
    client.config.update(sync_root=tmp_path / "Sync")
    shell = make_shell(client)
    shell.start()
    view = shell.shared_view
    qtbot.waitUntil(lambda: view.table.model().rowCount() == 1)
    view.table.selectRow(0)
    asked: list[str] = []

    def decline(title: str, text: str, action: str) -> bool:
        asked.append(action)
        return False

    view.confirm = decline

    _button(find_by_name, view, "Leave…").click()
    calls = len(client.server.calls)
    [refresh] = [
        a for a in view.actions() if a.shortcut() == QKeySequence(QKeySequence.StandardKey.Refresh)
    ]
    refresh.trigger()

    assert asked == ["Leave folder"]
    assert view.table.model().index(0, 4).data() == "Yes"  # not left: the user said no
    qtbot.waitUntil(lambda: "GET /api/v1/shared-with-me" in client.server.calls[calls:])
