"""Dialogs of the Administration section (FR-ADMIN-02/06): a new user, a storage limit, and
the one-time temporary password."""

from collections.abc import Callable

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.admin_controller import (
    AdminUserCreateIn,
    AdminUserOut,
    TemporaryPasswordOut,
    UserRole,
)
from dropshare_client.ui.forms import add_field, announce, message_label, show_message, status_label

GIB = 1024**3  # GB as the CLI and the server's default quota count them
DEFAULT_QUOTA_GB = 50  # NFR-SCALE-03
_MAX_QUOTA_GB = 1_000_000  # 1 PB: far above any real quota

# Windows: keep a copied password out of clipboard history and cloud clipboard sync. Qt hands
# formats spelled this way to the Windows clipboard under the given name; other systems ignore
# them. Needs checking on the Windows machine.
_NO_HISTORY_FORMATS = (
    'application/x-qt-windows-mime;value="ExcludeClipboardContentFromMonitorProcessing"',
    'application/x-qt-windows-mime;value="CanIncludeInClipboardHistory"',
    'application/x-qt-windows-mime;value="CanUploadToCloudClipboard"',
)
_ZERO_DWORD = b"\x00\x00\x00\x00"


def _quota_field(gb: int) -> QSpinBox:
    """Whole GB, at least 1 (OQ-61 (5))."""
    field = QSpinBox()
    field.setRange(1, _MAX_QUOTA_GB)
    field.setSuffix(" GB")
    field.setValue(gb)
    return field


def _buttons(dialog: QDialog, action: str) -> tuple[QDialogButtonBox, list[QWidget]]:
    buttons = QDialogButtonBox()
    go = buttons.addButton(action, QDialogButtonBox.ButtonRole.AcceptRole)
    go.setDefault(True)
    cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    return buttons, [go, cancel]


class NewUserDialog(QDialog):
    """The new account's details. Create calls `submit`; the view closes the dialog once the
    user exists (`created`) or shows here what was wrong (`failed`), keeping what was typed."""

    def __init__(
        self, submit: Callable[[AdminUserCreateIn], None], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._submit = submit
        self.setWindowTitle("New user")
        self.setAccessibleName(self.windowTitle())
        intro = QLabel(
            "A temporary password is created, shown to you once, and emailed to the user. "
            "They choose their own password when they first sign in."
        )
        intro.setWordWrap(True)
        self._message = message_label("newUserMessage")
        self._username = QLineEdit()
        self._email = QLineEdit()
        self._display_name = QLineEdit()
        self._role = QComboBox()
        self._role.addItem(messages.USER_ROLE, UserRole.USER)
        self._role.addItem(messages.ADMIN_ROLE, UserRole.ADMIN)
        self._quota = _quota_field(DEFAULT_QUOTA_GB)
        form = QFormLayout()
        add_field(form, "&Username", self._username)
        add_field(form, "&Email", self._email)
        add_field(form, "&Display name", self._display_name)
        add_field(form, "&Role", self._role)
        add_field(form, "&Storage limit", self._quota)
        buttons, self._controls = _buttons(self, "Create user")

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._message)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def accept(self) -> None:
        """Not closed here: see the class docstring."""
        fields = (
            ("username", self._username),
            ("email", self._email),
            ("display name", self._display_name),
        )
        for name, field in fields:
            if not field.text().strip():
                self.failed(f"Enter the {name}.")
                field.setFocus()
                return
        show_message(self._message, "")
        self._submit(
            AdminUserCreateIn(
                username=self._username.text().strip(),
                email=self._email.text().strip(),
                display_name=self._display_name.text().strip(),
                role=UserRole(self._role.currentData()),
                quota_bytes=self._quota.value() * GIB,
            )
        )

    def created(self) -> None:
        super().accept()

    def failed(self, message: str) -> None:
        show_message(self._message, message)

    def set_busy(self, busy: bool) -> None:
        for widget in self._controls:
            widget.setEnabled(not busy)


class QuotaDialog(QDialog):
    """A user's storage limit, in whole GB. It can't go below what they store (OQ-46); the
    server says so if it does."""

    def __init__(self, user: AdminUserOut, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Change storage limit")
        self.setAccessibleName(self.windowTitle())
        intro = QLabel(
            f"{user.username} uses {messages.storage_text(user.used_bytes, user.quota_bytes)}, "
            "trash included."
        )
        intro.setWordWrap(True)
        self._quota = _quota_field(max(1, round(user.quota_bytes / GIB)))
        form = QFormLayout()
        add_field(form, "&Storage limit", self._quota)
        buttons, _ = _buttons(self, "Change limit")
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @property
    def quota_bytes(self) -> int:
        return self._quota.value() * GIB


class TemporaryPasswordDialog(QDialog):
    """The one-time temporary password (FR-ADMIN-06): shown once, with Copy, and whether the
    email went. Nothing of it is kept once the dialog closes."""

    def __init__(self, issued: TemporaryPasswordOut, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Temporary password")
        self.setAccessibleName(self.windowTitle())
        heading = QLabel(f"Temporary password for {issued.user.username}")
        heading.setProperty("textRole", "subtitle")
        heading.setWordWrap(True)
        hint = QLabel(messages.TEMPORARY_PASSWORD_HINT)
        hint.setWordWrap(True)
        email = QLabel(messages.email_outcome(str(issued.email), issued.user.email))
        email.setObjectName("emailOutcome")
        email.setWordWrap(True)
        self._password = QLineEdit(issued.temporary_password)
        self._password.setReadOnly(True)
        self._password.setAccessibleName("Temporary password")
        self._password.setAccessibleDescription(hint.text())
        self._status = status_label("passwordStatus")

        buttons = QDialogButtonBox()
        copy = buttons.addButton("&Copy password", QDialogButtonBox.ButtonRole.ActionRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.setDefault(True)
        copy.clicked.connect(self._copy)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(self._password)
        layout.addWidget(hint)
        layout.addWidget(email)
        layout.addWidget(self._status)
        layout.addWidget(buttons)
        self.finished.connect(lambda _: self._password.clear())
        self._password.setFocus()
        self._password.selectAll()

    def _copy(self) -> None:
        data = QMimeData()
        data.setText(self._password.text())
        for name in _NO_HISTORY_FORMATS:
            data.setData(name, _ZERO_DWORD)
        QGuiApplication.clipboard().setMimeData(data)
        announce(self._status, messages.TEMPORARY_PASSWORD_COPIED)
