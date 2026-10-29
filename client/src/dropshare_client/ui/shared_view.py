"""Shared with me (FR-FILE-01, FR-SHARE-02/03): folders and files other people share with the
user. Folders can be added to their DropShare (they then sync) or left; single files aren't in
the sync folder, so a copy is saved from here, and an editor uploads a new version from here
(OQ-32). Loading, error with Retry, empty and populated states (NFR-UX-01)."""

import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.shared_controller import (
    NodeKind,
    SharedController,
    SharedItemOut,
    ShareRole,
)
from dropshare_client.ui import theme
from dropshare_client.ui.forms import announce, confirm, message_label, show_message, status_label
from dropshare_client.ui.shared_model import IN_DROPSHARE, NAME, OWNER, ROLE, SHARED, SharedModel
from dropshare_client.ui.stateful_view import StatefulView

_STATUS_MS = 8000


class SharedWithMeView(QWidget):
    def __init__(self, controller: SharedController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self._pending_select: uuid.UUID | None = None
        space = theme.SPACE
        # Hooks (tests replace them): where to save a copy, which file to upload, and asking
        # before leaving a folder.
        self.choose_save_path: Callable[[str], Path | None] = self._ask_save_path
        self.choose_source: Callable[[], Path | None] = self._ask_source
        self.confirm: Callable[[str, str, str], bool] = lambda title, text, action: confirm(
            self, title, text, action
        )

        title = QLabel("Shared with me")
        title.setProperty("textRole", "title")
        self._add = QPushButton("&Add to my DropShare")
        self._add.setProperty("variant", "primary")
        self._leave = QPushButton("L&eave…")
        self._save = QPushButton("Save a &copy…")
        self._upload = QPushButton("&Upload a new version…")
        self._link = QPushButton("Copy &link")
        self._refresh = QPushButton("Re&fresh")
        self._refresh.setToolTip("Refresh (F5)")
        actions = QHBoxLayout()
        for button in (self._add, self._leave, self._save, self._upload, self._link):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self._refresh)

        self._model = SharedModel()
        self._table = QTableView()
        self._table.setObjectName("sharedTable")
        self._table.setAccessibleName("Shared with me")
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.verticalHeader().hide()
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in (OWNER, ROLE, SHARED, IN_DROPSHARE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        self._view = StatefulView(self._table)

        # A running transfer (Save a copy, Upload a new version): what, how far, and Cancel.
        self._transfer_text = QLabel()
        self._transfer_bar = QProgressBar()
        self._transfer_bar.setAccessibleName("Transfer progress")
        self._transfer_bar.setTextVisible(False)
        self._cancel = QPushButton("Ca&ncel")
        transfer_row = QHBoxLayout()
        transfer_row.setContentsMargins(0, 0, 0, 0)
        transfer_row.addWidget(self._transfer_text)
        transfer_row.addWidget(self._transfer_bar, stretch=1)
        transfer_row.addWidget(self._cancel)
        self._transfer = QWidget()
        self._transfer.setLayout(transfer_row)
        self._transfer.hide()

        self._error = message_label("sharedError")
        self._status = status_label("sharedStatus")
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(_STATUS_MS)
        self._status_timer.timeout.connect(self._status.clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.md)
        layout.addWidget(title)
        layout.addLayout(actions)
        layout.addWidget(self._error)
        layout.addWidget(self._view, stretch=1)
        layout.addWidget(self._transfer)
        layout.addWidget(self._status)

        refresh = QAction("Re&fresh", self)
        refresh.setShortcut(QKeySequence(QKeySequence.StandardKey.Refresh))
        refresh.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        refresh.triggered.connect(controller.refresh)
        self.addAction(refresh)

        self._add.clicked.connect(lambda: self._with_item(controller.add_to_dropshare))
        self._leave.clicked.connect(self._ask_leave)
        self._save.clicked.connect(self._save_copy)
        self._upload.clicked.connect(self._upload_version)
        self._link.clicked.connect(self._copy_link)
        self._refresh.clicked.connect(controller.refresh)
        self._cancel.clicked.connect(controller.cancel)
        self._view.retry_requested.connect(controller.refresh)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self._update())
        self._table.activated.connect(self._activated)
        self._table.customContextMenuRequested.connect(self._menu)
        controller.loading.connect(self._loading)
        controller.loaded.connect(self._show)
        controller.failed.connect(self._view.show_error)
        controller.busy_changed.connect(self._busy)
        controller.succeeded.connect(self._announce)
        controller.action_failed.connect(lambda message: show_message(self._error, message))
        controller.transfer_started.connect(self._transfer_began)
        controller.transfer_progressed.connect(self._transfer_moved)
        self._show()

    @property
    def table(self) -> QTableView:
        return self._table

    @property
    def state(self) -> str:
        return self._view.current_state()

    def select(self, node_id: uuid.UUID) -> None:
        """Select an item (a link to it was opened), now or once it has loaded."""
        row = self._model.row_of(node_id)
        if row is None:
            self._pending_select = node_id
            self._controller.refresh()
            return
        self._pending_select = None
        self._table.selectRow(row)
        self._table.scrollTo(self._model.index(row, NAME))
        self._table.setFocus()

    def _loading(self, keep: bool) -> None:
        if not keep:
            self._view.show_loading("Loading what's shared with you…", keep_content=False)

    def _show(self) -> None:
        items = self._controller.items()
        self._model.set_rows(items)
        if items:
            self._view.show_content()
        else:
            self._view.show_empty(
                "Nothing is shared with you yet. When someone shares a folder or file with "
                "you, it appears here, and you're told."
            )
        if (
            self._pending_select is not None
            and self._model.row_of(self._pending_select) is not None
        ):
            self.select(self._pending_select)
        self._update()

    def _selected(self) -> SharedItemOut | None:
        rows = self._table.selectionModel().selectedRows()
        return self._model.item_at(rows[0].row()) if rows else None

    def _update(self) -> None:
        item = self._selected()
        idle = not self._controller.busy
        folder = item is not None and item.kind == NodeKind.FOLDER
        file = item is not None and item.kind == NodeKind.FILE
        added = folder and item is not None and item.mount_node_id is not None
        editor = file and item is not None and item.role == ShareRole.EDITOR
        self._add.setEnabled(idle and folder and not added)
        self._leave.setEnabled(idle and added)
        self._save.setEnabled(idle and file)
        self._upload.setEnabled(idle and editor)
        self._link.setEnabled(item is not None)
        view_only = file and not editor
        self._upload.setToolTip("You can view this file but not change it." if view_only else "")

    def _busy(self, busy: bool) -> None:
        if not busy:
            self._transfer.hide()
        self._update()

    # --- keyboard and menu ------------------------------------------------------------------

    def _activated(self, index: QModelIndex) -> None:
        """Enter or double-click: the obvious thing for that item."""
        item = self._model.item_at(index.row())
        if item is None:
            return
        if item.kind == NodeKind.FOLDER and item.mount_node_id is None:
            self._add.click()
        elif item.kind == NodeKind.FILE:
            self._save.click()

    def _menu(self, position: QPoint) -> None:
        menu = QMenu(self._table)
        for button in (self._add, self._leave, self._save, self._upload, self._link):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            action.triggered.connect(button.click)
        menu.addSeparator()
        menu.addAction(self._refresh.text()).triggered.connect(self._controller.refresh)
        current = self._table.currentIndex()
        if current.isValid() and not self._table.indexAt(position).isValid():
            position = self._table.visualRect(current).bottomLeft()  # the menu key
        menu.popup(self._table.viewport().mapToGlobal(position))

    # --- actions -----------------------------------------------------------------------------

    def _with_item(self, act: Callable[[SharedItemOut], None]) -> None:
        item = self._selected()
        if item is not None:
            self._clear_messages()
            act(item)

    def _ask_leave(self) -> None:
        item = self._selected()
        if item is not None and self.confirm(
            "Leave shared folder", messages.leave_folder(item.name), "Leave folder"
        ):
            self._clear_messages()
            self._controller.leave(item)

    def _save_copy(self) -> None:
        item = self._selected()
        if item is None:
            return
        target = self.choose_save_path(item.name)
        if target is not None:
            self._clear_messages()
            self._controller.save_copy(item, target)

    def _upload_version(self) -> None:
        item = self._selected()
        if item is None:
            return
        source = self.choose_source()
        if source is not None:
            self._clear_messages()
            self._controller.upload_version(item, source)

    def _copy_link(self) -> None:
        item = self._selected()
        if item is not None:
            QGuiApplication.clipboard().setText(self._controller.link(item))
            self._announce(messages.LINK_COPIED)

    def _transfer_began(self, what: str) -> None:
        self._transfer_text.setText(what)
        self._transfer_bar.setRange(0, 0)  # busy until the first progress
        self._transfer.show()
        announce(self._status, what)

    def _transfer_moved(self, done: object, total: object) -> None:
        if isinstance(done, int) and isinstance(total, int) and total > 0:
            percent = min(100, done * 100 // total)
            self._transfer_bar.setRange(0, 100)
            self._transfer_bar.setValue(percent)
            self._transfer_bar.setAccessibleDescription(f"{percent} percent")

    def _announce(self, text: str) -> None:
        show_message(self._error, "")
        announce(self._status, text)
        self._status_timer.start()

    def _clear_messages(self) -> None:
        show_message(self._error, "")
        announce(self._status, "")

    def _ask_save_path(self, name: str) -> Path | None:
        # The copy never replaces a file (Sharing.save_copy), so don't offer to.
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save a copy", name, options=QFileDialog.Option.DontConfirmOverwrite
        )
        return Path(chosen) if chosen else None

    def _ask_source(self) -> Path | None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Upload a new version")
        return Path(chosen) if chosen else None
