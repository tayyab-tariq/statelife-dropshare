"""The rows of the Trash (FR-TRASH-01): what was deleted, from where, when, by whom, and how big."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from dropshare_client.controllers import messages
from dropshare_client.controllers.trash_controller import NodeKind, TrashItemOut
from dropshare_client.ui import theme
from dropshare_client.ui.format import modified_text, size_text
from dropshare_client.ui.keyed_rows import update_rows

type _Index = QModelIndex | QPersistentModelIndex

NAME, LOCATION, DELETED, DELETED_BY, SIZE = range(5)
_HEADERS = ("Name", "Original location", "Deleted", "Deleted by", "Size")


def location_text(item: TrashItemOut) -> str:
    """Where it was: "My files / Docs", or the shared folder's name and the path inside it."""
    top = messages.MY_FILES if item.shared_folder is None else item.shared_folder
    return " / ".join([top, *[part for part in item.location.split("/") if part]])


class TrashModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[TrashItemOut] = []

    def set_rows(self, rows: list[TrashItemOut]) -> None:
        update_rows(self, self._rows, rows, lambda item: item.id)

    def item_at(self, row: int) -> TrashItemOut | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        if role == Qt.ItemDataRole.TextAlignmentRole and section == SIZE:  # as its cells
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        item = self.item_at(index.row())
        if item is None:
            return None
        column = index.column()
        if role == Qt.ItemDataRole.TextAlignmentRole and column == SIZE:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        folder = item.kind == NodeKind.FOLDER
        if role == Qt.ItemDataRole.DecorationRole and column == NAME:
            return theme.file_icon(item.name, folder=folder)
        if role == Qt.ItemDataRole.ToolTipRole:  # the full text, which the columns may shorten
            kind = "Folder" if folder else "File"
            return f"{item.name}\n{kind} · from {location_text(item)}"
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return None
        if column == NAME:
            if role == Qt.ItemDataRole.AccessibleTextRole:
                if item.kind != NodeKind.FOLDER:
                    return f"{item.name}, file"
                inside = "1 item" if item.item_count == 1 else f"{item.item_count} items"
                return f"{item.name}, folder with {inside}"
            return item.name
        if column == LOCATION:
            return location_text(item)
        if column == DELETED:
            return modified_text(item.trashed_at)
        if column == DELETED_BY:
            return item.trashed_by or "Unknown"
        return size_text(item.size_bytes)
