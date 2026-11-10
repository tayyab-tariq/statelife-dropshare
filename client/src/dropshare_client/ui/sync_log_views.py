"""The Activity view (FR-CLIENT-03) and the Can't sync view (FR-SYNC-07). Each loads when
opened and keeps itself up to date while syncing; loading, error with Retry, empty and
populated states (NFR-UX-01)."""

from PySide6.QtCore import QAbstractTableModel, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.sync_log_controller import (
    ActivityController,
    CantSyncController,
)
from dropshare_client.ui import theme
from dropshare_client.ui.forms import (
    announce,
    clear_reload_message,
    message_label,
    show_message,
    status_label,
)
from dropshare_client.ui.stateful_view import StatefulView
from dropshare_client.ui.sync_log_models import (
    CANT_SYNC_ITEM,
    CANT_SYNC_REASON,
    DETAIL,
    ITEM,
    WHAT,
    WHEN,
    ActivityModel,
    CantSyncModel,
)

_STATUS_MS = 8000


def _table(name: str, model: QAbstractTableModel, *, many: bool) -> QTableView:
    table = QTableView()
    table.setObjectName(name.replace(" ", "").replace("'", ""))
    table.setAccessibleName(name)
    table.setModel(model)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(
        QAbstractItemView.SelectionMode.ExtendedSelection
        if many
        else QAbstractItemView.SelectionMode.SingleSelection
    )
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideMiddle)  # keeps the extension
    table.verticalHeader().hide()
    header = table.horizontalHeader()
    header.setHighlightSections(False)
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return table


class _LogView(QWidget):
    """Title, actions, the table in its four states, and a line for messages."""

    def __init__(
        self,
        title: str,
        table: QTableView,
        buttons: list[QPushButton],
        parent: QWidget | None,
    ) -> None:
        super().__init__(parent)
        space = theme.SPACE
        heading = QLabel(title)
        heading.setProperty("textRole", "title")
        self._refresh = QPushButton("Re&fresh")
        self._refresh.setToolTip("Refresh (F5)")
        actions = QHBoxLayout()
        for button in buttons:
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self._refresh)
        self._table = table
        self._view = StatefulView(table)
        self._error = message_label(f"{table.objectName()}Error")
        self._status = status_label(f"{table.objectName()}Status")
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(_STATUS_MS)
        self._status_timer.timeout.connect(self._status.clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.md)
        layout.addWidget(heading)
        layout.addLayout(actions)
        layout.addWidget(self._error)
        layout.addWidget(self._view, stretch=1)
        layout.addWidget(self._status)

        refresh = QAction("Re&fresh", self)
        refresh.setShortcut(QKeySequence(QKeySequence.StandardKey.Refresh))
        refresh.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        refresh.triggered.connect(self.refresh)
        self.addAction(refresh)
        self._refresh.clicked.connect(self.refresh)
        self._view.retry_requested.connect(self.refresh)
        table.selectionModel().selectionChanged.connect(lambda *_: self._update())

    @property
    def table(self) -> QTableView:
        return self._table

    @property
    def state(self) -> str:
        return self._view.current_state()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        """Not polled: it loads whenever it is opened, then follows the engine's reports."""
        super().showEvent(event)
        self.refresh()

    def refresh(self) -> None:
        show_message(self._error, "")
        self._load()

    def _load(self) -> None:
        raise NotImplementedError

    def _update(self) -> None:
        raise NotImplementedError

    def _selected_rows(self) -> list[int]:
        return sorted(index.row() for index in self._table.selectionModel().selectedRows())

    def _failed(self, message: str) -> None:
        show_message(self._error, message)

    def _reload_failed(self, message: str) -> None:
        show_message(self._error, message, from_reload=True)

    def _reloaded(self) -> None:
        clear_reload_message(self._error)

    def _announce(self, text: str) -> None:
        show_message(self._error, "")
        announce(self._status, text)
        self._status_timer.start()


class ActivityView(_LogView):
    def __init__(self, controller: ActivityController, parent: QWidget | None = None) -> None:
        model = ActivityModel()
        open_button = QPushButton("&Open")
        open_button.setProperty("variant", "primary")
        show_in_folder = QPushButton("Show in fol&der")
        table = _table(messages.ACTIVITY, model, many=False)
        super().__init__(messages.ACTIVITY, table, [open_button, show_in_folder], parent)
        self._controller = controller
        self._model = model
        self._open = open_button
        self._show_in_folder = show_in_folder
        header = table.horizontalHeader()
        for column in (WHEN, WHAT):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (ITEM, DETAIL):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)

        self._open.clicked.connect(self._open_current)
        self._show_in_folder.clicked.connect(self._show_current)
        table.activated.connect(lambda index: self._open_row(index.row()))
        controller.loading.connect(
            lambda: self._view.show_loading(messages.LOADING_ACTIVITY, keep_content=False)
        )
        controller.loaded.connect(self._show)
        controller.loaded.connect(self._reloaded)
        controller.failed.connect(self._view.show_error)
        controller.reload_failed.connect(self._reload_failed)
        controller.action_failed.connect(self._failed)
        controller.open_file.connect(
            lambda path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        )
        self._update()

    def _load(self) -> None:
        self._controller.refresh()

    def _show(self) -> None:
        rows = self._controller.rows()
        self._model.set_rows(rows)
        if rows:
            self._view.show_content()
        else:
            self._view.show_empty(messages.ACTIVITY_EMPTY)
        self._update()

    def _current_row(self) -> int:
        rows = self._selected_rows()
        return rows[0] if rows else -1

    def _update(self) -> None:
        row = self._model.row_at(self._current_row())
        openable = row is not None and row.can_open
        self._open.setEnabled(openable)
        self._show_in_folder.setEnabled(openable)

    def _open_row(self, number: int) -> None:
        row = self._model.row_at(number)
        if row is not None and row.can_open:
            self._controller.open(row.path)

    def _open_current(self) -> None:
        self._open_row(self._current_row())

    def _show_current(self) -> None:
        row = self._model.row_at(self._current_row())
        if row is not None and row.can_open:
            self._controller.show(row.path)


class CantSyncView(_LogView):
    def __init__(self, controller: CantSyncController, parent: QWidget | None = None) -> None:
        model = CantSyncModel()
        retry = QPushButton("&Try again")
        retry.setProperty("variant", "primary")
        retry.setToolTip("Look at the selected items again, after fixing them")
        show_in_folder = QPushButton("Show in fol&der")
        table = _table(messages.CANT_SYNC, model, many=True)
        super().__init__(messages.CANT_SYNC, table, [retry, show_in_folder], parent)
        self._controller = controller
        self._model = model
        self._retry = retry
        self._show_in_folder = show_in_folder
        header = table.horizontalHeader()
        header.setSectionResizeMode(CANT_SYNC_ITEM, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(CANT_SYNC_REASON, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(CANT_SYNC_ITEM, theme.STATE_MAX_WIDTH // 2)

        self._retry.clicked.connect(self._retry_selected)
        self._show_in_folder.clicked.connect(self._show_current)
        table.activated.connect(lambda index: self._show_row(index.row()))
        controller.loading.connect(
            lambda: self._view.show_loading(messages.LOADING_CANT_SYNC, keep_content=False)
        )
        controller.loaded.connect(self._show)
        controller.loaded.connect(self._reloaded)
        controller.failed.connect(self._view.show_error)
        controller.reload_failed.connect(self._reload_failed)
        controller.action_failed.connect(self._failed)
        controller.retried.connect(self._announce)
        self._update()

    def _load(self) -> None:
        self._controller.refresh()

    def _show(self) -> None:
        rows = self._controller.rows()
        self._model.set_rows(rows)
        if rows:
            self._view.show_content()
        else:
            self._view.show_empty(messages.CANT_SYNC_EMPTY)
        self._update()

    def _update(self) -> None:
        chosen = self._selected_rows()
        self._retry.setEnabled(bool(chosen))
        self._show_in_folder.setEnabled(len(chosen) == 1)

    def _retry_selected(self) -> None:
        rows = [row for n in self._selected_rows() if (row := self._model.row_at(n)) is not None]
        self._controller.retry(rows)

    def _show_row(self, number: int) -> None:
        row = self._model.row_at(number)
        if row is not None:
            self._controller.show(row.path)

    def _show_current(self) -> None:
        chosen = self._selected_rows()
        if len(chosen) == 1:
            self._show_row(chosen[0])
