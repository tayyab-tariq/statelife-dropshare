"""Settings (FR-CLIENT-04): account, sync folder (with its move), signed-in computers
(devices_card.py), preferences (preference_cards.py), and about with updates (OQ-41) and export
diagnostics; one line for problems above the sections and one for confirmations below them."""

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from dropshare_client.controllers import messages
from dropshare_client.controllers.devices_controller import DevicesController
from dropshare_client.controllers.diagnostics_controller import DiagnosticsController
from dropshare_client.controllers.settings_controller import (
    SettingsController,
)
from dropshare_client.controllers.update_controller import UpdateController, UpdateState
from dropshare_client.ui import theme
from dropshare_client.ui.cards import Value, button_row, card
from dropshare_client.ui.devices_card import DevicesCard
from dropshare_client.ui.forms import (
    announce,
    message_label,
    set_available,
    show_message,
    status_label,
)
from dropshare_client.ui.preference_cards import PreferenceCards

_STATUS_MS = 8000


class SettingsView(QScrollArea):
    change_password_requested = Signal()
    sign_out_requested = Signal()
    move_folder_requested = Signal()
    install_update_requested = Signal()  # show the update dialog

    def __init__(
        self,
        settings: SettingsController,
        devices: DevicesController,
        diagnostics: DiagnosticsController,
        updates: UpdateController,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._updates = updates
        self._update_error = ""
        self._user_check = False
        self._devices = devices
        space = theme.SPACE
        self._diagnostics = diagnostics
        self._lasting_error = ""
        self._message = ""  # what last happened here (an action failed); "" for none
        self._reload_message = ""  # the computers couldn't be listed again, until they are
        self._has_folder = False
        self._move_blocked = ""
        # Hook (tests replace it): where to save diagnostics; "" when cancelled.
        self.choose_file: Callable[[str], str] = lambda name: QFileDialog.getSaveFileName(
            self, "Export diagnostics", str(Path.home() / name), "Zip files (*.zip)"
        )[0]
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)

        title = QLabel(messages.SETTINGS)
        title.setProperty("textRole", "title")
        self._error = message_label("settingsError")
        self._status = status_label("settingsStatus")
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.setInterval(_STATUS_MS)
        self._status_timer.timeout.connect(self._status.clear)

        self._devices_card = DevicesCard(devices)
        self._devices_card.revoke_started.connect(lambda: self._show_error(""))

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(space.xxl, space.xl, space.xxl, space.xl)
        layout.setSpacing(space.lg)
        layout.addWidget(title)
        layout.addWidget(self._error)
        for section in (
            self._account_card(),
            self._folder_card(),
            self._devices_card,
            PreferenceCards(settings),
            self._about_card(),
        ):
            layout.addWidget(section)
        layout.addWidget(self._status)
        layout.addStretch()
        self.setWidget(content)
        self._connect_controllers()
        self._show_about()
        self._show_updates()

    # --- sections ------------------------------------------------------------------------

    def _account_card(self) -> QFrame:
        self._account = Value("Signed in as")
        account, layout = card("Account", [("Signed in as", self._account)])
        change_password = QPushButton("Change password…")  # Alt+C is in the header
        sign_out = QPushButton("Sign out…")  # Alt+S is in the header
        layout.addLayout(button_row(change_password, sign_out))
        change_password.clicked.connect(self.change_password_requested)
        sign_out.clicked.connect(self.sign_out_requested)
        self._account_buttons = (change_password, sign_out)
        return account

    def _folder_card(self) -> QFrame:
        self._folder = Value("Location")
        folder, layout = card("Sync folder", [("Location", self._folder)])
        self._move_folder = QPushButton("&Move…")
        layout.addLayout(button_row(self._move_folder))
        self._move_folder.clicked.connect(self.move_folder_requested)
        return folder

    def _about_card(self) -> QFrame:
        self._version = Value("Version")
        self._server = Value("Server")
        self._update_status = Value(messages.UPDATES)
        about, layout = card(
            "About",
            [
                ("Version", self._version),
                ("Server", self._server),
                (messages.UPDATES, self._update_status),
            ],
        )
        self._check_updates = QPushButton("Check for &updates")
        self._install_update = QPushButton("&Install update…")
        self._export_button = QPushButton("&Export diagnostics…")
        self._export_button.setToolTip("Save DropShare's logs and settings in a zip, for support")
        layout.addLayout(button_row(self._check_updates, self._install_update))
        layout.addLayout(button_row(self._export_button))
        self._check_updates.clicked.connect(self._check_for_updates)
        self._install_update.clicked.connect(self.install_update_requested)
        self._export_button.clicked.connect(self._export)
        return about

    def _connect_controllers(self) -> None:
        self._settings.changed.connect(self._show_about)
        self._updates.changed.connect(self._show_updates)
        self._settings.failed.connect(self._show_error)
        self._devices.action_failed.connect(self._show_error)
        self._devices.reload_failed.connect(self._devices_reload_failed)
        self._devices.loaded.connect(self._devices_reloaded)
        self._devices.revoked.connect(self._announce)
        self._diagnostics.exported.connect(self._announce)
        self._diagnostics.failed.connect(self._show_error)
        self._diagnostics.busy_changed.connect(self._exporting)

    @property
    def devices_card(self) -> DevicesCard:
        return self._devices_card

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._show_error("")
        self._devices.refresh()

    def set_account(self, text: str) -> None:
        self._account.set_value(text)

    def set_folder(self, path: str | None) -> None:
        self._folder.set_value(path or "Not chosen yet")
        self._has_folder = path is not None
        self._update_move()

    def set_move_blocked(self, why: str) -> None:
        """Move… is off, saying why, while a move runs or needs attention ("" when it isn't)."""
        self._move_blocked = why
        self._update_move()

    def set_lasting_error(self, text: str) -> None:
        """A problem that lasts (a folder move that needs attention): kept above the sections,
        before any other message, until it is cleared ("") when resolved or at sign-out."""
        self._lasting_error = text
        self._show_error("")

    def announce(self, text: str) -> None:
        """A confirmation from elsewhere (the folder moved), on this page's status line."""
        self._announce(text)

    def set_actions_enabled(self, enabled: bool, sign_out_blocked: str = "") -> None:
        """Off while a sign-in, password change or sign-out is in progress. Sign out is also
        off, saying why (`sign_out_blocked`), while the sync folder is being moved."""
        change_password, sign_out = self._account_buttons
        change_password.setEnabled(enabled)
        set_available(sign_out, enabled and not sign_out_blocked, sign_out_blocked)

    def _update_move(self) -> None:
        set_available(
            self._move_folder, self._has_folder and not self._move_blocked, self._move_blocked
        )

    def _show_about(self) -> None:
        self._version.set_value(self._settings.version)
        self._server.set_value(self._settings.server_url)

    def _check_for_updates(self) -> None:
        self._user_check = True  # its result is announced; background checks stay quiet
        self._show_error("")
        self._updates.check()

    def _show_updates(self) -> None:
        """The four states (NFR-UX-01): checking or downloading, a failure saying what to do
        (Check for updates tries again), up to date, and an update ready to install. Check for
        updates stays enabled while busy (pressing it again does nothing), so focus stays put."""
        updates = self._updates
        status = updates.status
        self._update_status.set_value(status)
        ready = updates.state is UpdateState.READY
        had_focus = self._check_updates.hasFocus()
        self._install_update.setVisible(ready)
        set_available(
            self._check_updates, not ready, messages.UPDATE_ALREADY_READY if ready else ""
        )
        if ready and had_focus:
            self._install_update.setFocus()
        if self._user_check and not updates.busy:
            self._user_check = False
            self._announce(status)
        if ready and updates.error and updates.error != self._update_error:
            self._show_error(updates.error)  # installing it failed; it's still ready
        self._update_error = updates.error

    def _exporting(self, busy: bool) -> None:
        self._export_button.setEnabled(not busy)
        if busy:
            announce(self._status, messages.EXPORTING_DIAGNOSTICS)

    def _export(self) -> None:
        if self._diagnostics.busy:
            return
        destination = self.choose_file(self._diagnostics.default_name())
        if destination:
            self._show_error("")
            self._diagnostics.export(destination)

    def _show_error(self, text: str) -> None:
        """A message above the sections ("" clears it), always after the lasting one. It
        replaces the previous one, and a failed listing of the computers with it."""
        self._message, self._reload_message = text, ""
        self._show_messages()

    def _devices_reload_failed(self, text: str) -> None:
        """Shown after the message already there, which stays true: the next listing that
        works takes away only this part."""
        self._reload_message = text
        self._show_messages()

    def _devices_reloaded(self) -> None:
        if self._reload_message:
            self._reload_message = ""
            self._show_messages(alert=False)  # what stays was announced when it came

    def _show_messages(self, *, alert: bool = True) -> None:
        reload = "" if self._reload_message == self._message else self._reload_message
        parts = (self._lasting_error, self._message, reload)
        show_message(self._error, "\n\n".join(part for part in parts if part), alert=alert)

    def _announce(self, text: str) -> None:
        self._show_error("")
        announce(self._status, text)
        self._status_timer.start()
