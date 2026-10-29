"""The Transfers view (FR-CLIENT-08): each upload and download with a progress bar, what's
queued or waiting, the last 50 finished, the overall summary, and Retry for a failed one.

Its rows come from the sync engine's own events, not a request, so it has no loading or
error state: it is empty or populated (NFR-UX-01)."""

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStackedWidget,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers.transfers_controller import TransfersController, TransferStatus
from dropshare_client.ui import theme
from dropshare_client.ui.transfers_model import (
    DIRECTION,
    NAME,
    PERCENT_ROLE,
    PROGRESS,
    SIZE,
    STATUS,
    TransfersModel,
)

_ROW_HEIGHT = 36  # visual-design.md §3: file rows
_PROGRESS_HEIGHT = 6
_EMPTY = "Nothing is uploading or downloading right now. New and changed files appear here."
_IDLE = "Nothing to sync right now"
_RETRYABLE = (TransferStatus.FAILED, TransferStatus.WAITING)
# Progress doesn't fade when the window loses focus (the inactive highlight is muted).
_ACTIVE = QPalette.ColorGroup.Active


class _ProgressDelegate(QStyledItemDelegate):
    """A thin progress bar (visual-design.md §4: 6 px, radius 3), in palette colours so it
    follows light, dark and high contrast. The percent is also in the Status column, so the
    bar is never the only way to read it."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        super().paint(painter, option, index)  # selection and hover background
        percent = index.data(PERCENT_ROLE)
        if not isinstance(percent, int):
            return
        cell = option.rect.adjusted(theme.SPACE.sm, 0, -theme.SPACE.sm, 0)
        track = QRectF(
            cell.left(), cell.center().y() - _PROGRESS_HEIGHT / 2, cell.width(), _PROGRESS_HEIGHT
        )
        radius = _PROGRESS_HEIGHT / 2
        # The app palette: the table's own Highlight is the pale selection colour (QSS).
        palette = QGuiApplication.palette()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color(_ACTIVE, QPalette.ColorRole.Mid))
        painter.drawRoundedRect(track, radius, radius)
        if percent > 0:
            filled = QRectF(track)
            filled.setWidth(max(track.height(), track.width() * percent / 100))
            painter.setBrush(palette.color(_ACTIVE, QPalette.ColorRole.Highlight))
            painter.drawRoundedRect(filled, radius, radius)
        painter.restore()

    def displayText(self, value: object, locale: object) -> str:  # noqa: N802 - Qt API
        return ""  # the bar is the content; the percent is in the Status column


class TransfersView(QWidget):
    def __init__(self, controller: TransfersController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        space = theme.SPACE

        title = QLabel("Transfers")
        title.setProperty("textRole", "title")
        self._summary = QLabel()
        self._summary.setProperty("textRole", "caption")

        self._model = TransfersModel()
        self._table = QTableView()
        self._table.setObjectName("transferTable")
        self._table.setAccessibleName("Transfers")
        self._table.setModel(self._model)
        self._table.setItemDelegateForColumn(PROGRESS, _ProgressDelegate(self._table))
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._table.verticalHeader().hide()
        self._table.verticalHeader().setDefaultSectionSize(_ROW_HEIGHT)
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in (DIRECTION, SIZE):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(PROGRESS, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(PROGRESS, theme.PROGRESS_COLUMN_WIDTH)
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(STATUS, QHeaderView.ResizeMode.Stretch)

        self._retry = QPushButton("&Retry")
        self._retry.setAccessibleName("Retry")
        self._retry.setToolTip("Try the selected transfer again now")
        self._retry.setEnabled(False)
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self._retry)

        self._content = QWidget()
        table_layout = QVBoxLayout(self._content)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.setSpacing(space.md)
        table_layout.addWidget(self._table, stretch=1)
        table_layout.addLayout(actions)
        self._empty = QLabel(_EMPTY)
        self._empty.setObjectName("transfers_empty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)
        self._view = QStackedWidget()
        self._view.addWidget(self._empty)
        self._view.addWidget(self._content)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.sm)
        layout.addWidget(title)
        layout.addWidget(self._summary)
        layout.addSpacing(space.md)
        layout.addWidget(self._view, stretch=1)

        self._retry.clicked.connect(self._retry_selected)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self._update_retry())
        controller.rows_changed.connect(self._refresh)
        controller.summary_changed.connect(self._show_summary)
        self._show_summary(controller.summary)
        self._refresh()

    def _refresh(self) -> None:
        selected = self._selected_key()
        self._model.set_rows(self._controller.rows())
        self._view.setCurrentWidget(self._content if self._model.rowCount() else self._empty)
        row = self._model.row_of(selected) if selected is not None else None
        if row is not None and row != self._table.currentIndex().row():
            self._table.selectRow(row)
        self._update_retry()

    def _show_summary(self, summary: str) -> None:
        text = f"Syncing {summary}" if summary else _IDLE
        self._summary.setText(text)
        self._summary.setAccessibleName(f"Transfers summary: {text}")

    def _selected_key(self) -> str | None:
        rows = self._table.selectionModel().selectedRows()
        event = self._model.event_at(rows[0].row()) if rows else None
        return None if event is None else event.key

    def _update_retry(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        event = self._model.event_at(rows[0].row()) if rows else None
        self._retry.setEnabled(event is not None and event.status in _RETRYABLE)

    def _retry_selected(self) -> None:
        key = self._selected_key()
        if key is not None:
            self._controller.retry(key)
