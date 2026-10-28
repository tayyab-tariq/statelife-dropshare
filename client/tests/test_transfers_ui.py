"""The Transfers view and the tray summary (FR-CLIENT-08, FR-CLIENT-01), driven by transfer
events from the (fake) engine, as the real one sends them."""

import time
from pathlib import Path

from client_fakes import USER_PASSWORD, Client
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton, QTableView
from pytestqt.qtbot import QtBot

from dropshare_client.controllers.transfers_controller import FINISHED_KEPT, TransferBook
from dropshare_client.sync.engine import SyncState
from dropshare_client.sync.transfers import Direction, TransferEvent, TransferStatus
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import TrayState

DOT = "\N{MIDDLE DOT}"


def _event(
    key: str,
    status: TransferStatus,
    done: int = 0,
    total: int = 100,
    reason: str = "",
    direction: Direction = Direction.UPLOAD,
) -> TransferEvent:
    return TransferEvent(key, key, direction, status, done, total, reason)


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
    shell.window.show_page("Transfers")
    return client, shell


# --- the book (no Qt) --------------------------------------------------------------------


def test_rows_are_active_then_waiting_then_queued_then_finished_newest_first() -> None:
    book = TransferBook()
    for event in (
        _event("q.txt", TransferStatus.QUEUED),
        _event("w.txt", TransferStatus.WAITING, reason="Open in another program."),
        _event("a.txt", TransferStatus.ACTIVE, 40),
        _event("old.txt", TransferStatus.DONE, 100),
        _event("new.txt", TransferStatus.FAILED, reason="No space"),
    ):
        book.apply(event)

    assert [r.key for r in book.rows()] == ["a.txt", "w.txt", "q.txt", "new.txt", "old.txt"]
    assert book.summary() == f"3 items {DOT} 13%"  # 40 of 300 bytes


def test_only_the_last_50_finished_are_kept_and_a_retried_file_appears_once() -> None:
    book = TransferBook()
    for number in range(FINISHED_KEPT + 10):
        book.apply(_event(f"{number}.txt", TransferStatus.DONE, 100))
    book.apply(_event("55.txt", TransferStatus.DONE, 100))  # done again after a retry

    keys = [r.key for r in book.rows()]
    assert len(keys) == FINISHED_KEPT and keys.count("55.txt") == 1
    assert keys[0] == "55.txt" and "0.txt" not in keys
    assert book.summary() == ""


def test_up_to_date_clears_what_was_still_open() -> None:
    book = TransferBook()
    book.apply(_event("q.txt", TransferStatus.QUEUED))
    book.apply(_event("d.txt", TransferStatus.DONE, 100))

    book.settle()

    assert [r.key for r in book.rows()] == ["d.txt"]


# --- the view and the tray -----------------------------------------------------------------


def test_the_view_is_empty_until_something_transfers(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    view = shell.transfers_view
    empty = view.findChild(QLabel, "transfers_empty")
    assert empty is not None and empty.isVisible()

    client.engines.last.listener.transfer(_event("report.docx", TransferStatus.QUEUED))

    table = view.findChild(QTableView, "transferTable")
    assert table is not None
    qtbot.waitUntil(lambda: table.isVisible() and table.model().rowCount() == 1)
    assert table.model().index(0, 0).data() == "report.docx"


def test_progress_rows_and_the_tray_show_the_same_summary(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    listener = client.engines.last.listener
    listener.state_changed(SyncState.SYNCING, None)

    listener.transfer(_event("a.bin", TransferStatus.ACTIVE, 900, 2000))
    listener.transfer(_event("b.bin", TransferStatus.QUEUED, 0, 2000))

    summary = f"2 items {DOT} 22%"
    qtbot.waitUntil(lambda: summary in shell.tray.tooltip())
    find_by_name(shell.window, QLabel, f"Transfers summary: Syncing {summary}")
    assert shell.tray.status_line.text() == f"Syncing {summary}"
    table = shell.transfers_view.findChild(QTableView, "transferTable")
    assert table is not None
    model = table.model()
    assert [model.index(0, c).data() for c in range(5)] == [
        "a.bin",
        "Upload",
        "45%",
        "900 bytes of 2.0 KB",
        "45%",
    ]
    assert model.index(1, 4).data() == "Queued"

    listener.transfer(_event("a.bin", TransferStatus.DONE, 2000, 2000))
    listener.transfer(_event("b.bin", TransferStatus.DONE, 2000, 2000))
    listener.state_changed(SyncState.UP_TO_DATE, None)
    qtbot.waitUntil(lambda: shell.tray.tooltip() == "StateLife DropShare: Up to date")
    qtbot.waitUntil(  # the view follows on its next refresh (at most 250 ms)
        lambda: any(
            label.accessibleName() == "Transfers summary: Nothing to sync right now"
            for label in shell.window.findChildren(QLabel)
        )
    )


def test_a_failed_transfer_shows_why_and_retry_asks_the_engine(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    tmp_path: Path,
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    engine = client.engines.last
    engine.listener.transfer(_event("ok.txt", TransferStatus.DONE, 100))
    engine.listener.transfer(
        _event("big.iso", TransferStatus.FAILED, reason="There isn't enough space left.")
    )
    table = shell.transfers_view.findChild(QTableView, "transferTable")
    assert table is not None
    qtbot.waitUntil(lambda: table.model().rowCount() == 2)
    retry = find_by_name(shell.window, QPushButton, "Retry")
    assert isinstance(retry, QPushButton)
    assert table.model().index(0, 4).data() == "Failed: There isn't enough space left."

    table.selectRow(1)  # done: nothing to retry
    assert not retry.isEnabled()
    table.selectRow(0)
    assert retry.isEnabled()
    QTest.mouseClick(retry, Qt.MouseButton.LeftButton)

    assert engine.calls[-1] == "retry big.iso"


def test_a_thousand_queued_files_arrive_as_one_quick_update(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """FR-CLIENT-08: the view stays responsive with 1,000 queued items."""
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    listener = client.engines.last.listener
    table = shell.transfers_view.findChild(QTableView, "transferTable")
    assert table is not None
    resets: list[None] = []
    table.model().modelReset.connect(lambda: resets.append(None))

    started = time.monotonic()
    for number in range(1000):
        listener.transfer(_event(f"file {number}.txt", TransferStatus.QUEUED))
    qtbot.waitUntil(lambda: table.model().rowCount() == 1000, timeout=5000)

    assert time.monotonic() - started < 2.0
    assert len(resets) <= 2  # gathered, not one update per file


def test_signing_out_clears_the_list(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    client.engines.last.listener.transfer(_event("a.txt", TransferStatus.DONE, 100))
    table = shell.transfers_view.findChild(QTableView, "transferTable")
    assert table is not None
    qtbot.waitUntil(lambda: table.model().rowCount() == 1)

    client.sync.stop()

    qtbot.waitUntil(lambda: table.model().rowCount() == 0)


def test_new_transfers_dont_move_the_selection(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, tmp_path: Path
) -> None:
    """Rows are updated by key, not reset (audit S18)."""
    client, shell = _signed_in(qtbot, make_client, make_shell, tmp_path / "Sync")
    listener = client.engines.last.listener
    table = shell.transfers_view.findChild(QTableView, "transferTable")
    assert table is not None
    listener.transfer(_event("b.txt", TransferStatus.QUEUED))
    listener.transfer(_event("c.txt", TransferStatus.QUEUED))
    qtbot.waitUntil(lambda: table.model().rowCount() == 2)
    table.selectRow(1)

    listener.transfer(_event("a.txt", TransferStatus.ACTIVE, 10))  # sorts first

    qtbot.waitUntil(lambda: table.model().rowCount() == 3)
    [selected] = table.selectionModel().selectedRows()
    assert table.model().index(selected.row(), 0).data() == "c.txt"
