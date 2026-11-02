"""Settings (FR-CLIENT-04): which notifications show, starting at Windows sign-in, and how many
files move at once (OQ-39). Saved in the settings file; the registry is written on the pool."""

import logging

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.notification_controller import Notice
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.worker import Worker
from dropshare_client.platform.base import Autostart
from dropshare_client.services.config import (
    MAX_TRANSFERS_LIMIT,
    ConfigStore,
    NotificationSettings,
)

logger = logging.getLogger(__name__)

# Re-exported so ui/ needn't import services or the notification controller.
__all__ = ["MAX_TRANSFERS_LIMIT", "Notice", "SettingsController"]


class SettingsController(QObject):
    changed = Signal()  # read the properties again
    failed = Signal(str)

    def __init__(
        self,
        config: ConfigStore,
        autostart: Autostart,
        sync: SyncController,
        version: str,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._autostart = autostart
        self._sync = sync
        self.version = version
        self._worker = worker or Worker()

    @property
    def server_url(self) -> str:
        return self._config.config.server_url

    @property
    def notifications(self) -> NotificationSettings:
        return self._config.config.notifications

    @property
    def max_transfers(self) -> int:
        return self._config.config.max_transfers

    @property
    def autostart_available(self) -> bool:
        return self._autostart.available()

    @property
    def autostart(self) -> bool:
        """What the user chose; on (the default) until they decide (FR-CLIENT-04)."""
        return self._config.config.autostart is not False

    def set_notification(self, kind: Notice, on: bool) -> None:
        updated = self.notifications.model_copy(update={kind.value: on})
        self._save(notifications=updated)

    def set_max_transfers(self, count: int) -> None:
        if self._save(max_transfers=count):
            self._sync.set_max_transfers(self.max_transfers)  # as clamped by the settings

    def set_autostart(self, on: bool) -> None:
        if self._save(autostart=on):
            self._register(on)

    def apply_autostart(self) -> None:
        """After signing in: register the choice (on for a first sign-in), and register again
        if the app moved since (a reinstall elsewhere)."""
        if self._autostart.available():
            if self._config.config.autostart is None:
                self._save(autostart=True)
            self._register(self.autostart)

    def _register(self, on: bool) -> None:
        def write() -> None:
            if self._autostart.is_enabled() != on:
                self._autostart.set_enabled(on)

        self._worker.submit(write, lambda _: None, self._register_failed)

    def _register_failed(self, exc: Exception) -> None:
        logger.warning("Couldn't change starting at sign-in: %s", type(exc).__name__)
        self.failed.emit(messages.AUTOSTART_FAILED)

    def _save(self, **changes: object) -> bool:
        try:
            self._config.update(**changes)
        except OSError:
            logger.warning("Couldn't save the settings", exc_info=True)
            self.failed.emit(messages.SETTINGS_NOT_SAVED)
            self.changed.emit()  # the controls go back to what is saved
            return False
        self.changed.emit()
        return True
