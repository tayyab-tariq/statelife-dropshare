"""Sign-in lifecycle for the UI: resume, sign in, forced or voluntary password change, sign out.

Every service call runs on the thread pool; results arrive as signals on the GUI thread. Only
one session operation runs at a time: a sign-out racing a password change could otherwise
store a new refresh token after the old one was forgotten (audit B6).
"""

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.platform.base import RecycleBin
from dropshare_client.services import sync_folder
from dropshare_client.services.auth import AuthService, Session, SignOut
from dropshare_client.services.config import DEFAULT_SERVER_URL, ConfigStore, check_server_url
from dropshare_client.services.errors import (
    ApiError,
    InvalidInputError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.auth import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from dropshare_shared.schemas.users import EMAIL_MAX_LENGTH

logger = logging.getLogger(__name__)


class SessionController(QObject):
    busy_changed = Signal(bool)
    signed_in = Signal(object)  # UserOut: ready to use the app
    password_change_required = Signal()  # signed in with a temporary password (FR-AUTH-05)
    sign_in_failed = Signal(str)
    needs_sign_in = Signal(str)  # why, or "" on first run
    offline = Signal(str)  # couldn't reach the server; the saved sign-in is kept
    upgrade_required = Signal(str)
    password_changed = Signal()  # done; `signed_in` follows once the account is loaded
    password_change_failed = Signal(str)
    signed_out = Signal(str)  # anything the user should know, or ""

    def __init__(
        self,
        auth: AuthService,
        config: ConfigStore,
        recycle_bin: RecycleBin,
        app_data: Path,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._auth = auth
        self._config = config
        self._recycle_bin = recycle_bin
        self._app_data = app_data
        self._worker = worker or Worker()
        self._busy = False

    @property
    def server_url(self) -> str:
        return self._config.config.server_url

    @property
    def default_server_url(self) -> str:
        return DEFAULT_SERVER_URL

    @property
    def busy(self) -> bool:
        return self._busy

    def resume(self) -> bool:
        """Continue the saved session without asking for the password (FR-AUTH-02). False if
        another session operation is still running."""
        return self._run(self._auth.resume, self._resumed, self._load_failed)

    def _resumed(self, session: Session | None) -> None:
        if session is None:
            self.needs_sign_in.emit("")
        else:
            self._deliver(session)

    def _load_failed(self, exc: Exception) -> None:
        if isinstance(exc, OfflineError):
            self.offline.emit(messages.OFFLINE)
        elif isinstance(exc, UpgradeRequiredError):
            self.upgrade_required.emit(messages.UPGRADE)
        elif isinstance(exc, ApiError) and exc.code == ErrorCode.PASSWORD_CHANGE_REQUIRED:
            self.password_change_required.emit()
        else:
            self._log_unexpected(exc)
            self.needs_sign_in.emit(messages.message_for(exc))

    def sign_in(self, server_url: str, login: str, password: str) -> bool:
        try:
            url = check_server_url(server_url)
        except ValueError as exc:
            self.sign_in_failed.emit(str(exc))
            return False
        login = login.strip()
        problem = _sign_in_problem(login, password)
        if problem:
            self.sign_in_failed.emit(problem)
            return False

        def sign_in() -> Session:
            session = self._auth.sign_in(url, login, password)
            if url != self._config.config.server_url:
                self._config.update(server_url=url)
            return session

        return self._run(sign_in, self._deliver, self._sign_in_failed)

    def _sign_in_failed(self, exc: Exception) -> None:
        self._log_unexpected(exc)
        self.sign_in_failed.emit(messages.sign_in_message_for(exc))

    def _deliver(self, session: Session) -> None:
        if session.must_change_password or session.user is None:
            self.password_change_required.emit()
        else:
            self.signed_in.emit(session.user)

    def change_password(self, current: str, new: str, confirm: str) -> bool:
        problem = _new_password_problem(current, new, confirm)
        if problem:
            self.password_change_failed.emit(problem)
            return False
        return self._run(
            lambda: self._auth.change_password(current, new),
            self._password_changed,
            self._password_change_failed,
        )

    def _password_changed(self, _: None) -> None:
        self.password_changed.emit()
        self._run(self._auth.current_user, self.signed_in.emit, self._load_failed)

    def _password_change_failed(self, exc: Exception) -> None:
        self._log_unexpected(exc)
        if isinstance(exc, SessionExpiredError):
            self.needs_sign_in.emit(messages.message_for(exc))
        else:
            self.password_change_failed.emit(messages.message_for(exc))

    def sign_out(self, remove_files: bool) -> bool:
        """End the session (FR-AUTH-04); with `remove_files`, the sync folder goes to the
        Recycle Bin, never deleted outright."""

        def sign_out() -> str:
            notes = _sign_out_notes(self._auth.sign_out())
            root = self._config.config.sync_root
            if remove_files and root is not None:
                notes.extend(self._recycle(root))
            return " ".join(notes)

        return self._run(sign_out, self.signed_out.emit, self._sign_out_failed)

    def _recycle(self, root: Path) -> list[str]:
        if not root.exists():
            self._config.update(sync_root=None)
            return []
        problem = sync_folder.unsafe_to_remove(root, self._app_data)
        if problem:  # never recycle a folder DropShare doesn't own, whatever the settings say
            logger.error("Refusing to move %s to the Recycle Bin: %s", root, problem)
            return [f"Your files are still in {root}: {problem}"]
        try:
            self._recycle_bin.move_to_recycle_bin(root)
        except OSError:
            logger.exception("Couldn't move the sync folder to the Recycle Bin")
            return [f"Your files are still in {root}: they couldn't be moved to the Recycle Bin."]
        self._config.update(sync_root=None)
        return []

    def _sign_out_failed(self, exc: Exception) -> None:
        logger.error("Sign-out failed", exc_info=exc)
        self.signed_out.emit(messages.message_for(exc))

    def _run[T](
        self,
        fn: Callable[[], T],
        on_success: Callable[[T], None],
        on_failure: Callable[[Exception], None],
    ) -> bool:
        if self._busy:
            logger.info("Ignored a session request: another one is still running")
            return False
        self._set_busy(True)

        def succeeded(result: T) -> None:
            self._set_busy(False)
            on_success(result)

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            on_failure(exc)

        self._worker.submit(fn, succeeded, failed)
        return True

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)

    @staticmethod
    def _log_unexpected(exc: Exception) -> None:
        expected = (
            ApiError,
            InvalidInputError,
            OfflineError,
            SessionExpiredError,
            UpgradeRequiredError,
        )
        if not isinstance(exc, expected):
            logger.error("Unexpected failure: %s", type(exc).__name__)


def _sign_out_notes(result: SignOut) -> list[str]:
    notes = []
    if not result.server_confirmed:
        notes.append(
            "You're signed out on this computer. The server couldn't be told, so the "
            "session ends there when it expires."
        )
    if not result.forgotten_locally:
        notes.append(
            "DropShare couldn't remove your saved sign-in from the Windows credential store. "
            'Remove the "StateLife DropShare" entry in Credential Manager.'
        )
    return notes


def _sign_in_problem(login: str, password: str) -> str | None:
    """Checked before sending, so nothing invalid ever reaches a model (and a log)."""
    if not login or not password:
        return "Enter your username or email and your password."
    if len(login) > EMAIL_MAX_LENGTH or len(password) > PASSWORD_MAX_LENGTH:
        return messages.INVALID_CREDENTIALS
    return None


def _new_password_problem(current: str, new: str, confirm: str) -> str | None:
    """NFR-SEC-04 length rules, checked before sending (the server checks again)."""
    if not current:
        return "Enter your current password."
    if len(current) > PASSWORD_MAX_LENGTH:
        return "Your current password is incorrect."
    if len(new) < PASSWORD_MIN_LENGTH:
        return f"Use at least {PASSWORD_MIN_LENGTH} characters for the new password."
    if len(new) > PASSWORD_MAX_LENGTH:
        return f"Use at most {PASSWORD_MAX_LENGTH} characters for the new password."
    if new != confirm:
        return "The new passwords don't match."
    return None
