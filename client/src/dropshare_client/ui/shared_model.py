"""The rows of Shared with me (FR-FILE-01): what is shared, by whom, what the user can do with
it, and whether a folder is in their DropShare."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from dropshare_client.controllers.shared_controller import NodeKind, SharedItemOut, ShareRole
from dropshare_client.ui.format import modified_text
from dropshare_client.ui.keyed_rows import update_rows

type _Index = QModelIndex | QPersistentModelIndex

NAME, OWNER, ROLE, SHARED, IN_DROPSHARE = range(5)
_HEADERS = ("Name", "Shared by", "You can", "Shared", "In my DropShare")


class SharedModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[SharedItemOut] = []

    def set_rows(self, rows: list[SharedItemOut]) -> None:
        update_rows(self, self._rows, rows, lambda item: item.node_id)

    def item_at(self, row: int) -> SharedItemOut | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def row_of(self, node_id: object) -> int | None:
        return next((n for n, item in enumerate(self._rows) if item.node_id == node_id), None)

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> str | None:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        item = self.item_at(index.row())
        if item is None or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.AccessibleTextRole,
        ):
            return None
        column = index.column()
        if column == NAME:
            kind = "folder" if item.kind == NodeKind.FOLDER else "file"
            if role == Qt.ItemDataRole.AccessibleTextRole:
                return f"{item.name}, {kind}"
            return item.name
        if column == OWNER:
            return item.owner.display_name
        if column == ROLE:
            return "Edit" if item.role == ShareRole.EDITOR else "View"
        if column == SHARED:
            return modified_text(item.shared_at)
        if item.kind != NodeKind.FOLDER:
            return "Not synced (a single file)"
        return "Yes" if item.mount_node_id is not None else "No"
