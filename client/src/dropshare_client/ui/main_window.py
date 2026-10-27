"""The main window for M4: account, sync folder and status. The file browser comes later (F5)."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.ui.stateful_view import StatefulView
from dropshare_shared.schemas.users import UserOut


class _Value(QLabel):
    """A value next to a visible row label. Its accessible name says both ("Server:
    https://…"), so a screen reader announces the value with what it is."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self._caption = caption
        self.setWordWrap(True)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    def set_value(self, value: str) -> None:
        self.setText(value)
        self.setAccessibleName(f"{self._caption}: {value}" if value else self._caption)


class MainWindow(QMainWindow):
    open_folder_requested = Signal()
    choose_folder_requested = Signal()
    change_password_requested = Signal()
    sign_out_requested = Signal()
    retry_requested = Signal()
    quit_requested = Signal()  # closing the window when there's no tray to hide into

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("StateLife DropShare")
        self.setAccessibleName("DropShare")
        self._quitting = False
        self._hide_on_close = True

        heading = QLabel("StateLife DropShare")
        self._account = _Value("Signed in as")
        self._server = _Value("Server")
        self._folder = _Value("Sync folder")
        self._status = _Value("Status")
        details = QFormLayout()
        for caption, value in (
            ("Signed in as", self._account),
            ("Server", self._server),
            ("Sync folder", self._folder),
            ("Status", self._status),
        ):
            details.addRow(caption, value)
        self._open_folder = QPushButton("&Open sync folder")

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(heading)
        layout.addLayout(details)
        layout.addWidget(self._open_folder, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addStretch()
        self._view = StatefulView(content)

        # Account actions stay reachable in every state, including empty and error.
        self._change_password = QPushButton("&Change password…")
        self._sign_out = QPushButton("&Sign out…")
        actions = QHBoxLayout()
        actions.addStretch()
        actions.addWidget(self._change_password)
        actions.addWidget(self._sign_out)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.addWidget(self._view, stretch=1)
        outer.addLayout(actions)
        self.setCentralWidget(central)

        self._open_folder.clicked.connect(self.open_folder_requested)
        self._change_password.clicked.connect(self.change_password_requested)
        self._sign_out.clicked.connect(self.sign_out_requested)
        self._view.retry_requested.connect(self.retry_requested)
        self._view.empty_action_requested.connect(self.choose_folder_requested)

    @property
    def view(self) -> StatefulView:
        return self._view

    def set_hide_on_close(self, hide: bool) -> None:
        """Without a system tray there is nothing to hide into: closing then quits."""
        self._hide_on_close = hide

    def show_loading(self, message: str) -> None:
        self._view.show_loading(message)

    def show_error(self, message: str) -> None:
        self._view.show_error(message)

    def show_account(self, user: UserOut, server: str, folder: str | None, status: str) -> None:
        self._account.set_value(f"{user.display_name} ({user.username})")
        self._server.set_value(server)
        self._folder.set_value(folder or "Not chosen yet")
        self._status.set_value(status)
        if folder is None:  # the empty state offers to choose one
            self._view.show_empty(
                "Choose where DropShare keeps your files to start syncing.",
                "Choose sync folder…",
            )
        else:
            self._view.show_content()

    def clear_account(self) -> None:
        """After sign-out: nothing of the previous user stays in the (hidden) window."""
        for value in (self._account, self._server, self._folder, self._status):
            value.set_value("")

    def set_status(self, status: str) -> None:
        self._status.set_value(status)

    def set_actions_enabled(self, enabled: bool) -> None:
        """Off while a sign-in, password change or sign-out is in progress."""
        for button in (self._change_password, self._sign_out):
            button.setEnabled(enabled)

    def bring_to_front(self) -> None:
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        self._quitting = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        """Closing hides to the tray; only Quit ends the app (FR-CLIENT-01)."""
        if self._quitting:
            event.accept()
        elif self._hide_on_close:
            event.ignore()
            self.hide()
        else:
            event.ignore()
            self.quit_requested.emit()
