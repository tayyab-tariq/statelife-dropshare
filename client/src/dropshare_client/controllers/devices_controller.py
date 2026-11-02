"""The account's signed-in devices in Settings (FR-AUTH-07): listing them and signing one out.
Revoking this computer isn't offered here: Sign out does that and asks about the files."""

import logging

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.devices import Devices
from dropshare_shared.schemas.devices import DeviceOut

logger = logging.getLogger(__name__)

# Re-exported so ui/ needn't import the shared contract (architecture §1).
__all__ = ["DeviceOut", "DevicesController"]


class DevicesController(QObject):
    loading = Signal()
    loaded = Signal()  # read devices()
    failed = Signal(str)  # nothing to show: the view shows the error state
    busy_changed = Signal(bool)
    revoked = Signal(str)  # a short confirmation
    action_failed = Signal(str)

    def __init__(self, devices: Devices, worker: Worker | None = None) -> None:
        super().__init__()
        self._service = devices
        self._worker = worker or Worker()
        self._devices: list[DeviceOut] = []
        self._loaded_once = False
        self._busy = False
        self._generation = 0  # changes at sign-out: late answers are dropped

    @property
    def busy(self) -> bool:
        return self._busy

    def devices(self) -> list[DeviceOut]:
        return list(self._devices)

    def clear(self) -> None:
        self._generation += 1
        self._devices = []
        self._loaded_once = False
        self._set_busy(False)
        self.loaded.emit()

    def refresh(self) -> None:
        generation = self._generation
        if not self._loaded_once:
            self.loading.emit()
        self._worker.submit(
            self._service.list,
            lambda devices: self._loaded(generation, devices),
            lambda exc: self._load_failed(generation, exc),
        )

    def revoke(self, device: DeviceOut) -> None:
        if self._busy or device.is_current:
            return
        generation = self._generation
        self._set_busy(True)
        self._worker.submit(
            lambda: self._service.revoke(device.id),
            lambda _: self._revoked(generation, device),
            lambda exc: self._revoke_failed(generation, exc),
        )

    def _loaded(self, generation: int, devices: list[DeviceOut]) -> None:
        if generation == self._generation:
            self._devices = devices
            self._loaded_once = True
            self.loaded.emit()

    def _load_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        logger.info("Couldn't list devices: %s", type(exc).__name__)
        if self._loaded_once:
            self.action_failed.emit(messages.message_for(exc))
        else:
            self.failed.emit(messages.message_for(exc))

    def _revoked(self, generation: int, device: DeviceOut) -> None:
        if generation != self._generation:
            return
        self._set_busy(False)
        self.revoked.emit(messages.device_signed_out(device.name))
        self.refresh()

    def _revoke_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        self._set_busy(False)
        logger.info("Couldn't sign a device out: %s", type(exc).__name__)
        self.action_failed.emit(messages.message_for(exc))

    def _set_busy(self, busy: bool) -> None:
        if busy != self._busy:
            self._busy = busy
            self.busy_changed.emit(busy)
