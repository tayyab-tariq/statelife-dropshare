"""Which window is showing when: the sign-in flow, first-run folder choice, main window and
tray, and syncing while signed in (FR-AUTH-01/02/04/05, FR-SYNC-01/05/08, FR-CLIENT-01).
Reacts to controller signals only."""

import logging
import uuid
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QDialog

from dropshare_client.controllers import messages
from dropshare_client.controllers.browser_controller import BrowserController
from dropshare_client.controllers.devices_controller import DevicesController
from dropshare_client.controllers.diagnostics_controller import DiagnosticsController
from dropshare_client.controllers.file_actions_controller import FileActionsController
from dropshare_client.controllers.folder_move_controller import FolderMoveController
from dropshare_client.controllers.notification_controller import NotificationController
from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.settings_controller import SettingsController
from dropshare_client.controllers.share_controller import ShareController, node_of
from dropshare_client.controllers.shared_controller import SharedController
from dropshare_client.controllers.sync_controller import SyncController, SyncStatus
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.controllers.sync_log_controller import (
    ActivityController,
    CantSyncController,
)
from dropshare_client.controllers.transfers_controller import TransfersController
from dropshare_client.controllers.trash_controller import TrashController
from dropshare_client.controllers.upload_controller import UploadController
from dropshare_client.ui.browser_view import BrowserView
from dropshare_client.ui.change_password_dialog import ChangePasswordDialog
from dropshare_client.ui.main_window import OVERVIEW, MainWindow
from dropshare_client.ui.mass_delete_dialog import MassDeleteDialog
from dropshare_client.ui.move_folder_dialog import MoveFolderDialog
from dropshare_client.ui.settings_view import SettingsView
from dropshare_client.ui.share_dialog import ShareDialog
from dropshare_client.ui.shared_view import SharedWithMeView
from dropshare_client.ui.sign_in_dialog import SignInDialog
from dropshare_client.ui.sign_out_dialog import SignOutDialog
from dropshare_client.ui.sync_folder_dialog import SyncFolderDialog
from dropshare_client.ui.sync_log_views import ActivityView, CantSyncView
from dropshare_client.ui.theme import TrayState
from dropshare_client.ui.transfers_view import TransfersView
from dropshare_client.ui.trash_view import TrashView
from dropshare_client.ui.tray import Tray
from dropshare_shared.schemas.users import UserOut

logger = logging.getLogger(__name__)

_OFFLINE_RETRY_MS = 30_000
_SIGN_OUT_RETRY_MS = 500  # the session was busy (e.g. an automatic resume): try again shortly
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
        transfers: TransfersController,
        browser: BrowserController,
        file_actions: FileActionsController,
        uploads: UploadController,
        *,
        share: ShareController,
        shared: SharedController,
        trash: TrashController,
        notifications: NotificationController,
        activity: ActivityController,
        cant_sync: CantSyncController,
        settings: SettingsController,
        devices: DevicesController,
        folder_move: FolderMoveController,
        diagnostics: DiagnosticsController,
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
        self._detail = ""
        self._transfers = transfers
        self._browser = browser
        self._paused = False
        self.window = MainWindow()
        self.window.set_hide_on_close(tray_available)
        self._uploads = uploads
        self.browser_view = BrowserView(browser, file_actions, uploads)
        self.window.add_page(messages.MY_FILES, self.browser_view, 0)
        self._shared = shared
        self.shared_view = SharedWithMeView(shared)
        self.window.add_page("Shared with me", self.shared_view, 1)
        self.share_dialog = ShareDialog(share, self.window)
        self._pending_link: uuid.UUID | None = None  # opened once signed in
        self._trash = trash
        self.trash_view = TrashView(trash)
        self.window.add_page(messages.TRASH, self.trash_view, 2)
        self.transfers_view = TransfersView(transfers)
        self.window.add_page("Transfers", self.transfers_view, 3)
        self._activity = activity
        self.activity_view = ActivityView(activity)
        self.window.add_page(messages.ACTIVITY, self.activity_view, 4)
        self._cant_sync = cant_sync
        self.cant_sync_view = CantSyncView(cant_sync)
        self.window.add_page(messages.CANT_SYNC, self.cant_sync_view, 5)
        self._settings = settings
        self._devices = devices
        self.settings_view = SettingsView(settings, devices, diagnostics)
        self.window.add_page(messages.SETTINGS, self.settings_view)
        self.window.show_page(messages.MY_FILES)
        self.tray = Tray()
        self.sign_in_dialog = SignInDialog(session)
        self.password_dialog: ChangePasswordDialog | None = None
        self.folder_dialog: SyncFolderDialog | None = None
        self.delete_dialog: MassDeleteDialog | None = None
        self._folder_move = folder_move
        self._session_working = False
        self.move_dialog: MoveFolderDialog | None = None
        self._offline_retry = QTimer(self)
        self._offline_retry.setSingleShot(True)
        self._offline_retry.setInterval(_OFFLINE_RETRY_MS)
        self._offline_retry.timeout.connect(self._resume)
        self._notifications = notifications

        session.signed_in.connect(self._signed_in)
        session.password_change_required.connect(lambda: self._ask_for_new_password(forced=True))
        session.password_changed.connect(self._password_changed)
        session.needs_sign_in.connect(self._needs_sign_in)
        session.offline.connect(self._offline)
        session.upgrade_required.connect(self._upgrade_required)
        session.signed_out.connect(self._signed_out)
        session.busy_changed.connect(self._session_busy)
        folders.saved.connect(self._folder_saved)
        folders.cleared.connect(self._folder_cleared)
        sync.status_changed.connect(self._sync_status)
        sync.conflict.connect(notifications.conflict)
        sync.problems_changed.connect(notifications.problems_changed)
        sync.activity_added.connect(notifications.activity_added)
        notifications.notify.connect(self._notify)
        sync.deletes_need_confirmation.connect(self._confirm_deletes)
        sync.session_expired.connect(self._needs_sign_in)
        sync.folder_rejected.connect(self._folder_rejected)
        sync.start_failed.connect(self._sync_start_failed)
        transfers.summary_changed.connect(
            lambda _: self._set_connection(self._connection, self._detail)
        )

        self.window.open_folder_requested.connect(self.open_sync_folder)
        self.window.choose_folder_requested.connect(self._choose_folder)
        self.window.change_password_requested.connect(
            lambda: self._ask_for_new_password(forced=False)
        )
        self.window.sign_out_requested.connect(self._confirm_sign_out)
        self.settings_view.change_password_requested.connect(
            lambda: self._ask_for_new_password(forced=False)
        )
        self.settings_view.sign_out_requested.connect(self._confirm_sign_out)
        self.settings_view.move_folder_requested.connect(self._ask_move_folder)
        folder_move.moved.connect(self._folder_moved)
        folder_move.attention.connect(self._move_needs_attention)
        folder_move.state_changed.connect(self._update_actions)
        self.window.retry_requested.connect(self._resume)
        self.window.quit_requested.connect(self.quit)
        self.window.pause_toggled.connect(self._pause_toggled)
        self.tray.open_folder_requested.connect(self.open_sync_folder)
        self.tray.open_app_requested.connect(self.open_app)
        self.tray.recent_activity_requested.connect(self._show_activity)
        self.tray.settings_requested.connect(self._show_settings)
        self.tray.pause_toggled.connect(self._pause_toggled)
        self.tray.quit_requested.connect(self.quit)
        self.sign_in_dialog.rejected.connect(self._sign_in_cancelled)
        self.browser_view.share_requested.connect(self.share_dialog.open_for)
        browser.shared_file_linked.connect(self._show_shared_item)
        shared.new_shares.connect(notifications.new_shares)

    def start(self) -> None:
        if self._tray_available:
            self.tray.show()
        self.window.show_loading("Signing in…")
        if not self._background:
            self.window.bring_to_front()
        self._folder_move.recover_at_start()  # syncing waits for it
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

    def _show_activity(self) -> None:
        """The tray's "Recent activity" (FR-CLIENT-01/03)."""
        self._show_page(messages.ACTIVITY)

    def _show_settings(self) -> None:
        """The tray's "Settings" (FR-CLIENT-01/04)."""
        self._show_page(messages.SETTINGS)

    def _show_page(self, title: str) -> None:
        self.open_app()
        if self._user is not None:
            self.window.show_page(title)

    def _session_busy(self, busy: bool) -> None:
        self._session_working = busy
        self._update_actions()

    def _update_actions(self) -> None:
        """Account actions are off while the session is busy. Sign out and Move… are also off
        while the sync folder is being moved or a move needs attention: sign-out may move that
        very folder."""
        moving = self._folder_move.busy or self._folder_move.needs_attention
        sign_out_blocked = messages.WAIT_FOR_MOVE if moving else ""
        self.window.set_actions_enabled(not self._session_working, sign_out_blocked)
        self.settings_view.set_actions_enabled(not self._session_working, sign_out_blocked)
        self.settings_view.set_move_blocked(messages.MOVE_UNAVAILABLE if moving else "")

    def _forget_account(self) -> None:
        """Signed out, or the session ended: nothing of this account stays for the next."""
        self._folder_move.signed_out()
        self._notifications.reset()
        self._activity.clear()
        self._cant_sync.clear()
        self._devices.clear()
        self.settings_view.set_account("")
        self.settings_view.set_lasting_error("")

    def open_link(self, link: str) -> None:
        """A `statelife-dropshare://` link (FR-SHARE-07), from the command line or another
        start of the app. Anything that isn't exactly such a link is ignored."""
        node_id = node_of(link)
        if node_id is None:
            logger.warning("Ignored a link that isn't a DropShare link")
            return
        self.open_app()
        if self._user is None:
            self._pending_link = node_id  # after signing in
            return
        self.window.show_page(messages.MY_FILES)
        self._browser.open_node(node_id)

    def _show_shared_item(self, node_id: uuid.UUID) -> None:
        """A link to a file shared on its own: it lives in Shared with me (FR-SHARE-03/07)."""
        self.window.show_page("Shared with me")
        self.shared_view.select(node_id)

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
            self.window.show_loading("Signing in…", switch_page=self._user is None)
        if not self._session.resume():
            self._offline_retry.start()  # busy with something else: try again later

    def _signed_in(self, user: UserOut) -> None:
        self._user = user
        self.sign_in_dialog.done_signing_in()
        self._browser.start(user.id)
        # Nothing of whoever was signed in before (the session may have just expired) shows,
        # not even for the moment the window comes up on these pages.
        self._shared.clear()
        self._trash.clear()
        self._uploads.set_user(user)
        self._notifications.account_loaded(user)
        self._settings.apply_autostart()
        if self._folders.current_folder is None:
            self._set_connection(TrayState.PAUSED, _NO_FOLDER)
        elif not self._sync.running:
            self._set_connection(TrayState.SYNCING)
        self._show_account()
        if not self._background:
            self.window.bring_to_front()
        if self._folders.current_folder is None:
            self.window.show_page(OVERVIEW)  # its empty state asks for a folder
            self._choose_folder()
        else:
            self.window.show_page(messages.MY_FILES)
            self._sync.start(user)
        self._shared.start()
        if self._pending_link is not None:
            node_id, self._pending_link = self._pending_link, None
            self._browser.open_node(node_id)

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
        self._forget_account()
        self._sync.stop()
        self._shared.clear()
        self._trash.clear()
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
        self.window.show_error(message, switch_page=self._user is None)
        self._offline_retry.start()

    def _upgrade_required(self, message: str) -> None:
        self._set_connection(TrayState.ERROR, message)
        self.window.show_error(message)
        self.window.bring_to_front()

    def _confirm_sign_out(self) -> None:
        # Sign-out may move the very folder being moved, or one a move hasn't finished with.
        if self._folder_move.busy or self._folder_move.needs_attention:
            self.window.set_status(messages.WAIT_FOR_MOVE)  # the buttons are off too
            return
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
        self._forget_account()
        self._browser.clear()
        self._shared.clear()
        self._trash.clear()
        self.share_dialog.hide()
        self._uploads.set_user(None)
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

    def _ask_move_folder(self) -> None:
        if self._user is None or self._folders.current_folder is None:
            return
        if self.move_dialog is None:
            dialog = MoveFolderDialog(self._folder_move, parent=self.window)
            dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dialog.move_requested.connect(self._move_folder)
            dialog.finished.connect(lambda _: self._move_dialog_closed(dialog))
            self.move_dialog = dialog
        _bring_forward(self.move_dialog)

    def _move_dialog_closed(self, dialog: MoveFolderDialog) -> None:
        if self.move_dialog is dialog:
            self.move_dialog = None

    def _move_folder(self, path: str) -> None:
        if self._user is not None:
            self._folder_move.move(path, self._user)

    def _move_needs_attention(self, message: str) -> None:
        if self.move_dialog is not None:
            self.move_dialog.done(QDialog.DialogCode.Rejected)
        self._notify("DropShare folder", message)
        self.settings_view.set_lasting_error(message)

    def _folder_moved(self, message: str) -> None:
        self._show_account()
        self.settings_view.set_lasting_error("")  # resolved, including by start-up recovery
        self.settings_view.announce(message)

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
        self.settings_view.set_folder(self._folders.current_folder)
        if self._user is not None:
            self.settings_view.set_account(f"{self._user.display_name} ({self._user.email})")
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
                if known is SyncStatus.UP_TO_DATE and self._connection is TrayState.SYNCING:
                    self._browser.refresh()  # a round of syncing ended: show what changed
                self._set_connection(_TRAY_STATE[known], message)

    def _sync_start_failed(self, message: str) -> None:
        """Try again (the error view's button) resumes the session, which restarts syncing."""
        if self._user is not None:
            self._set_connection(TrayState.ERROR, message)
            self.window.show_error(message)

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
        self._connection, self._detail = state, detail
        shown = state
        if self._paused and state in (TrayState.UP_TO_DATE, TrayState.SYNCING):
            shown = TrayState.PAUSED
        summary = self._transfers.summary if shown is TrayState.SYNCING else ""
        self.tray.set_state(
            shown, detail or ("paused" if self._paused and shown != state else ""), summary
        )
        if self._user is not None:
            self.window.set_status(f"{shown.value}: {detail}" if detail else shown.value)
