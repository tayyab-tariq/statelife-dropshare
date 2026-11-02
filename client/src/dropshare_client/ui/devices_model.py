"""The rows of Settings' signed-in computers (FR-AUTH-07): name and OS, last seen, version."""

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt

from dropshare_client.controllers import messages
from dropshare_client.controllers.devices_controller import DeviceOut
from dropshare_client.ui.format import modified_text
from dropshare_client.ui.keyed_rows import update_rows

type _Index = QModelIndex | QPersistentModelIndex

_DEVICE_HEADERS = ("Computer", "Last seen", "DropShare version")


class DevicesModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[DeviceOut] = []

    def set_rows(self, rows: list[DeviceOut]) -> None:
        update_rows(self, self._rows, rows, lambda device: device.id)

    def device_at(self, row: int) -> DeviceOut | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def rowCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: _Index = QModelIndex()) -> int:  # noqa: B008, N802 - Qt API
        return 0 if parent.isValid() else len(_DEVICE_HEADERS)

    def headerData(  # noqa: N802 - Qt API
        self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole
    ) -> object:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _DEVICE_HEADERS[section]
        return None

    def data(self, index: _Index, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        device = self.device_at(index.row())
        if device is None or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.AccessibleTextRole,
        ):
            return None
        column = index.column()
        if column == 0:
            name = f"{device.name} ({device.os_name})"
            return f"{name} · {messages.THIS_DEVICE}" if device.is_current else name
        if column == 1:
            return modified_text(device.last_seen_at)
        return device.client_version
