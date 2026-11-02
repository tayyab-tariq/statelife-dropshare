"""Which notifications are shown, and when (FR-CLIENT-02, ui.md §7).

Each type can be turned off in Settings. A burst of one type becomes a single notification, and
a type is shown at most once every few seconds. Shows nothing itself: the shell puts `notify`
in the tray (or the window's status line when there is no tray).
"""

import logging
import time
from collections.abc import Callable
from enum import StrEnum

from PySide6.QtCore import QObject, QTimer, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.config import ConfigStore, NotificationSettings
from dropshare_client.store.records import Activity, Problem
from dropshare_shared.schemas.users import UserOut

logger = logging.getLogger(__name__)

GATHER_MS = 3_000  # what arrives within this becomes one notification
MIN_GAP_MS = 10_000  # at most one notification of a type this often
QUOTA_CHECK_SECONDS = 60.0  # at most one quota check this often while uploading
_NEARLY_FULL_PERCENT = 90


class Notice(StrEnum):
    """Each notification type that can be turned off (FR-CLIENT-02); each value is the name
    of its switch in NotificationSettings."""

    NEW_SHARE = "new_share"
    CONFLICTS = "conflicts"
    CANT_SYNC = "cant_sync"
    QUOTA = "quota"
    UPDATE_AVAILABLE = "update_available"


def _enabled(settings: NotificationSettings, notice: Notice) -> bool:
    return bool(getattr(settings, notice.value))


def _quota_level(user: UserOut) -> int:
    """100 when full, 90 when at least 90% full, else 0."""
    if user.quota_bytes <= 0:
        return 0
    if user.used_bytes >= user.quota_bytes:
        return 100
    return _NEARLY_FULL_PERCENT if user.used_bytes * 100 >= user.quota_bytes * 90 else 0


class _Gatherer(QObject):
    """One notification type: collects messages, then shows one notification for them."""

    def __init__(
        self, compose: Callable[[list[str]], tuple[str, str]], gather_ms: int, gap_ms: int
    ) -> None:
        super().__init__()
        self._compose = compose
        self._gather_ms = gather_ms
        self._gap = gap_ms / 1000
        self._pending: list[str] = []
        self._last_shown: float | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show)
        self.show: Callable[[str, str], None] = lambda title, message: None

    def add(self, item: str) -> None:
        self._pending.append(item)
        if self._timer.isActive():
            return
        wait = self._gather_ms / 1000
        if self._last_shown is not None:
            wait = max(wait, self._last_shown + self._gap - time.monotonic())
        self._timer.start(round(wait * 1000))

    def clear(self) -> None:
        self._timer.stop()
        self._pending = []

    def _show(self) -> None:
        gathered, self._pending = self._pending, []
        if gathered:
            self._last_shown = time.monotonic()
            self.show(*self._compose(gathered))


def _conflicts(items: list[str]) -> tuple[str, str]:
    text = items[0] if len(items) == 1 else messages.conflicts_message(len(items))
    return messages.NOTICE_CONFLICTS, text


class NotificationController(QObject):
    notify = Signal(str, str)  # title, message

    def __init__(
        self,
        config: ConfigStore,
        current_user: Callable[[], UserOut],
        worker: Worker | None = None,
        *,
        gather_ms: int = GATHER_MS,
        gap_ms: int = MIN_GAP_MS,
        quota_check_seconds: float = QUOTA_CHECK_SECONDS,
    ) -> None:
        super().__init__()
        self._config = config
        self._current_user = current_user
        self._worker = worker or Worker()
        self._quota_check = quota_check_seconds
        self._gatherers = {
            Notice.NEW_SHARE: _Gatherer(
                lambda names: (messages.NOTICE_NEW_SHARE, messages.new_shares(names)),
                gather_ms,
                gap_ms,
            ),
            Notice.CONFLICTS: _Gatherer(_conflicts, gather_ms, gap_ms),
            Notice.CANT_SYNC: _Gatherer(
                lambda paths: (messages.NOTICE_CANT_SYNC, messages.cant_sync_notice(paths)),
                gather_ms,
                gap_ms,
            ),
            Notice.QUOTA: _Gatherer(lambda _: self._quota_notice(), 0, gap_ms),
            Notice.UPDATE_AVAILABLE: _Gatherer(
                lambda versions: (messages.NOTICE_UPDATE, messages.update_notice(versions[-1])),
                0,
                gap_ms,
            ),
        }
        for gatherer in self._gatherers.values():
            gatherer.setParent(self)
            gatherer.show = self.notify.emit
        self._known_problems: set[str] | None = None  # None: the next report is the baseline
        self._quota_level = 0
        self._usage: UserOut | None = None  # as last read
        self._quota_checked_at: float | None = None
        self._checking_quota = False

    def reset(self) -> None:
        """Signed out: forget everything about the account."""
        for gatherer in self._gatherers.values():
            gatherer.clear()
        self._known_problems = None
        self._quota_level = 0
        self._usage = None
        self._quota_checked_at = None

    # --- inputs --------------------------------------------------------------------------

    def new_shares(self, names: list[str]) -> None:
        for name in names:
            self._add(Notice.NEW_SHARE, name)

    def conflict(self, message: str) -> None:
        self._add(Notice.CONFLICTS, message)

    def problems_changed(self, problems: list[Problem]) -> None:
        """Notifies about items newly unable to sync. The first list after signing in or
        starting is what earlier runs left: listed in Can't sync, not notified again."""
        keys = {problem.key for problem in problems}
        known, self._known_problems = self._known_problems, keys
        if known is None:
            return
        for problem in problems:
            if problem.key not in known:
                self._add(Notice.CANT_SYNC, problem.path)

    def update_ready(self, version: str) -> None:
        """An update was downloaded and verified (FR-CLIENT-02, FR-UPD-03)."""
        self._add(Notice.UPDATE_AVAILABLE, version)

    def activity_added(self, item: Activity) -> None:
        if item.kind == "uploaded":
            self.check_quota()

    def account_loaded(self, user: UserOut) -> None:
        """Signed in, or the account was read again: notify on crossing 90% or 100%. Each
        level notifies once per run, and again only after use drops below 90%."""
        self._usage = user
        level = _quota_level(user)
        if level > self._quota_level:
            self._add(Notice.QUOTA, str(level))
        if level == 0 or level > self._quota_level:
            self._quota_level = level

    def _quota_notice(self) -> tuple[str, str]:
        user = self._usage
        if user is None:
            raise RuntimeError("no usage read yet")
        title = (
            messages.NOTICE_QUOTA_FULL
            if user.used_bytes >= user.quota_bytes
            else messages.NOTICE_QUOTA_NEARLY_FULL
        )
        return title, messages.quota_notice(user.used_bytes, user.quota_bytes)

    def check_quota(self) -> None:
        """Read the account's usage again, at most once every QUOTA_CHECK_SECONDS."""
        now = time.monotonic()
        if self._checking_quota or (
            self._quota_checked_at is not None and now - self._quota_checked_at < self._quota_check
        ):
            return
        self._checking_quota = True
        self._quota_checked_at = now
        self._worker.submit(self._current_user, self._quota_read, self._quota_failed)

    def _quota_read(self, user: UserOut) -> None:
        self._checking_quota = False
        if self._quota_checked_at is not None:  # not signed out meanwhile
            self.account_loaded(user)

    def _quota_failed(self, exc: Exception) -> None:
        self._checking_quota = False
        # Offline or signed out: the next upload asks again; the engine reports the rest.
        logger.info("Couldn't read the storage used: %s", type(exc).__name__)

    def _add(self, notice: Notice, item: str) -> None:
        if _enabled(self._config.config.notifications, notice):
            self._gatherers[notice].add(item)
