"""The Trash (FR-TRASH-01 … 03): what was deleted, with Restore, Delete for good and Empty
trash. Loading, error with Retry, empty and populated states (NFR-UX-01)."""

from collections.abc import Callable

from PySide6.QtCore import QEvent, QModelIndex, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QAction, QContextMenuEvent, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from dropshare_client.controllers.trash_controller import TrashController, TrashItemOut
from dropshare_client.ui import theme
from dropshare_client.ui.forms import announce, confirm, message_label, show_message, status_label
from dropshare_client.ui.stateful_view import StatefulView
from dropshare_client.ui.trash_model import DELETED, DELETED_BY, LOCATION, NAME, SIZE, TrashModel

_STATUS_MS = 8000


class TrashView(QWidget):
    def __init__(self, controller: TrashController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        space = theme.SPACE
        # Hook (tests replace it): ask before deleting anything for good.
        self.confirm: Callable[[str, str, str], bool] = lambda title, text, action: confirm(
            self, title, text, action
        )

        title = QLabel(messages.TRASH)
        title.setProperty("textRole", "title")
        self._restore = QPushButton("&Restore")
        self._restore.setProperty("variant", "primary")
        self._delete = QPushButton("&Delete permanently…")
        self._delete.setProperty("variant", "danger")
        self._empty = QPushButton("&Empty trash…")
        self._empty.setProperty("variant", "danger")
        self._refresh = QPushButton("Re&fresh")
        self._refresh.setToolTip("Refresh (F5)")
        self._cancel = QPushButton("Ca&ncel")
        self._cancel.hide()  # while an action runs
        actions = QHBoxLayout()
        for button in (self._restore, self._delete, self._cancel):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self._empty)
        actions.addWidget(self._refresh)
        self._return_to: int | None = None  # the row to select once an action's reload lands
        self._keyboard_menu = False

        self._model = TrashModel()
        self._table = QTableView()
        self._table.setObjectName("trashTable")
        self._table.setAccessibleName(messages.TRASH)
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)  # keeps the extension
        self._table.installEventFilter(self)  # to tell a menu-key menu from a mouse one
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.verticalHeader().hide()
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in (DELETED, DELETED_BY, SIZE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (NAME, LOCATION):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self._view = StatefulView(self._table)

        self._error = message_label("trashError")
        self._status = status_label("trashStatus")
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
        layout.addWidget(self._status)

        refresh = QAction("Re&fresh", self)
        refresh.setShortcut(QKeySequence(QKeySequence.StandardKey.Refresh))
        refresh.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        refresh.triggered.connect(controller.refresh)
        self.addAction(refresh)
        delete = QAction(messages.DELETE_FOREVER, self._table)
        delete.setShortcut(QKeySequence(QKeySequence.StandardKey.Delete))
        delete.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        delete.triggered.connect(self._ask_delete)  # also says why when it can't
        self._table.addAction(delete)
        self._shortcuts = {"refresh": refresh, "delete": delete}

        self._restore.clicked.connect(self._restore_selected)
        self._delete.clicked.connect(self._ask_delete)
        self._empty.clicked.connect(self._ask_empty)
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
        controller.progressed.connect(lambda text: announce(self._status, text))
        controller.cleared.connect(self._cleared)
        self._show()

    @property
    def table(self) -> QTableView:
        return self._table

    @property
    def state(self) -> str:
        return self._view.current_state()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        """Not polled: it loads whenever it is opened."""
        super().showEvent(event)
        self._controller.refresh()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self._table and event.type() == QEvent.Type.ContextMenu:
            assert isinstance(event, QContextMenuEvent)  # noqa: S101 - its type says so
            self._keyboard_menu = event.reason() == QContextMenuEvent.Reason.Keyboard
        return False

    def _loading(self, keep: bool) -> None:
        if not keep:
            self._view.show_loading("Loading the trash…", keep_content=False)

    def _show(self) -> None:
        items = self._controller.items()
        self._model.set_rows(items)
        if items:
            self._view.show_content()
        else:
            self._view.show_empty(messages.TRASH_EMPTY)
        if self._return_to is not None and not self._controller.busy:
            row, self._return_to = min(self._return_to, len(items) - 1), None
            if row >= 0:  # the keyboard user keeps their place (the acted-on rows are gone)
                self._table.selectRow(row)
                self._table.setFocus()
        self._update()

    def _cleared(self) -> None:
        self._table.clearSelection()
        self._return_to = None
        self._clear_messages()

    def _busy(self, busy: bool) -> None:
        self._cancel.setVisible(busy)
        self._update()

    def _selected(self) -> list[TrashItemOut]:
        rows = sorted(index.row() for index in self._table.selectionModel().selectedRows())
        return [item for row in rows if (item := self._model.item_at(row)) is not None]

    def _update(self) -> None:
        chosen = self._selected()
        idle = not self._controller.busy
        deletable = [item for item in chosen if item.can_delete]
        self._restore.setEnabled(idle and bool(chosen))
        self._delete.setEnabled(idle and bool(deletable))
        only_others = bool(chosen) and not deletable
        why = messages.ONLY_OWNER_DELETES if only_others else ""
        self._delete.setToolTip(why)
        self._delete.setAccessibleDescription(why)
        self._empty.setEnabled(idle and any(item.can_delete for item in self._controller.items()))

    def _menu(self, position: QPoint) -> None:
        menu = QMenu(self._table)
        for button, shortcut in (
            (self._restore, QKeySequence(Qt.Key.Key_Return)),
            (self._delete, self._shortcuts["delete"].shortcut()),
            (self._empty, None),
        ):
            action = menu.addAction(button.text())
            action.setEnabled(button.isEnabled())
            if shortcut is not None:
                action.setShortcut(shortcut)  # shown in the menu; the real ones are elsewhere
            action.triggered.connect(button.click)
        menu.addSeparator()
        refresh = menu.addAction(self._refresh.text())
        refresh.setShortcut(self._shortcuts["refresh"].shortcut())
        refresh.triggered.connect(self._controller.refresh)
        current = self._table.currentIndex()
        if current.isValid() and (
            self._keyboard_menu or not self._table.indexAt(position).isValid()
        ):
            position = self._table.visualRect(current).bottomLeft()  # the menu key: at the row
        self._keyboard_menu = False
        menu.popup(self._table.viewport().mapToGlobal(position))

    # --- actions -----------------------------------------------------------------------------

    def _activated(self, index: QModelIndex) -> None:
        """Enter or double-click restores that row only, never a whole selection by accident."""
        item = self._model.item_at(index.row())
        if item is not None and self._restore.isEnabled():
            self._act(lambda: self._controller.restore([item]))

    def _restore_selected(self) -> None:
        chosen = self._selected()
        if chosen and self._restore.isEnabled():
            self._act(lambda: self._controller.restore(chosen))

    def _ask_delete(self) -> None:
        chosen = self._selected()
        deletable = [item for item in chosen if item.can_delete]
        if chosen and not deletable:  # the Delete key on items of others: say why, not nothing
            announce(self._status, messages.ONLY_OWNER_DELETES_SELECTED)
            return
        if not deletable or self._controller.busy:
            return
        names = [item.name for item in deletable]
        text = messages.confirm_delete_forever(names, kept=len(chosen) - len(deletable))
        if self.confirm("Delete permanently", text, "Delete permanently"):
            self._act(lambda: self._controller.delete_forever(deletable))

    def _act(self, start: Callable[[], None]) -> None:
        rows = [index.row() for index in self._table.selectionModel().selectedRows()]
        current = self._table.currentIndex().row()
        self._return_to = min(rows) if rows else max(current, 0)
        self._clear_messages()
        start()

    def _ask_empty(self) -> None:
        if self._empty.isEnabled() and self.confirm(
            "Empty trash", messages.CONFIRM_EMPTY_TRASH, "Empty trash"
        ):
            self._clear_messages()
            self._controller.empty()

    def _announce(self, text: str) -> None:
        show_message(self._error, "")
        announce(self._status, text)
        self._status_timer.start()

    def _clear_messages(self) -> None:
        show_message(self._error, "")
        announce(self._status, "")
