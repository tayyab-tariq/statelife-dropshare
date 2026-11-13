"""The Administration section (FR-ADMIN-06), shown to admins only: the user list with search,
and New user, Enable/Disable, Reset password, Make administrator / regular user, and Change
storage limit. Loading, error with Retry, empty and populated states (NFR-UX-01)."""

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.admin_controller import (
    AdminController,
    AdminUserCreateIn,
    AdminUserOut,
    TemporaryPasswordOut,
    UserRole,
    UserStatus,
)
from dropshare_client.ui import theme
from dropshare_client.ui.admin_dialogs import NewUserDialog, QuotaDialog, TemporaryPasswordDialog
from dropshare_client.ui.admin_model import EMAIL, NAME, USERNAME, AdminUsersModel, UserSearch
from dropshare_client.ui.forms import (
    announce,
    clear_reload_message,
    confirm,
    message_label,
    show_message,
    status_label,
)
from dropshare_client.ui.stateful_view import StatefulView

_STATUS_MS = 8000


class AdminView(QWidget):
    def __init__(self, controller: AdminController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        space = theme.SPACE
        # Hooks (tests replace them): ask before a change, and ask for a storage limit.
        self.confirm: Callable[[str, str, str], bool] = lambda title, text, action: confirm(
            self, title, text, action
        )
        self.ask_quota: Callable[[AdminUserOut], int | None] = self._ask_quota
        self.new_user_dialog: NewUserDialog | None = None
        self.password_dialog: TemporaryPasswordDialog | None = None

        title = QLabel(messages.ADMINISTRATION)
        title.setProperty("textRole", "title")
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search by username, name or email")
        self._search.setAccessibleName("Search users")
        self._search.setClearButtonEnabled(True)
        self._new = QPushButton("&New user…")
        self._new.setProperty("variant", "primary")
        self._toggle = QPushButton("&Disable…")
        self._reset = QPushButton("Reset &password…")
        self._role = QPushButton("Make &administrator…")
        self._quota = QPushButton("Change storage &limit…")
        self._refresh = QPushButton("Re&fresh")
        self._refresh.setToolTip("Refresh (F5)")
        actions = QHBoxLayout()
        for button in (self._new, self._toggle, self._reset, self._role, self._quota):
            actions.addWidget(button)
        actions.addStretch()
        actions.addWidget(self._refresh)

        self._model = AdminUsersModel()
        self._proxy = UserSearch()
        self._proxy.setSourceModel(self._model)
        self._table = QTableView()
        self._table.setObjectName("usersTable")
        self._table.setAccessibleName("Users")
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setShowGrid(False)
        self._table.setWordWrap(False)
        self._table.verticalHeader().hide()
        header = self._table.horizontalHeader()
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for column in (USERNAME, NAME, EMAIL):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        self._view = StatefulView(self._table)

        self._error = message_label("adminError")
        self._status = status_label("adminStatus")
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(_STATUS_MS)
        self._status_timer.timeout.connect(self._status.clear)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.md)
        layout.addWidget(title)
        layout.addWidget(self._search)
        layout.addLayout(actions)
        layout.addWidget(self._error)
        layout.addWidget(self._view, stretch=1)
        layout.addWidget(self._status)

        refresh = QAction("Re&fresh", self)
        refresh.setShortcut(QKeySequence(QKeySequence.StandardKey.Refresh))
        refresh.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        refresh.triggered.connect(controller.refresh)
        self.addAction(refresh)

        self._search.textChanged.connect(self._filter)
        self._new.clicked.connect(self._ask_new_user)
        self._toggle.clicked.connect(self._ask_toggle)
        self._reset.clicked.connect(self._ask_reset)
        self._role.clicked.connect(self._ask_role)
        self._quota.clicked.connect(self._change_quota)
        self._refresh.clicked.connect(controller.refresh)
        self._view.retry_requested.connect(controller.refresh)
        self._view.empty_action_requested.connect(self._ask_new_user)
        self._table.selectionModel().selectionChanged.connect(lambda *_: self._update())
        # Enter or double-click on a row: change that user's storage limit.
        self._table.activated.connect(lambda _index: self._change_quota())
        controller.loading.connect(
            lambda: self._view.show_loading(messages.LOADING_USERS, keep_content=False)
        )
        controller.loaded.connect(self._show)
        controller.failed.connect(self._view.show_error)
        controller.reload_failed.connect(
            lambda message: show_message(self._error, message, from_reload=True)
        )
        controller.busy_changed.connect(self._busy)
        controller.succeeded.connect(self._announce)
        controller.action_failed.connect(self._action_failed)
        controller.password_issued.connect(self._show_password)
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

    # --- state -------------------------------------------------------------------------------

    def _show(self) -> None:
        clear_reload_message(self._error)
        selected = self._selected()
        self._model.set_rows(self._controller.users())
        self._show_rows()
        if selected is not None:
            self._select(selected.id)
        self._update()

    def _show_rows(self) -> None:
        if self._proxy.rowCount():
            self._view.show_content()
        elif self._model.rowCount():
            self._view.show_empty(messages.NO_MATCHING_USERS)
        else:
            self._view.show_empty(messages.NO_USERS, "New user…")

    def _filter(self, text: str) -> None:
        self._proxy.set_prefix(text)
        self._show_rows()
        self._update()

    def _cleared(self) -> None:
        self._search.clear()
        self._table.clearSelection()
        show_message(self._error, "")
        announce(self._status, "")
        for dialog in (self.new_user_dialog, self.password_dialog):
            if dialog is not None:
                dialog.done(QDialog.DialogCode.Rejected)
        self.new_user_dialog = self.password_dialog = None

    def _busy(self, busy: bool) -> None:
        if self.new_user_dialog is not None:
            self.new_user_dialog.set_busy(busy)
        self._update()

    def _selected(self) -> AdminUserOut | None:
        rows = self._table.selectionModel().selectedRows()
        return self._proxy.user_at(rows[0].row()) if rows else None

    def _select(self, user_id: object) -> None:
        for row in range(self._proxy.rowCount()):
            user = self._proxy.user_at(row)
            if user is not None and user.id == user_id:
                self._table.selectRow(row)
                return

    def _update(self) -> None:
        user = self._selected()
        idle = not self._controller.busy
        self._new.setEnabled(idle)
        for button in (self._toggle, self._reset, self._role, self._quota):
            button.setEnabled(idle and user is not None)
        disabled = user is not None and user.status == UserStatus.DISABLED
        self._toggle.setText("&Enable" if disabled else "&Disable…")
        admin = user is not None and user.role == UserRole.ADMIN
        self._role.setText("Make regular &user…" if admin else "Make &administrator…")

    # --- actions -----------------------------------------------------------------------------

    def _ask_new_user(self) -> None:
        if self._controller.busy:
            return
        dialog = NewUserDialog(self._create, self)
        dialog.finished.connect(lambda _: self._forget_new_user(dialog))
        self.new_user_dialog = dialog
        dialog.open()

    def _forget_new_user(self, dialog: NewUserDialog) -> None:
        if self.new_user_dialog is dialog:
            self.new_user_dialog = None

    def _create(self, body: AdminUserCreateIn) -> None:
        self._clear_messages()
        self._controller.create(body)

    def _ask_toggle(self) -> None:
        user = self._selected()
        if user is None or self._controller.busy:
            return
        self._clear_messages()
        if user.status == UserStatus.DISABLED:
            self._controller.set_status(user, UserStatus.ACTIVE)
        elif self.confirm("Disable user", messages.confirm_disable(user.username), "Disable"):
            self._controller.set_status(user, UserStatus.DISABLED)

    def _ask_reset(self) -> None:
        user = self._selected()
        if user is None or self._controller.busy:
            return
        self._clear_messages()
        text = messages.confirm_reset(user.username)
        if self.confirm("Reset password", text, "Reset password"):
            self._controller.reset_password(user)

    def _ask_role(self) -> None:
        user = self._selected()
        if user is None or self._controller.busy:
            return
        self._clear_messages()
        to_admin = user.role != UserRole.ADMIN
        title = "Make administrator" if to_admin else "Make regular user"
        if self.confirm(title, messages.confirm_role(user.username, to_admin), title):
            self._controller.set_role(user, UserRole.ADMIN if to_admin else UserRole.USER)

    def _change_quota(self) -> None:
        user = self._selected()
        if user is None or self._controller.busy:
            return
        self._clear_messages()
        quota = self.ask_quota(user)
        if quota is not None and quota != user.quota_bytes:
            self._controller.set_quota(user, quota)

    def _ask_quota(self, user: AdminUserOut) -> int | None:
        dialog = QuotaDialog(user, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted
        return dialog.quota_bytes if accepted else None

    # --- results -----------------------------------------------------------------------------

    def _show_password(self, issued: object) -> None:
        if not isinstance(issued, TemporaryPasswordOut):
            return
        if self.new_user_dialog is not None:
            self.new_user_dialog.created()
        dialog = TemporaryPasswordDialog(issued, self)
        dialog.finished.connect(lambda _: self._forget_password(dialog))
        self.password_dialog = dialog
        dialog.open()

    def _forget_password(self, dialog: TemporaryPasswordDialog) -> None:
        if self.password_dialog is dialog:
            self.password_dialog = None

    def _action_failed(self, message: str) -> None:
        """While New user is open, its own error goes there, beside what was typed."""
        if self.new_user_dialog is not None:
            self.new_user_dialog.failed(message)
        else:
            show_message(self._error, message)

    def _announce(self, text: str) -> None:
        announce(self._status, text)
        self._status_timer.start()

    def _clear_messages(self) -> None:
        show_message(self._error, "")
        announce(self._status, "")
