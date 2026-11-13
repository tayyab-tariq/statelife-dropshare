"""User administration (FR-ADMIN-02/06): the user list, creating users, enabling and disabling
them, resetting passwords, and changing roles and quotas. Every request runs on the pool; the
server decides who may do this, so a refusal ends the section (`access_lost`)."""

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.admin import AdminUsers
from dropshare_client.services.errors import ApiError
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.admin import (
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserUpdateIn,
    TemporaryPasswordOut,
)
from dropshare_shared.schemas.users import UserRole, UserStatus

logger = logging.getLogger(__name__)

# Re-exported so ui/ needn't import the shared contract (architecture §1).
__all__ = [
    "AdminController",
    "AdminUserCreateIn",
    "AdminUserOut",
    "TemporaryPasswordOut",
    "UserRole",
    "UserStatus",
]


class AdminController(QObject):
    loading = Signal()  # the first load: nothing to show yet
    loaded = Signal()  # read users()
    failed = Signal(str)  # nothing to show: the view shows the error state
    reload_failed = Signal(str)  # the list is kept; the next reload that works clears it
    busy_changed = Signal(bool)
    succeeded = Signal(str)  # a short confirmation
    action_failed = Signal(str)
    # A new temporary password (TemporaryPasswordOut): shown once, never kept here.
    password_issued = Signal(object)
    access_lost = Signal()  # the server says this account isn't an admin (any more)
    cleared = Signal()  # signed out: forget messages and selection

    def __init__(self, admin: AdminUsers, worker: Worker | None = None) -> None:
        super().__init__()
        self._service = admin
        self._worker = worker or Worker()
        self._users: list[AdminUserOut] = []
        self._loaded_once = False
        self._busy = False
        self._generation = 0  # changes at sign-out: late answers are dropped
        self._reload = 0  # the latest reload: an older one finishing later is dropped

    @property
    def busy(self) -> bool:
        return self._busy

    def users(self) -> list[AdminUserOut]:
        return list(self._users)

    def clear(self) -> None:
        self._generation += 1
        self._users = []
        self._loaded_once = False
        self._set_busy(False)
        self.loaded.emit()
        self.cleared.emit()

    def refresh(self) -> None:
        generation = self._generation
        self._reload += 1
        reload = self._reload
        if not self._loaded_once:
            self.loading.emit()
        self._worker.submit(
            self._service.list,
            lambda users: self._loaded(generation, reload, users),
            lambda exc: self._load_failed(generation, reload, exc),
        )

    def create(self, body: AdminUserCreateIn) -> None:
        self._act(
            lambda: self._service.create(body),
            lambda result: self._password_issued(result, messages.user_created),
        )

    def reset_password(self, user: AdminUserOut) -> None:
        self._act(
            lambda: self._service.reset_password(user.id),
            lambda result: self._password_issued(result, messages.password_reset_done),
        )

    def set_status(self, user: AdminUserOut, status: UserStatus) -> None:
        done = messages.user_disabled if status is UserStatus.DISABLED else messages.user_enabled
        self._update(user, AdminUserUpdateIn(status=status), done(user.username))

    def set_role(self, user: AdminUserOut, role: UserRole) -> None:
        text = messages.account_role_changed(user.username, role is UserRole.ADMIN)
        self._update(user, AdminUserUpdateIn(role=role), text)

    def set_quota(self, user: AdminUserOut, quota_bytes: int) -> None:
        text = messages.quota_changed(user.username, quota_bytes)
        self._update(user, AdminUserUpdateIn(quota_bytes=quota_bytes), text)

    # --- results -----------------------------------------------------------------------------

    def _update(self, user: AdminUserOut, body: AdminUserUpdateIn, done: str) -> None:
        self._act(lambda: self._service.update(user.id, body), lambda _: self._done(done))

    def _act[T](self, call: Callable[[], T], on_success: Callable[[T], None]) -> None:
        """One action at a time: the view disables its buttons while one runs."""
        if self._busy:
            self.action_failed.emit(messages.ACTION_ALREADY_RUNNING)
            return
        generation = self._generation
        self._set_busy(True)

        def succeeded(result: T) -> None:
            if generation == self._generation:
                self._set_busy(False)
                on_success(result)
                self.refresh()

        self._worker.submit(call, succeeded, lambda exc: self._action_failed(generation, exc))

    def _password_issued(self, result: TemporaryPasswordOut, done: Callable[[str], str]) -> None:
        self.succeeded.emit(done(result.user.username))
        self.password_issued.emit(result)

    def _done(self, text: str) -> None:
        self.succeeded.emit(text)

    def _loaded(self, generation: int, reload: int, users: list[AdminUserOut]) -> None:
        if generation != self._generation or reload != self._reload:
            return
        self._users = users
        self._loaded_once = True
        self.loaded.emit()

    def _load_failed(self, generation: int, reload: int, exc: Exception) -> None:
        if generation != self._generation or reload != self._reload:
            return
        logger.info("Couldn't list users: %s", type(exc).__name__)
        if self._lost_access(exc):
            return
        if self._loaded_once:
            self.reload_failed.emit(messages.message_for(exc))
        else:
            self.failed.emit(messages.message_for(exc))

    def _action_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        self._set_busy(False)
        logger.info("An administration action failed: %s", type(exc).__name__)
        if not self._lost_access(exc):
            self.action_failed.emit(messages.message_for(exc))

    def _lost_access(self, exc: Exception) -> bool:
        """The server refused: this account isn't an admin (any more). Forget the list."""
        if not (isinstance(exc, ApiError) and exc.code == ErrorCode.FORBIDDEN):
            return False
        self.clear()
        self.access_lost.emit()
        return True

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)
