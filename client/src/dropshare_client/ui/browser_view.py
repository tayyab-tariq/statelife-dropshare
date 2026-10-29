"""My files (FR-FILE-01): the server's folders and files with breadcrumbs, loaded page by page,
each with its sync state, and the actions on them (FR-FILE-03/04/05), which work on the local
copy (OQ-23) and are off, with the reason shown, for items not on this computer yet (OQ-28).

Keyboard (ui.md §5): Enter opens, Backspace or Alt+Up goes up a level, F5 refreshes,
Ctrl+Shift+N makes a folder, F2 renames, Delete moves to the Recycle Bin, and the menu key or
Shift+F10 opens the menu with every action, Upload included. Dragging onto a folder moves;
"Move to…" does too. Files dragged in from Explorer are uploaded (FR-FILE-02).
"""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAccessible,
    QAccessibleEvent,
    QAction,
    QDragEnterEvent,
    QDragMoveEvent,
    QDropEvent,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.browser_controller import BrowserController, BrowserRow
from dropshare_client.controllers.file_actions_controller import FileActionsController, Item
from dropshare_client.controllers.upload_controller import UploadController
from dropshare_client.ui import theme
from dropshare_client.ui.breadcrumb_bar import BreadcrumbBar
from dropshare_client.ui.browser_model import (
    MODIFIED,
    MODIFIED_BY,
    NAME,
    PATHS_MIME,
    SHARING,
    SIZE,
    SYNC,
    BrowserModel,
)
from dropshare_client.ui.file_dialogs import NameDialog, RecycleDialog
from dropshare_client.ui.forms import show_message
from dropshare_client.ui.stateful_view import StatefulView
from dropshare_client.ui.upload_flow import UploadFlow

_ROW_HEIGHT = 36  # visual-design.md §3: file rows
_EMPTY = "This folder is empty. Drag files here or choose Upload."
_EMPTY_NOT_HERE = (
    "This folder isn't on this computer yet, so nothing can be added to it here. Wait until "
    "it has synced."
)
_STATUS_MS = 6000  # how long a confirmation stays in the status line

# Dialog hooks, replaceable in tests: (title, button, current name) -> new name or None;
# names -> confirmed; (title, start folder) -> chosen folder or None.
type AskName = Callable[[str, str, str], str | None]
type ConfirmRecycle = Callable[[list[str]], bool]
type ChooseFolder = Callable[[str, Path | None], Path | None]


class _FileTable(QTableView):
    """Takes rows dragged within itself (moves) and files from Explorer (uploads); a drag of
    the internal format from anywhere else is refused (it would name paths to move)."""

    def _accepts(self, event: QDragMoveEvent | QDropEvent) -> bool:
        data = event.mimeData()
        return not data.hasFormat(PATHS_MIME) or event.source() is self

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        if self._accepts(event):
            super().dragEnterEvent(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802 - Qt override
        if self._accepts(event):
            super().dragMoveEvent(event)
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        if self._accepts(event):
            super().dropEvent(event)
        else:
            event.ignore()


class BrowserView(QWidget):
    share_requested = Signal(object, str)  # node ID, name: the shell opens the Share dialog

    def __init__(
        self,
        controller: BrowserController,
        actions: FileActionsController,
        uploads: UploadController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._actions = actions
        self.uploads = UploadFlow(uploads, self)
        self.ask_name: AskName = self._ask_name
        self.confirm_recycle: ConfirmRecycle = self._confirm_recycle
        self.choose_folder: ChooseFolder = self._choose_folder
        space = theme.SPACE

        self._crumbs = BreadcrumbBar()
        self._new_folder = QPushButton("&New folder")
        self._upload = QPushButton("U&pload")
        self._upload.setAccessibleName("Upload")
        self._upload.setProperty("variant", "primary")
        self._refresh = QPushButton()
        self._refresh.setAccessibleName("Refresh")
        self._refresh.setToolTip("Refresh (F5)")
        self._refresh.setProperty("variant", "subtle")
        self._refresh.setIconSize(QSize(theme.TOOLBAR_ICON_SIZE, theme.TOOLBAR_ICON_SIZE))
        top = QHBoxLayout()
        top.setSpacing(space.sm)
        top.addWidget(self._crumbs, stretch=1)
        top.addWidget(self._new_folder)
        top.addWidget(self._upload)
        top.addWidget(self._refresh)

        self._reason = QLabel()  # why actions are off, in words (not only in tooltips)
        self._reason.setObjectName("browser_reason")
        self._reason.setProperty("textRole", "caption")
        self._reason.setWordWrap(True)
        self._reason.hide()
        self._error = QLabel()
        self._error.setObjectName("browser_error")
        self._error.setProperty("textRole", "error")
        self._error.setWordWrap(True)
        self._error.hide()
        self._status = QLabel()
        self._status.setObjectName("browser_status")
        self._status.setProperty("textRole", "caption")
        self._status.setWordWrap(True)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(_STATUS_MS)
        self._status_timer.timeout.connect(self._status.clear)

        self._model = BrowserModel(controller)
        self._table = _FileTable()
        self._setup_table()
        self._view = StatefulView(self._table)
        self.setAcceptDrops(True)  # the empty page too: "Drag files here"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.md)
        layout.addLayout(top)
        layout.addWidget(self._reason)
        layout.addWidget(self._error)
        layout.addWidget(self._view, stretch=1)
        layout.addWidget(self._status)

        # Folder-level actions belong to the view (they work in an empty folder too);
        # item actions to the list (they need a selection).
        self._new_folder_action = self._action(self, "&New folder…", self._ask_new_folder)
        self._new_folder_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self._upload_files_action = self._action(self, "Upload &files…", self._upload_files)
        self._upload_folder_action = self._action(self, "Upload a f&older…", self._upload_folder)
        self._refresh_action = self._action(self, "Re&fresh", controller.refresh)
        self._refresh_action.setShortcut(QKeySequence(QKeySequence.StandardKey.Refresh))
        self._up_action = self._action(self, "&Up", controller.go_up)
        self._up_action.setShortcuts([QKeySequence(Qt.Key.Key_Backspace), QKeySequence("Alt+Up")])
        self._open_action = self._action(self._table, "&Open", self._open_current)
        # Enter opens on every platform (Qt leaves it to the platform; macOS doesn't).
        self._open_action.setShortcuts(
            [QKeySequence(Qt.Key.Key_Return), QKeySequence(Qt.Key.Key_Enter)]
        )
        self._rename_action = self._action(self._table, "&Rename…", self._ask_rename)
        self._rename_action.setShortcut(QKeySequence("F2"))
        self._move_action = self._action(self._table, "&Move to…", self._ask_move)
        self._recycle_action = self._action(self._table, "Move to Recycle &Bin…", self._ask_recycle)
        # Delete, plus the platform's own delete key if that is another one (on the Mac
        # they're the same: listing it twice would make the key ambiguous and do nothing).
        deletes = [QKeySequence(Qt.Key.Key_Delete)]
        deletes += [
            k for k in QKeySequence.keyBindings(QKeySequence.StandardKey.Delete) if k not in deletes
        ]
        self._recycle_action.setShortcuts(deletes)
        self._show_action = self._action(self._table, "&Show in folder", self._show_in_folder)
        self._copy_action = self._action(self._table, "Save a &copy to…", self._ask_save_copy)
        self._share_action = self._action(self._table, "S&hare…", self._share_current)
        self._share_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        upload_menu = QMenu(self._upload)
        upload_menu.addAction(self._upload_files_action)
        upload_menu.addAction(self._upload_folder_action)
        self._upload.setMenu(upload_menu)

        self._retint()
        # Re-tint when light/dark or contrast changes while running (visual-design.md §1);
        # connected to bound slots, so Qt disconnects them if this view goes away.
        hints = QGuiApplication.styleHints()
        hints.colorSchemeChanged.connect(self._retint)
        hints.accessibility().contrastPreferenceChanged.connect(self._retint)
        self._crumbs.up_requested.connect(controller.go_up)
        self._crumbs.crumb_chosen.connect(controller.open_crumb)
        self._new_folder.clicked.connect(self._new_folder_action.trigger)
        self._refresh.clicked.connect(controller.refresh)
        self._table.customContextMenuRequested.connect(self._menu)
        self._table.activated.connect(self._activated)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
        self._model.move_requested.connect(self._dropped)
        self._model.upload_requested.connect(self._dropped_files)
        self._view.retry_requested.connect(controller.refresh)
        self._view.empty_action_requested.connect(self._upload_files)
        actions.busy_changed.connect(lambda _: self._update_actions())
        actions.succeeded.connect(self._announce)
        actions.failed.connect(lambda message: show_message(self._error, message))
        actions.stale.connect(controller.refresh)
        uploads.busy_changed.connect(lambda _: self._update_actions())
        self.uploads.succeeded.connect(self._announce)
        self.uploads.failed.connect(lambda message: show_message(self._error, message))
        controller.location_changed.connect(self._show_location)
        controller.loading.connect(self._loading)
        controller.loaded.connect(self._show_rows)
        controller.failed.connect(self._failed)
        controller.notice.connect(self._announce)
        controller.reveal.connect(self._select_node)
        self._show_location()

    @property
    def table(self) -> QTableView:
        return self._table

    @property
    def state(self) -> str:
        """Which of the four states the list is in (NFR-UX-01)."""
        return self._view.current_state()

    def _setup_table(self) -> None:
        table = self._table
        table.setObjectName("fileTable")
        table.setAccessibleName("Files")
        table.setModel(self._model)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        table.setDragEnabled(True)
        table.setAcceptDrops(True)
        table.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        table.setDefaultDropAction(Qt.DropAction.MoveAction)
        table.setDragDropOverwriteMode(True)  # dropped onto a folder, not between rows
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in (MODIFIED, MODIFIED_BY, SIZE, SHARING, SYNC):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)

    def _action(self, owner: QWidget, text: str, slot: Callable[[], None]) -> QAction:
        """Its key works while `owner` (or anything in it) has focus."""
        action = QAction(text, owner)
        action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        action.triggered.connect(slot)
        owner.addAction(action)
        return action

    def _retint(self, *_: object) -> None:
        self._crumbs.retint()
        self._refresh.setIcon(theme.icon("arrow_clockwise"))
        self._table.viewport().update()  # file icons are tinted as they're painted

    # --- what can be done now --------------------------------------------------------------

    def _selected(self) -> list[BrowserRow]:
        rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        return [row for r in rows if (row := self._model.row_at(r)) is not None]

    def _items(self, rows: list[BrowserRow]) -> list[Item]:
        return [row.item for row in rows if row.item is not None]

    def _update_actions(self) -> None:
        idle = not self._actions.busy and not self.uploads.busy
        rows = self._selected()
        here = bool(rows) and all(row.item is not None for row in rows)
        single = len(rows) == 1
        folder_here = self._controller.current_folder is not None
        item_reason = "" if here or not rows else messages.NOT_HERE_YET
        self._open_action.setEnabled(single and rows[0].is_folder)
        # On the server, not the disk: an item not synced here yet can be shared too.
        self._share_action.setEnabled(single and rows[0].is_known_kind)
        for action, allowed in (
            (self._rename_action, single and here),
            (self._show_action, single and here),
            (self._copy_action, single and here),
            (self._move_action, here),
            (self._recycle_action, here),
        ):
            action.setEnabled(idle and allowed)
            action.setToolTip(item_reason)
        for action in (
            self._new_folder_action,
            self._upload_files_action,
            self._upload_folder_action,
        ):
            action.setEnabled(idle and folder_here)
        folder_reason = "" if folder_here else messages.NOT_HERE_YET
        for button in (self._new_folder, self._upload):
            button.setEnabled(idle and folder_here)
            button.setAccessibleDescription(folder_reason)
        self._new_folder.setToolTip("New folder (Ctrl+Shift+N)" if folder_here else folder_reason)
        self._upload.setToolTip(
            "Add files or a folder; you can also drag them here" if folder_here else folder_reason
        )
        reason = folder_reason or item_reason
        self._reason.setText(reason)
        self._reason.setVisible(bool(reason))

    def _menu(self, position: QPoint) -> None:
        menu = QMenu(self._table)
        menu.setToolTipsVisible(True)  # says why an action is off
        menu.addAction(self._open_action)
        menu.addSeparator()
        for action in (self._rename_action, self._move_action, self._recycle_action):
            menu.addAction(action)
        menu.addSeparator()
        menu.addAction(self._show_action)
        menu.addAction(self._copy_action)
        menu.addAction(self._share_action)
        menu.addSeparator()
        for action in (
            self._new_folder_action,
            self._upload_files_action,
            self._upload_folder_action,
            self._refresh_action,
        ):
            menu.addAction(action)
        current = self._table.currentIndex()
        if current.isValid() and not self._table.indexAt(position).isValid():
            # Not over a row (the menu key, Shift+F10): open it at the current row.
            position = self._table.visualRect(current).bottomLeft()
        menu.popup(self._table.viewport().mapToGlobal(position))

    # --- the actions (FR-FILE-03/04/05) ------------------------------------------------------

    def _select_node(self, node_id: object) -> None:
        """A link opened at this item: select it and bring it into view."""
        for row in range(self._model.rowCount()):
            found = self._model.row_at(row)
            if found is not None and found.node.id == node_id:
                self._table.selectRow(row)
                self._table.scrollTo(self._model.index(row, 0))
                self._table.setFocus()
                return

    def _share_current(self) -> None:
        rows = self._selected()
        if len(rows) == 1 and self._share_action.isEnabled():
            self.share_requested.emit(rows[0].node.id, rows[0].node.name or "")

    def _ask_new_folder(self) -> None:
        folder = self._controller.current_folder
        if folder is None or not self._new_folder_action.isEnabled():
            return
        name = self.ask_name("New folder", "Create folder", "")
        if name:
            self._clear_messages()
            self._actions.new_folder(folder if folder.path else None, name)

    def _ask_rename(self) -> None:
        rows = self._selected()
        item = rows[0].item if len(rows) == 1 else None
        if item is None or not self._rename_action.isEnabled():
            return
        current = item.path.rpartition("/")[2]
        name = self.ask_name("Rename", "Rename", current)
        if name and name != current:
            self._clear_messages()
            self._actions.rename(item, name)

    def _ask_move(self) -> None:
        items = self._items(self._selected())
        if not items or not self._move_action.isEnabled():
            return
        folder = self.choose_folder("Move to", self._actions.sync_root)
        if folder is not None:
            self._clear_messages()
            self._actions.move_to_disk_folder(items, folder)

    def _ask_recycle(self) -> None:
        rows = self._selected()
        items = self._items(rows)
        if not items or not self._recycle_action.isEnabled():
            return
        if self.confirm_recycle([row.node.name or "" for row in rows]):
            self._clear_messages()
            self._actions.delete(items)

    def _show_in_folder(self) -> None:
        items = self._items(self._selected())
        if len(items) == 1 and self._show_action.isEnabled():
            self._actions.show(items[0])

    def _ask_save_copy(self) -> None:
        items = self._items(self._selected())
        if len(items) != 1 or not self._copy_action.isEnabled():
            return
        folder = self.choose_folder("Save a copy to", None)
        if folder is not None:
            self._clear_messages()
            self._actions.save_copy(items[0], folder)

    def _dropped(self, items: list[Item], folder: Item) -> None:
        if self._actions.busy:
            show_message(self._error, messages.ACTION_ALREADY_RUNNING)
            return
        self._clear_messages()
        self._actions.move(items, folder if folder.path else None)

    # --- upload (FR-FILE-02) -----------------------------------------------------------------

    def _upload_files(self) -> None:
        folder = self._controller.current_folder
        if folder is None:
            show_message(self._error, messages.NOT_HERE_YET)
            return
        self._clear_messages()
        self.uploads.upload_files(folder)

    def _upload_folder(self) -> None:
        folder = self._controller.current_folder
        if folder is not None:
            self._clear_messages()
            self.uploads.upload_folder(folder)

    def _dropped_files(self, paths: list[Path], folder: Item) -> None:
        self._clear_messages()
        self.uploads.upload(paths, folder)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt override
        """Files from Explorer dropped anywhere on My files (the empty page too)."""
        if event.mimeData().hasUrls() and self._controller.current_folder is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt override
        folder = self._controller.current_folder
        files = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if folder is None or not files:
            event.ignore()
            return
        event.acceptProposedAction()
        self._dropped_files(files, folder)

    # --- the list's states -----------------------------------------------------------------

    def _open_current(self) -> None:
        self._activated(self._table.currentIndex())

    def _activated(self, index: QModelIndex) -> None:
        row = self._model.row_at(index.row())
        if row is not None:
            self._controller.open(row)

    def _loading(self, keep: bool) -> None:
        self._view.show_loading("Loading…", keep_content=keep)

    def _show_rows(self) -> None:
        if self._model.rowCount() == 0:
            if not self._controller.crumbs():
                return  # signed out, or still finding My files: loading or blank
            if self._controller.current_folder is None:
                self._view.show_empty(_EMPTY_NOT_HERE)
            else:
                self._view.show_empty(_EMPTY, "Upload files…")
        else:
            self._view.show_content()
            if not self._table.currentIndex().isValid():
                self._table.setCurrentIndex(self._model.index(0, NAME))
        self._update_actions()

    def _failed(self, message: str) -> None:
        """Keep what's shown when a refresh fails (ui.md §3); the full error page only when
        there is nothing to show."""
        if self._model.rowCount():
            show_message(self._error, message)
        else:
            self._view.show_error(message)

    def _show_location(self) -> None:
        self._crumbs.show_path([crumb.name for crumb in self._controller.crumbs()])
        chain = [*self._crumbs.focus_chain(), self._new_folder, self._upload, self._refresh]
        for before, after in zip(chain, [*chain[1:], self._table], strict=True):
            QWidget.setTabOrder(before, after)
        self._clear_messages()
        self._update_actions()

    def _announce(self, message: str) -> None:
        """A confirmation in the status line, read out by screen readers too."""
        show_message(self._error, "")
        self._status.setText(message)
        if message:
            QAccessible.updateAccessibility(QAccessibleEvent(self._status, QAccessible.Event.Alert))
            self._status_timer.start()

    def _clear_messages(self) -> None:
        show_message(self._error, "")
        self._status.clear()

    # --- default dialogs -----------------------------------------------------------------

    def _ask_name(self, title: str, action: str, current: str) -> str | None:
        dialog = NameDialog(title, action, current, parent=self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.name if accepted else None

    def _confirm_recycle(self, names: list[str]) -> bool:
        return RecycleDialog(names, parent=self).exec() == QDialog.DialogCode.Accepted

    def _choose_folder(self, title: str, start: Path | None) -> Path | None:
        chosen = QFileDialog.getExistingDirectory(self, title, str(start) if start else "")
        return Path(chosen) if chosen else None
