"""The rows of the Activity view (FR-CLIENT-03) and the Can't sync view (FR-SYNC-07)."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from dropshare_client.controllers.sync_log_controller import ActivityRow, CantSyncRow
from dropshare_client.ui import theme
from dropshare_client.ui.format import modified_text
from dropshare_client.ui.keyed_rows import update_rows

type _Index = QModelIndex | QPersistentModelIndex

WHEN, WHAT, ITEM, DETAIL = range(4)
_ACTIVITY_HEADERS = ("Time", "What happened", "Item", "Details")
CANT_SYNC_ITEM, CANT_SYNC_REASON = range(2)
_CANT_SYNC_HEADERS = ("Item", "Why, and how to fix it")


def _name(path: str) -> str:
    return path.rpartition("/")[2] or path


class ActivityModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ActivityRow] = []

    def set_rows(self, rows: list[ActivityRow]) -> None:
        update_rows(self, self._rows, rows, lambda row: (row.at, row.what, row.path, row.detail))

    def row_at(self, row: int) -> ActivityRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_ACTIVITY_HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _ACTIVITY_HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        row = self.row_at(index.row())
        if row is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DecorationRole and column == ITEM:
            return theme.file_icon(_name(row.path), folder=False)
        if role == Qt.ItemDataRole.ToolTipRole:  # the full text, which the columns may shorten
            return "\n".join(part for part in (row.path, row.detail) if part)
        if role == Qt.ItemDataRole.AccessibleTextRole and column == ITEM:
            return f"{row.path}, {row.what}"
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return None
        if column == WHEN:
            return modified_text(row.at)
        if column == WHAT:
            return row.what
        if column == ITEM:
            return row.path
        return row.detail


class CantSyncModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[CantSyncRow] = []

    def set_rows(self, rows: list[CantSyncRow]) -> None:
        update_rows(self, self._rows, rows, lambda row: row.key)

    def row_at(self, row: int) -> CantSyncRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_CANT_SYNC_HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _CANT_SYNC_HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        row = self.row_at(index.row())
        if row is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.DecorationRole and column == CANT_SYNC_ITEM:
            return theme.file_icon(_name(row.path), folder=False)
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row.path}\n{row.reason}"
        if role == Qt.ItemDataRole.AccessibleTextRole and column == CANT_SYNC_ITEM:
            return f"{row.path}, can't sync"
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return None
        return row.path if column == CANT_SYNC_ITEM else row.reason
