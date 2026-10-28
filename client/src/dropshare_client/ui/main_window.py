"""The main window: a header with the account actions, sections down the side (My files,
Shared with me, Transfers, and the overview of account and sync folder), and the chosen
section's page."""

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.ui import theme
from dropshare_client.ui.stateful_view import StatefulView
from dropshare_shared.schemas.users import UserOut

OVERVIEW = "Overview"
_PAUSE = "&Pause syncing"
_RESUME = "&Resume syncing"


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


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("textRole", "caption")
    return label


def _card(heading: str, rows: list[tuple[str, QWidget]]) -> tuple[QFrame, QVBoxLayout]:
    """A titled card of caption/value rows; returns its layout for anything added below."""
    space = theme.SPACE
    card = QFrame()
    card.setProperty("card", True)
    title = QLabel(heading)
    title.setProperty("textRole", "subtitle")
    form = QFormLayout()
    form.setHorizontalSpacing(space.xl)
    form.setVerticalSpacing(space.md)
    for caption, value in rows:
        form.addRow(_caption(caption), value)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(space.xl, space.lg, space.xl, space.xl)
    layout.setSpacing(space.lg)
    layout.addWidget(title)
    layout.addLayout(form)
    return card, layout


class MainWindow(QMainWindow):
    open_folder_requested = Signal()
    choose_folder_requested = Signal()
    change_password_requested = Signal()
    sign_out_requested = Signal()
    retry_requested = Signal()
    pause_toggled = Signal(bool)  # True: pause (FR-SYNC-05; reachable without a tray too)
    quit_requested = Signal()  # closing the window when there's no tray to hide into

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("StateLife DropShare")
        self.setAccessibleName("DropShare")
        self._quitting = False
        self._hide_on_close = True

        self.setMinimumSize(theme.MAIN_WINDOW_MIN_SIZE)
        space = theme.SPACE

        self._account = _Value("Signed in as")
        self._server = _Value("Server")
        self._folder = _Value("Sync folder")
        self._status = _Value("Status")
        self._open_folder = QPushButton("&Open sync folder")
        self._open_folder.setProperty("variant", "primary")
        self._pause = QPushButton(_PAUSE)
        self._paused = False

        sync_card, sync_layout = _card(
            "Sync", [("Status", self._status), ("Sync folder", self._folder)]
        )
        folder_actions = QHBoxLayout()
        folder_actions.setSpacing(space.sm)
        folder_actions.addWidget(self._open_folder)
        folder_actions.addWidget(self._pause)
        folder_actions.addStretch()
        sync_layout.addLayout(folder_actions)
        account_card, _ = _card(
            "Account", [("Signed in as", self._account), ("Server", self._server)]
        )

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.lg)
        layout.addWidget(sync_card)
        layout.addWidget(account_card)
        layout.addStretch()
        self._view = StatefulView(content)

        # Account actions live in the header, so they stay reachable in every state,
        # including empty and error.
        header = QFrame()
        header.setObjectName("appHeader")
        mark = QLabel()
        mark.setPixmap(theme.logo_mark(theme.HEADER_MARK_SIZE, self.devicePixelRatioF()))
        wordmark = QLabel("StateLife DropShare")
        wordmark.setProperty("textRole", "subtitle")
        self._change_password = QPushButton("&Change password…")
        self._sign_out = QPushButton("&Sign out…")
        actions = QHBoxLayout(header)
        actions.setContentsMargins(space.xl, space.md, space.xl, space.md)
        actions.setSpacing(space.sm)
        actions.addWidget(mark)
        actions.addSpacing(space.xs)
        actions.addWidget(wordmark)
        actions.addStretch()
        for button in (self._change_password, self._sign_out):
            button.setProperty("variant", "subtle")
            actions.addWidget(button)

        # Sections down the side (ui.md §2); the overview is the first.
        self._nav = QListWidget()
        self._nav.setObjectName("navList")
        self._nav.setAccessibleName("Sections")
        self._nav.setMinimumWidth(theme.SIDEBAR_WIDTH)  # grows with larger text sizes
        self._nav.setMaximumWidth(theme.SIDEBAR_WIDTH * 2)
        self._pages = QStackedWidget()
        self._nav.currentRowChanged.connect(self._pages.setCurrentIndex)
        self.add_page(OVERVIEW, self._view)
        self._nav.setCurrentRow(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._nav)
        body.addWidget(self._pages, stretch=1)

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(header)
        outer.addLayout(body, stretch=1)
        self.setCentralWidget(central)

        self._open_folder.clicked.connect(self.open_folder_requested)
        self._pause.clicked.connect(lambda: self.pause_toggled.emit(not self._paused))
        self._change_password.clicked.connect(self.change_password_requested)
        self._sign_out.clicked.connect(self.sign_out_requested)
        self._view.retry_requested.connect(self.retry_requested)
        self._view.empty_action_requested.connect(self.choose_folder_requested)

    @property
    def view(self) -> StatefulView:
        return self._view

    def add_page(self, title: str, page: QWidget, position: int | None = None) -> None:
        """A section in the sidebar, shown when chosen there; at the end by default."""
        row = self._nav.count() if position is None else position
        current = self._nav.currentRow()
        self._nav.insertItem(row, QListWidgetItem(title))
        self._pages.insertWidget(row, page)
        if current >= 0 and row <= current:  # the same section stays shown
            self._nav.setCurrentRow(current + 1)

    def show_page(self, title: str) -> None:
        for row in range(self._nav.count()):
            if self._nav.item(row).text() == title:
                self._nav.setCurrentRow(row)
                return
        raise KeyError(title)

    def set_hide_on_close(self, hide: bool) -> None:
        """Without a system tray there is nothing to hide into: closing then quits."""
        self._hide_on_close = hide

    def show_loading(self, message: str, *, switch_page: bool = True) -> None:
        """App-level states (signing in, offline, syncing couldn't start) show on the overview.
        `switch_page` opens it too: before signing in nothing else works; once signed in, a
        passing state (offline, retrying) mustn't pull the user out of what they're doing."""
        if switch_page:
            self.show_page(OVERVIEW)
        self._view.show_loading(message)

    def show_error(self, message: str, *, switch_page: bool = True) -> None:
        if switch_page:
            self.show_page(OVERVIEW)
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

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._pause.setText(_RESUME if paused else _PAUSE)

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
