"""Rows of the file browser (FR-FILE-01), loaded page by page as the view scrolls and kept up to
date by node ID, so a refresh doesn't lose the selection or the current row (ui.md §4)."""

import uuid
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QMimeData,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.browser_controller import BrowserController, BrowserRow
from dropshare_client.controllers.file_actions_controller import Item
from dropshare_client.ui import theme
from dropshare_client.ui.format import modified_text
from dropshare_client.ui.keyed_rows import update_rows

type _Index = QModelIndex | QPersistentModelIndex

NAME, MODIFIED, MODIFIED_BY, SIZE, SHARING, SYNC = range(6)
_HEADERS = ("Name", "Modified", "Modified by", "Size", "Sharing", "Sync")
ROW_ROLE = Qt.ItemDataRole.UserRole + 1  # the BrowserRow
# Sharing arrives with roadmap M8; until then everything in My files is the user's alone.
_NOT_SHARED = "Only you"
# Rows dragged within the browser: "node id<TAB>local path" per line. Only honoured when the
# drag started in this view (BrowserView checks the source).
PATHS_MIME = "application/x-dropshare-items"
_FILES_MIME = "text/uri-list"  # files dragged from Explorer


def _local_files(data: QMimeData) -> list[Path]:
    return [Path(url.toLocalFile()) for url in data.urls() if url.isLocalFile()]


def _items(data: QMimeData) -> list[Item]:
    items = []
    for line in bytes(data.data(PATHS_MIME).data()).decode().splitlines():
        node, _, path = line.partition("\t")
        try:
            items.append(Item(path, uuid.UUID(node)))
        except ValueError:
            continue
    return items


class BrowserModel(QAbstractTableModel):
    move_requested = Signal(list, object)  # list[Item] dropped onto a folder (Item)
    upload_requested = Signal(list, object)  # files from Explorer (Paths), the folder (Item)

    def __init__(self, controller: BrowserController) -> None:
        super().__init__()
        self._controller = controller
        self._rows: list[BrowserRow] = []
        controller.loaded.connect(self._loaded)
        controller.appended.connect(self._appended)
        controller.states_changed.connect(self._states)

    def row_at(self, row: int) -> BrowserRow | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_HEADERS)

    # --- drag and drop -----------------------------------------------------------------------

    def flags(self, index: _Index) -> Qt.ItemFlag:
        flags = super().flags(index)
        row = self.row_at(index.row()) if index.isValid() else None
        if row is not None and row.item is not None:  # only what is on this computer moves
            flags |= Qt.ItemFlag.ItemIsDragEnabled
            if row.is_folder:
                flags |= Qt.ItemFlag.ItemIsDropEnabled
        return flags

    def supportedDropActions(self) -> Qt.DropAction:  # noqa: N802 - Qt API
        # Moves within the browser; Explorer offers its files to copy (FR-FILE-02).
        return Qt.DropAction.MoveAction | Qt.DropAction.CopyAction

    def mimeTypes(self) -> list[str]:  # noqa: N802 - Qt API
        return [PATHS_MIME, _FILES_MIME]

    def mimeData(self, indexes: Sequence[QModelIndex]) -> QMimeData:  # noqa: N802 - Qt API
        rows = sorted({index.row() for index in indexes})
        items = [r.item for i in rows if (r := self.row_at(i)) and r.item is not None]
        data = QMimeData()
        lines = "\n".join(f"{item.node_id}\t{item.path}" for item in items)
        data.setData(PATHS_MIME, lines.encode())
        return data

    def canDropMimeData(  # noqa: N802 - Qt API
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: _Index,
    ) -> bool:
        if data.hasFormat(PATHS_MIME):
            return self.folder_at(parent) is not None
        return bool(_local_files(data)) and self.upload_target(parent) is not None

    def dropMimeData(  # noqa: N802 - Qt API
        self,
        data: QMimeData,
        action: Qt.DropAction,
        row: int,
        column: int,
        parent: _Index,
    ) -> bool:
        """Asks for the move or the upload; rows change once it has synced, not here."""
        if data.hasFormat(PATHS_MIME):
            folder = self.folder_at(parent)
            if folder is not None:
                items = [i for i in _items(data) if i.node_id != folder.node_id]
                if items:
                    self.move_requested.emit(items, folder)
            return False
        files, target = _local_files(data), self.upload_target(parent)
        if files and target is not None:
            self.upload_requested.emit(files, target)
        return False

    def folder_at(self, parent: _Index) -> Item | None:
        """The synced folder at `parent`, if that's what it is."""
        row = self.row_at(parent.row()) if parent.isValid() else None
        return row.item if row is not None and row.is_folder else None

    def upload_target(self, parent: _Index) -> Item | None:
        """Onto a folder: into it. Anywhere else in the list: into the folder shown."""
        return self.folder_at(parent) or self._controller.current_folder

    # --- rows ------------------------------------------------------------------------------

    def canFetchMore(self, parent: _Index) -> bool:  # noqa: N802 - Qt API
        return not parent.isValid() and self._controller.can_load_more

    def fetchMore(self, parent: _Index) -> None:  # noqa: N802 - Qt API
        if not parent.isValid():
            self._controller.load_more()  # rows arrive later, through `appended`

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        row = self.row_at(index.row()) if index.isValid() else None
        if row is None:
            return None
        if role == ROW_ROLE:
            return row
        if role == Qt.ItemDataRole.DecorationRole and index.column() == NAME:
            return theme.file_icon(row.node.name or "", folder=row.is_folder)
        if role == Qt.ItemDataRole.ToolTipRole:
            kind = "Folder" if row.is_folder else "File"
            where = row.local_path or row.node.name or ""
            return f"{where}\n{kind} · {row.state.value}"
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return None
        return self._text(row, index.column(), role)

    @staticmethod
    def _text(row: BrowserRow, column: int, role: int) -> str:
        node = row.node
        if column == NAME:
            name = node.name or ""
            if role == Qt.ItemDataRole.AccessibleTextRole and row.is_folder:
                return f"{name}, folder"
            return name
        if column == MODIFIED:
            return modified_text(node.modified_at)
        if column == MODIFIED_BY:
            return "You" if row.modified_by_me else "Someone else"
        if column == SIZE:
            # The kind in words as well as by icon (never by the picture alone).
            return "Folder" if row.is_folder else messages.size_text(node.size_bytes)
        if column == SHARING:
            return _NOT_SHARED
        return row.state.value

    def _loaded(self) -> None:
        update_rows(self, self._rows, self._controller.rows(), lambda row: row.node.id)

    def _appended(self, first: int, count: int) -> None:
        if count <= 0:
            return
        self.beginInsertRows(QModelIndex(), first, first + count - 1)
        self._rows = self._controller.rows()
        self.endInsertRows()

    def _states(self, changed: list[int]) -> None:
        self._rows = self._controller.rows()
        for number in changed:
            self.dataChanged.emit(self.index(number, 0), self.index(number, SYNC))
