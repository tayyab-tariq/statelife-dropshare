"""Change password; forced after signing in with a temporary password (FR-AUTH-05)."""

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers.session_controller import PasswordField, SessionController
from dropshare_client.ui.forms import add_field, message_label, password_field, show_message
from dropshare_shared.schemas.auth import PASSWORD_MIN_LENGTH


class ChangePasswordDialog(QDialog):
    def __init__(
        self, controller: SessionController, *, forced: bool, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("Choose a new password" if forced else "Change password")
        self.setAccessibleName(self.windowTitle())
        intro = QLabel(
            "You signed in with a temporary password. Choose a new password to continue."
            if forced
            else "Other computers signed in to your account will be signed out."
        )
        intro.setWordWrap(True)
        hint = QLabel(
            f"Use at least {PASSWORD_MIN_LENGTH} characters. Common passwords aren't allowed."
        )
        hint.setWordWrap(True)
        self._message = message_label("password_message")

        self._current = password_field()
        self._new = password_field()
        self._confirm = password_field()
        # Each field's own description; while an error is shown, it leads the target's.
        self._descriptions = {self._current: "", self._new: hint.text(), self._confirm: ""}
        self._describe(None, "")
        form = QFormLayout()
        add_field(form, "&Current password", self._current)
        add_field(form, "&New password", self._new)
        add_field(form, "C&onfirm new password", self._confirm)
        form.addRow(hint)

        self._buttons = QDialogButtonBox()
        self._change = self._buttons.addButton(
            "Change password", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._change.setDefault(True)
        # Leaving a forced change signs out: the account can't be used until it's done.
        self._leave = self._buttons.addButton(
            "Sign out" if forced else "Cancel", QDialogButtonBox.ButtonRole.RejectRole
        )
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._message)
        layout.addLayout(form)
        layout.addWidget(self._buttons)

        controller.busy_changed.connect(self._set_busy)
        controller.password_change_failed.connect(self._failed)

    def accept(self) -> None:
        show_message(self._message, "")
        self._describe(None, "")
        self._controller.change_password(
            self._current.text(), self._new.text(), self._confirm.text()
        )

    def reject(self) -> None:
        """Not while a change is in flight: signing out then would race it (audit B6)."""
        if not self._controller.busy:
            super().reject()

    def done_changing(self) -> None:
        for field in (self._current, self._new, self._confirm):
            field.clear()
        super().accept()

    def _failed(self, message: str, field: PasswordField) -> None:
        """Focus moves first and the field's description carries the reason: a screen reader
        cuts an alert short when focus moves, but reads the focused field's description."""
        target = {PasswordField.NEW: self._new, PasswordField.CONFIRM: self._confirm}.get(
            field, self._current
        )
        if target is self._new:
            self._confirm.clear()  # it holds the refused password too
        self._describe(target, message)
        target.setFocus()
        target.selectAll()
        show_message(self._message, message)

    def _describe(self, target: QLineEdit | None, message: str) -> None:
        for field, description in self._descriptions.items():
            text = f"{message} {description}".strip() if field is target else description
            field.setAccessibleDescription(text)

    def _set_busy(self, busy: bool) -> None:
        for widget in (self._current, self._new, self._confirm, self._change, self._leave):
            widget.setEnabled(not busy)
