"""The rows of the Administration user list (FR-ADMIN-02), and the search over them: the start
of the username, display name or email, ignoring case."""

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QSortFilterProxyModel,
    Qt,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.admin_controller import AdminUserOut, UserRole, UserStatus
from dropshare_client.ui.keyed_rows import update_rows

type _Index = QModelIndex | QPersistentModelIndex

USERNAME, NAME, EMAIL, ROLE, STATUS, STORAGE = range(6)
_HEADERS = ("Username", "Name", "Email", "Role", "Status", "Storage used")


def role_text(role: object) -> str:
    if role == UserRole.ADMIN:
        return messages.ADMIN_ROLE
    return messages.USER_ROLE if role == UserRole.USER else str(role)


def status_text(status: object) -> str:
    if status == UserStatus.ACTIVE:
        return messages.ACTIVE
    return messages.DISABLED_STATUS if status == UserStatus.DISABLED else str(status)


class AdminUsersModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[AdminUserOut] = []

    def set_rows(self, rows: list[AdminUserOut]) -> None:
        update_rows(self, self._rows, rows, lambda user: user.id)

    def user_at(self, row: int) -> AdminUserOut | None:
        return self._rows[row] if 0 <= row < len(self._rows) else None

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
        user = self.user_at(index.row())
        if user is None or role not in (
            Qt.ItemDataRole.DisplayRole,
            Qt.ItemDataRole.AccessibleTextRole,
            Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        return _cell(user, index.column())


def _cell(user: AdminUserOut, column: int) -> str:
    if column == USERNAME:
        return user.username
    if column == NAME:
        return user.display_name
    if column == EMAIL:
        return user.email
    if column == ROLE:
        return role_text(user.role)
    if column == STATUS:
        return status_text(user.status)
    return messages.storage_text(user.used_bytes, user.quota_bytes)


class UserSearch(QSortFilterProxyModel):
    """Rows whose username, display name or email starts with the search text."""

    def __init__(self) -> None:
        super().__init__()
        self._prefix = ""

    def set_prefix(self, text: str) -> None:
        self.beginFilterChange()
        self._prefix = text.strip().casefold()
        self.endFilterChange()

    def user_at(self, row: int) -> AdminUserOut | None:
        source = self.sourceModel()
        if not isinstance(source, AdminUsersModel):
            return None
        return source.user_at(self.mapToSource(self.index(row, 0)).row())

    def filterAcceptsRow(self, source_row: int, source_parent: _Index) -> bool:  # noqa: N802 - Qt API
        source = self.sourceModel()
        user = source.user_at(source_row) if isinstance(source, AdminUsersModel) else None
        if user is None or not self._prefix:
            return user is not None
        return any(
            value.casefold().startswith(self._prefix)
            for value in (user.username, user.display_name, user.email)
        )
