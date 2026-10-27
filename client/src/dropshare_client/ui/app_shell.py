"""Which window is showing when: the sign-in flow, first-run folder choice, main window and
tray (FR-AUTH-01/02/04/05, FR-SYNC-01, FR-CLIENT-01). Reacts to controller signals only."""

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog

from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.ui.change_password_dialog import ChangePasswordDialog
from dropshare_client.ui.main_window import MainWindow
from dropshare_client.ui.sign_in_dialog import SignInDialog
from dropshare_client.ui.sign_out_dialog import SignOutDialog
from dropshare_client.ui.sync_folder_dialog import SyncFolderDialog
from dropshare_client.ui.theme import TrayState
from dropshare_client.ui.tray import Tray
from dropshare_shared.schemas.users import UserOut

_OFFLINE_RETRY_MS = 30_000
# Placeholder until the sync engine reports real progress (roadmap M5).
_SIGNED_IN_STATE = TrayState.UP_TO_DATE


def _bring_forward(dialog: QDialog) -> None:
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


class AppShell(QObject):
    def __init__(
        self,
        session: SessionController,
        folders: SyncFolderController,
        *,
        background: bool = False,
        tray_available: bool = True,
    ) -> None:
        super().__init__()
        self._session = session
        self._folders = folders
        # Autostart: stay in the tray until the user does something.
        self._background = background and tray_available
        self._tray_available = tray_available
        self._user: UserOut | None = None
        self._connection = TrayState.SIGNED_OUT  # what the server side says
        self._paused = False
        self.window = MainWindow()
        self.window.set_hide_on_close(tray_available)
        self.tray = Tray()
        self.sign_in_dialog = SignInDialog(session)
        self.password_dialog: ChangePasswordDialog | None = None
        self.folder_dialog: SyncFolderDialog | None = None
        self._offline_retry = QTimer(self)
        self._offline_retry.setSingleShot(True)
        self._offline_retry.setInterval(_OFFLINE_RETRY_MS)
        self._offline_retry.timeout.connect(self._resume)

        session.signed_in.connect(self._signed_in)
        session.password_change_required.connect(lambda: self._ask_for_new_password(forced=True))
        session.password_changed.connect(self._password_changed)
        session.needs_sign_in.connect(self._needs_sign_in)
        session.offline.connect(self._offline)
        session.upgrade_required.connect(self._upgrade_required)
        session.signed_out.connect(self._signed_out)
        session.busy_changed.connect(lambda busy: self.window.set_actions_enabled(not busy))
        folders.saved.connect(self._folder_saved)

        self.window.open_folder_requested.connect(self.open_sync_folder)
        self.window.choose_folder_requested.connect(self._choose_folder)
        self.window.change_password_requested.connect(
            lambda: self._ask_for_new_password(forced=False)
        )
        self.window.sign_out_requested.connect(self._confirm_sign_out)
        self.window.retry_requested.connect(self._resume)
        self.window.quit_requested.connect(self.quit)
        self.tray.open_folder_requested.connect(self.open_sync_folder)
        self.tray.open_app_requested.connect(self.open_app)
        self.tray.pause_toggled.connect(self._pause_toggled)
        self.tray.quit_requested.connect(self.quit)
        self.sign_in_dialog.rejected.connect(self._sign_in_cancelled)

    def start(self) -> None:
        if self._tray_available:
            self.tray.show()
        self.window.show_loading("Signing in…")
        if not self._background:
            self.window.bring_to_front()
        self._resume()

    def open_app(self) -> None:
        """Tray click, "Open DropShare", or a second launch (FR-CLIENT-07). Whatever the user
        was in the middle of comes to the front."""
        self._background = False
        for dialog in (self.password_dialog, self.folder_dialog):
            if dialog is not None:
                _bring_forward(dialog)
                return
        if self._user is None and self._connection is TrayState.SIGNED_OUT:
            self._show_sign_in(None)
        else:
            self.window.bring_to_front()

    def open_sync_folder(self) -> None:
        folder = self._folders.current_folder
        if folder is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(folder))))

    def quit(self) -> None:
        self._offline_retry.stop()
        self.tray.hide()
        self.window.quit()
        QApplication.quit()

    # --- session -------------------------------------------------------------------------

    def _resume(self) -> None:
        self._offline_retry.stop()
        if self.window.view.current_state() != "error":  # automatic retries don't flicker
            self.window.show_loading("Signing in…")
        if not self._session.resume():
            self._offline_retry.start()  # busy with something else: try again later

    def _signed_in(self, user: UserOut) -> None:
        self._user = user
        self.sign_in_dialog.done_signing_in()
        self._set_connection(_SIGNED_IN_STATE)
        self._show_account()
        if not self._background:
            self.window.bring_to_front()
        if self._folders.current_folder is None:
            self._choose_folder()

    def _password_changed(self) -> None:
        if self.password_dialog is not None:
            self.password_dialog.done_changing()  # `signed_in` follows

    def _ask_for_new_password(self, *, forced: bool) -> None:
        self.sign_in_dialog.done_signing_in()
        if self.password_dialog is None:
            dialog = ChangePasswordDialog(self._session, forced=forced, parent=self.window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.finished.connect(lambda result: self._password_dialog_closed(result, forced))
            self.password_dialog = dialog
        _bring_forward(self.password_dialog)

    def _password_dialog_closed(self, result: int, forced: bool) -> None:
        self.password_dialog = None
        if forced and result == QDialog.DialogCode.Rejected:
            # The temporary password can't be used for anything else.
            self._session.sign_out(remove_files=False)

    def _needs_sign_in(self, message: str) -> None:
        self._user = None
        self._close_dialogs()
        self._set_connection(TrayState.SIGNED_OUT)
        self.window.hide()
        self.window.clear_account()
        if not self._background or message:
            self._show_sign_in(message)

    def _sign_in_cancelled(self) -> None:
        self._set_connection(TrayState.SIGNED_OUT)

    def _show_sign_in(self, message: str | None) -> None:
        """`message` None keeps whatever the dialog already says."""
        self._background = False
        if message is not None:
            self.sign_in_dialog.show_message(message)
        _bring_forward(self.sign_in_dialog)

    def _offline(self, message: str) -> None:
        self._set_connection(TrayState.OFFLINE)
        self.window.show_error(message)
        self._offline_retry.start()

    def _upgrade_required(self, message: str) -> None:
        self._set_connection(TrayState.ERROR, message)
        self.window.show_error(message)
        self.window.bring_to_front()

    def _confirm_sign_out(self) -> None:
        dialog = SignOutDialog(self._folders.current_folder, parent=self.window)
        try:
            if dialog.exec() == QDialog.DialogCode.Accepted:
                remove = dialog.remove_files
                self.window.set_status("Signing out…")
                self._session.sign_out(remove_files=remove)
        finally:
            dialog.deleteLater()

    def _signed_out(self, note: str) -> None:
        self._user = None
        self._close_dialogs()
        self._set_connection(TrayState.SIGNED_OUT)
        self.window.hide()
        self.window.clear_account()
        self._show_sign_in(note)

    def _close_dialogs(self) -> None:
        for dialog in (self.password_dialog, self.folder_dialog):
            if dialog is not None:
                dialog.done(QDialog.DialogCode.Accepted)  # not a rejection: no sign-out
        self.password_dialog = self.folder_dialog = None

    # --- sync folder ---------------------------------------------------------------------

    def _choose_folder(self) -> None:
        if self.folder_dialog is None:
            dialog = SyncFolderDialog(self._folders, parent=self.window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.finished.connect(self._folder_dialog_closed)
            self.folder_dialog = dialog
        _bring_forward(self.folder_dialog)

    def _folder_saved(self, _: str) -> None:
        if self.folder_dialog is not None:
            self.folder_dialog.done_choosing()
        self._show_account()

    def _folder_dialog_closed(self, _: int) -> None:
        self.folder_dialog = None

    # --- status --------------------------------------------------------------------------

    def _show_account(self) -> None:
        if self._user is not None:
            self.window.show_account(
                self._user,
                self._session.server_url,
                self._folders.current_folder,
                self.tray.state.value,
            )

    def _pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self.tray.set_paused(paused)
        self._set_connection(self._connection)

    def _set_connection(self, state: TrayState, detail: str = "") -> None:
        """Show the connection state, unless syncing is paused while connected: signed out,
        errors and offline always win, so the tray never hides a problem behind "Paused"."""
        self._connection = state
        shown = state
        if self._paused and state in (TrayState.UP_TO_DATE, TrayState.SYNCING):
            shown = TrayState.PAUSED
        self.tray.set_state(shown, detail or ("paused" if self._paused and shown != state else ""))
        if self._user is not None:
            self.window.set_status(shown.value)
