"""Rows of the Transfers view (FR-CLIENT-08), one per file, identified by its key."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from dropshare_client.sync.transfers import Direction, TransferEvent, TransferStatus
from dropshare_client.ui.format import size_text

type _Index = QModelIndex | QPersistentModelIndex

NAME, DIRECTION, PROGRESS, SIZE, STATUS = range(5)
_HEADERS = ("Name", "Direction", "Progress", "Size", "Status")
_DIRECTION = {Direction.UPLOAD: "Upload", Direction.DOWNLOAD: "Download"}
PERCENT_ROLE = Qt.ItemDataRole.UserRole + 1  # int 0-100, for the progress bar delegate
EVENT_ROLE = Qt.ItemDataRole.UserRole + 2  # the TransferEvent itself


def status_text(event: TransferEvent) -> str:
    match event.status:
        case TransferStatus.ACTIVE:
            return f"{event.percent}%"
        case TransferStatus.QUEUED:
            return "Queued"
        case TransferStatus.WAITING:
            return f"Waiting: {event.reason}" if event.reason else "Waiting"
        case TransferStatus.DONE:
            return "Done"
        case TransferStatus.FAILED:
            return f"Failed: {event.reason}" if event.reason else "Failed"


def _name(event: TransferEvent) -> str:
    return event.path.rpartition("/")[2]


class TransfersModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[TransferEvent] = []

    def set_rows(self, rows: list[TransferEvent]) -> None:
        """Same files in the same order: only changed rows repaint. Otherwise the list is
        replaced; the view keeps its selection by key."""
        if [r.key for r in rows] == [r.key for r in self._rows]:
            old, self._rows = self._rows, rows
            for row, (before, after) in enumerate(zip(old, rows, strict=True)):
                if before != after:
                    self.dataChanged.emit(self.index(row, 0), self.index(row, len(_HEADERS) - 1))
            return
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def event_at(self, row: int) -> TransferEvent | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_of(self, key: str) -> int | None:
        return next((i for i, r in enumerate(self._rows) if r.key == key), None)

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        event = self.event_at(index.row()) if index.isValid() else None
        if event is None:
            return None
        column = index.column()
        if role == EVENT_ROLE:
            return event
        if role == PERCENT_ROLE:
            return event.percent
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{event.path}\n{status_text(event)}"
        if role == Qt.ItemDataRole.AccessibleTextRole and column == PROGRESS:
            return f"{event.percent} percent"
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return None
        if column == NAME:
            return _name(event)
        if column == DIRECTION:
            return _DIRECTION[event.direction]
        if column == PROGRESS:
            return f"{event.percent}%"
        if column == SIZE:
            if event.status is TransferStatus.ACTIVE:
                return f"{size_text(event.done)} of {size_text(event.total)}"
            return size_text(event.total)
        return status_text(event)
