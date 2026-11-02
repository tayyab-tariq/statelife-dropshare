"""Export diagnostics from Settings (FR-CLIENT-06): written on the pool."""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal, qVersion

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services import diagnostics
from dropshare_client.services.config import ConfigStore

logger = logging.getLogger(__name__)


class DiagnosticsController(QObject):
    exported = Signal(str)  # where, for the user
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self, config: ConfigStore, log_dir: Path, version: str, worker: Worker | None = None
    ) -> None:
        super().__init__()
        self._config = config
        self._log_dir = log_dir
        self._version = version
        self._worker = worker or Worker()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    @staticmethod
    def default_name() -> str:
        return diagnostics.default_name()

    def export(self, destination: str) -> None:
        """One at a time: a second export to the same file would race the first."""
        if self._busy:
            return
        self._set_busy(True)
        config, log_dir, version = self._config.config, self._log_dir, self._version
        self._worker.submit(
            lambda: diagnostics.export(Path(destination), log_dir, config, version, qVersion()),
            self._exported,
            self._failed,
        )

    def _exported(self, path: Path) -> None:
        self._set_busy(False)
        self.exported.emit(messages.diagnostics_saved(str(path)))

    def _failed(self, exc: Exception) -> None:
        self._set_busy(False)
        logger.warning("Couldn't export diagnostics", exc_info=exc)
        self.failed.emit(messages.DIAGNOSTICS_FAILED)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)
