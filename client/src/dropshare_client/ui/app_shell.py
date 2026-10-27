"""Which window is showing when: the sign-in flow, first-run folder choice, main window and
tray, and syncing while signed in (FR-AUTH-01/02/04/05, FR-SYNC-01/05/08, FR-CLIENT-01).
Reacts to controller signals only."""

from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog

from dropshare_client.controllers import messages
from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.sync_controller import SyncController, SyncStatus
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.ui.change_password_dialog import ChangePasswordDialog
from dropshare_client.ui.main_window import MainWindow
from dropshare_client.ui.mass_delete_dialog import MassDeleteDialog
from dropshare_client.ui.sign_in_dialog import SignInDialog
from dropshare_client.ui.sign_out_dialog import SignOutDialog
from dropshare_client.ui.sync_folder_dialog import SyncFolderDialog
from dropshare_client.ui.theme import TrayState
from dropshare_client.ui.tray import Tray
from dropshare_shared.schemas.users import UserOut

_OFFLINE_RETRY_MS = 30_000
_SIGN_OUT_RETRY_MS = 500  # the session was busy (e.g. an automatic resume): try again shortly
_CONFLICT_GATHER_MS = 3_000  # several conflicts at once become one notification (ui.md §7)
_NO_FOLDER = "no sync folder chosen"
_CONFIRMING = "waiting for you to confirm deleting files"
# How the sync status shows in the tray; Paused is the shell's own overlay (see _set_connection).
_TRAY_STATE = {
    SyncStatus.SYNCING: TrayState.SYNCING,
    SyncStatus.UP_TO_DATE: TrayState.UP_TO_DATE,
    SyncStatus.OFFLINE: TrayState.OFFLINE,
    SyncStatus.ERROR: TrayState.ERROR,
}


def _bring_forward(dialog: QDialog) -> None:
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()


class AppShell(QObject):
    def __init__(
        self,
        session: SessionController,
        folders: SyncFolderController,
        sync: SyncController,
        *,
        background: bool = False,
        tray_available: bool = True,
    ) -> None:
        super().__init__()
        self._session = session
        self._folders = folders
        self._sync = sync
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
        self.delete_dialog: MassDeleteDialog | None = None
        self._offline_retry = QTimer(self)
        self._offline_retry.setSingleShot(True)
        self._offline_retry.setInterval(_OFFLINE_RETRY_MS)
        self._offline_retry.timeout.connect(self._resume)
        self._conflicts: list[str] = []
        self._conflict_notice = QTimer(self)
        self._conflict_notice.setSingleShot(True)
        self._conflict_notice.setInterval(_CONFLICT_GATHER_MS)
        self._conflict_notice.timeout.connect(self._announce_conflicts)

        session.signed_in.connect(self._signed_in)
        session.password_change_required.connect(lambda: self._ask_for_new_password(forced=True))
        session.password_changed.connect(self._password_changed)
        session.needs_sign_in.connect(self._needs_sign_in)
        session.offline.connect(self._offline)
        session.upgrade_required.connect(self._upgrade_required)
        session.signed_out.connect(self._signed_out)
        session.busy_changed.connect(lambda busy: self.window.set_actions_enabled(not busy))
        folders.saved.connect(self._folder_saved)
        folders.cleared.connect(self._folder_cleared)
        sync.status_changed.connect(self._sync_status)
        sync.conflict.connect(self._conflict)
        sync.deletes_need_confirmation.connect(self._confirm_deletes)
        sync.session_expired.connect(self._needs_sign_in)
        sync.folder_rejected.connect(self._folder_rejected)
        sync.start_failed.connect(self._sync_start_failed)

        self.window.open_folder_requested.connect(self.open_sync_folder)
        self.window.choose_folder_requested.connect(self._choose_folder)
        self.window.change_password_requested.connect(
            lambda: self._ask_for_new_password(forced=False)
        )
        self.window.sign_out_requested.connect(self._confirm_sign_out)
        self.window.retry_requested.connect(self._resume)
        self.window.quit_requested.connect(self.quit)
        self.window.pause_toggled.connect(self._pause_toggled)
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
        for dialog in (self.password_dialog, self.folder_dialog, self.delete_dialog):
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
        if self._folders.current_folder is None:
            self._set_connection(TrayState.PAUSED, _NO_FOLDER)
        elif not self._sync.running:
            self._set_connection(TrayState.SYNCING)
        self._show_account()
        if not self._background:
            self.window.bring_to_front()
        if self._folders.current_folder is None:
            self._choose_folder()
        else:
            self._sync.start(user)

    def _password_changed(self) -> None:
        if self.password_dialog is not None:
            self.password_dialog.done_changing()  # `signed_in` follows

    def _ask_for_new_password(self, *, forced: bool) -> None:
        self.sign_in_dialog.done_signing_in()
        if self.password_dialog is None:
            dialog = ChangePasswordDialog(self._session, forced=forced, parent=self.window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.finished.connect(
                lambda result: self._password_dialog_closed(dialog, result, forced)
            )
            self.password_dialog = dialog
        _bring_forward(self.password_dialog)

    def _password_dialog_closed(
        self, dialog: ChangePasswordDialog, result: int, forced: bool
    ) -> None:
        if self.password_dialog is not dialog:
            return  # closed by the shell (signed out), not by the user
        self.password_dialog = None
        if forced and result == QDialog.DialogCode.Rejected:
            # The temporary password can't be used for anything else.
            self._sign_out(remove_files=False)

    def _needs_sign_in(self, message: str) -> None:
        self._user = None
        self._sync.stop()
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
                self.window.set_status("Signing out…")
                self._sign_out(remove_files=dialog.remove_files)
        finally:
            dialog.deleteLater()

    def _sign_out(self, *, remove_files: bool) -> None:
        """Syncing stops first: nothing may write into the folder while it's being moved
        (FR-AUTH-04). The index is only forgotten once the files are gone (`_signed_out`)."""
        self._offline_retry.stop()
        self.window.set_actions_enabled(False)
        self._sync.stop(then=lambda: self._request_sign_out(remove_files))

    def _request_sign_out(self, remove_files: bool) -> None:
        if not self._session.sign_out(remove_files=remove_files):
            QTimer.singleShot(_SIGN_OUT_RETRY_MS, lambda: self._request_sign_out(remove_files))

    def _signed_out(self, note: str) -> None:
        self._user = None
        # The index says whose files the folder holds; it goes only when the folder went.
        self._sync.stop(forget=self._folders.current_folder is None)
        self._close_dialogs()
        self._set_connection(TrayState.SIGNED_OUT)
        self.window.hide()
        self.window.clear_account()
        self._show_sign_in(note)

    def _close_dialogs(self) -> None:
        """Close what's open without answering it: each is detached first, so its own
        `finished` handler sees it's no longer current (no sign-out, no approved deletes)."""
        dialogs = (self.password_dialog, self.folder_dialog, self.delete_dialog)
        self.password_dialog = self.folder_dialog = None
        self.delete_dialog = None
        for dialog in dialogs:
            if dialog is not None:
                dialog.done(QDialog.DialogCode.Rejected)

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
        if self._user is not None:
            self._set_connection(TrayState.SYNCING)
            self._sync.start(self._user)
        self._show_account()

    def _folder_rejected(self, message: str) -> None:
        """The folder holds another account's files: keep them, and ask for a new folder."""
        self._notify("Choose a new sync folder", message)
        self._set_connection(TrayState.PAUSED, _NO_FOLDER)
        self._folders.clear()  # then `_folder_cleared`

    def _folder_cleared(self) -> None:
        self._show_account()  # the empty state offers "Choose sync folder…" if cancelled
        if self._user is not None:
            self._choose_folder()

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
        if paused:
            self._sync.pause()
        else:
            self._sync.resume()
        self._show_paused(paused)

    def _show_paused(self, paused: bool) -> None:
        self._paused = paused
        self.tray.set_paused(paused)
        self.window.set_paused(paused)
        self._set_connection(self._connection)

    # --- syncing -------------------------------------------------------------------------

    def _sync_status(self, status: str, message: str) -> None:
        if self._user is None:
            return  # a report from just before signing out
        match SyncStatus(status):
            case SyncStatus.UPGRADE_REQUIRED:
                self._upgrade_required(message)
            case SyncStatus.NEEDS_CONFIRMATION:
                self._set_connection(TrayState.PAUSED, _CONFIRMING)
            case SyncStatus.PAUSED | SyncStatus.STOPPED:
                pass  # pause is shown by the shell's own flag; stopping follows sign-out
            case known:
                self._set_connection(_TRAY_STATE[known], message)

    def _sync_start_failed(self, message: str) -> None:
        """Try again (the error view's button) resumes the session, which restarts syncing."""
        if self._user is not None:
            self._set_connection(TrayState.ERROR, message)
            self.window.show_error(message)

    def _conflict(self, message: str) -> None:
        self._conflicts.append(message)
        if not self._conflict_notice.isActive():
            self._conflict_notice.start()

    def _announce_conflicts(self) -> None:
        gathered, self._conflicts = self._conflicts, []
        if not gathered or self._user is None:
            return
        text = gathered[0] if len(gathered) == 1 else messages.conflicts_message(len(gathered))
        self._notify("Conflicting changes", text)

    def _notify(self, title: str, message: str) -> None:
        """A tray notification, or the window's status line when there is no tray."""
        if self._tray_available:
            self.tray.notify(title, message)
        else:
            self.window.set_status(message)

    def _confirm_deletes(self, message: str) -> None:
        """FR-SYNC-08. Not modal: syncing waits, the rest of the app doesn't."""
        if self._user is None:
            return
        old, self.delete_dialog = self.delete_dialog, None
        if old is not None:  # detached first, so closing it can't answer the new question
            old.done(QDialog.DialogCode.Rejected)
        dialog = MassDeleteDialog(message, parent=self.window)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.finished.connect(lambda result: self._deletes_answered(dialog, result))
        self.delete_dialog = dialog
        if self._background or not self.window.isVisible():
            self._notify("Confirm deleting files", message)
        _bring_forward(dialog)

    def _deletes_answered(self, dialog: MassDeleteDialog, result: int) -> None:
        if self.delete_dialog is not dialog:
            return  # replaced by a newer question, or closed by sign-out
        self.delete_dialog = None
        approved = result == QDialog.DialogCode.Accepted
        self._sync.answer_deletes(approved)
        if not approved:
            self._show_paused(True)

    def _set_connection(self, state: TrayState, detail: str = "") -> None:
        """Show the connection state, unless syncing is paused while connected: signed out,
        errors and offline always win, so the tray never hides a problem behind "Paused"."""
        self._connection = state
        shown = state
        if self._paused and state in (TrayState.UP_TO_DATE, TrayState.SYNCING):
            shown = TrayState.PAUSED
        self.tray.set_state(shown, detail or ("paused" if self._paused and shown != state else ""))
        if self._user is not None:
            self.window.set_status(f"{shown.value}: {detail}" if detail else shown.value)
