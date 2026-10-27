"""Sign in with username or email and password (FR-AUTH-01, FR-CLIENT-05)."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.ui import theme
from dropshare_client.ui.forms import add_field, message_label, password_field, show_message


class SignInDialog(QDialog):
    """Stays open while signing in; the app shell closes it once the session is ready."""

    def __init__(self, controller: SessionController, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("Sign in to StateLife DropShare")
        self.setAccessibleName("Sign in")
        self.setMinimumWidth(theme.DIALOG_WIDTH)

        strip = QFrame()
        strip.setObjectName("brandStrip")
        strip.setFixedHeight(theme.BRAND_STRIP_HEIGHT)
        logo = QLabel()
        logo.setPixmap(theme.logo(theme.LOGO_HEIGHT, self.devicePixelRatioF()))
        heading = QLabel("Sign in to DropShare")
        heading.setProperty("textRole", "subtitle")
        intro = QLabel("Sign in with the username or email your administrator gave you.")
        intro.setProperty("textRole", "caption")
        intro.setWordWrap(True)
        for label in (logo, heading, intro):
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._message = message_label("sign_in_message")

        self._login = QLineEdit()
        self._password = password_field()
        self._use_other_server = QCheckBox("Use a &different server")
        self._use_other_server.setAccessibleDescription(
            "For testing: sign in to a server other than the built-in one"
        )
        self._server = QLineEdit(controller.server_url)
        # A saved override shows as such; unticking goes back to the built-in server.
        self._use_other_server.setChecked(controller.server_url != controller.default_server_url)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)  # labels above fields
        form.setVerticalSpacing(theme.SPACE.sm)
        add_field(form, "&Username or email", self._login)
        add_field(form, "&Password", self._password)
        form.addRow(self._use_other_server)
        add_field(form, "Server &address", self._server)
        self._form = form
        self._use_other_server.toggled.connect(self._show_server)
        self._show_server(self._use_other_server.isChecked())

        # One full-width primary action, with Cancel quiet beneath it.
        self._sign_in = QPushButton("Sign in")
        self._sign_in.setProperty("variant", "primary")
        self._sign_in.setDefault(True)
        self._cancel = QPushButton("Cancel")
        self._cancel.setProperty("variant", "subtle")
        self._sign_in.clicked.connect(self.accept)
        self._cancel.clicked.connect(self.reject)

        body = QVBoxLayout()
        space = theme.SPACE
        body.setContentsMargins(space.xl, space.xl, space.xl, space.xl)
        body.setSpacing(0)
        body.addWidget(logo)
        body.addSpacing(space.lg)
        body.addWidget(heading)
        body.addSpacing(space.xs)
        body.addWidget(intro)
        body.addSpacing(space.xl)
        body.addLayout(form)
        body.addSpacing(space.md)
        body.addWidget(self._message)
        body.addSpacing(space.lg)
        body.addWidget(self._sign_in)
        body.addSpacing(space.xs)
        body.addWidget(self._cancel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(strip)
        layout.addLayout(body)

        controller.busy_changed.connect(self._set_busy)
        controller.sign_in_failed.connect(self._failed)

    def show_message(self, text: str) -> None:
        show_message(self._message, text)

    def accept(self) -> None:
        """Sign in rather than close: the shell closes the dialog when the session is ready."""
        self.show_message("")
        server = (
            self._server.text()
            if self._use_other_server.isChecked()
            else self._controller.default_server_url
        )
        self._controller.sign_in(server, self._login.text(), self._password.text())

    def reject(self) -> None:
        """Cancel and Escape do nothing while signing in: the result would arrive anyway."""
        if not self._controller.busy:
            super().reject()

    def done_signing_in(self) -> None:
        self._password.clear()
        super().accept()

    def _failed(self, message: str) -> None:
        self.show_message(message)
        self._password.clear()
        (self._login if not self._login.text() else self._password).setFocus()

    def _set_busy(self, busy: bool) -> None:
        for widget in (self._login, self._password, self._server, self._use_other_server):
            widget.setEnabled(not busy)
        self._sign_in.setEnabled(not busy)
        self._cancel.setEnabled(not busy)
        self._sign_in.setText("Signing in…" if busy else "Sign in")

    def _show_server(self, visible: bool) -> None:
        self._form.setRowVisible(self._server, visible)
