"""Choosing the sync folder on first run (FR-SYNC-01)."""

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.platform.base import Volumes
from dropshare_client.services import sync_folder
from dropshare_client.services.config import ConfigStore

logger = logging.getLogger(__name__)


class _UnusableFolderError(Exception):
    pass


class SyncFolderController(QObject):
    checked = Signal(str, str)  # path, problem ("" when the folder can be used)
    saved = Signal(str)
    save_failed = Signal(str)

    def __init__(
        self,
        volumes: Volumes,
        config: ConfigStore,
        default_root: Path,
        app_data: Path,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._app_data = app_data
        self._volumes = volumes
        self._config = config
        self._default_root = default_root
        self._worker = worker or Worker()

    @property
    def default_folder(self) -> str:
        return str(self._default_root)

    @property
    def current_folder(self) -> str | None:
        root = self._config.config.sync_root
        return None if root is None else str(root)

    def check(self, path: str) -> None:
        """Validate off the UI thread: looking at a network path can take seconds."""
        self._worker.submit(
            lambda: sync_folder.problem_with(Path(path), self._volumes, self._app_data) or "",
            lambda problem: self.checked.emit(path, problem),
            lambda exc: self.checked.emit(path, messages.message_for(exc)),
        )

    def save(self, path: str) -> None:
        def save() -> str:
            folder = Path(path)
            problem = sync_folder.problem_with(folder, self._volumes, self._app_data)
            if problem:
                raise _UnusableFolderError(problem)
            sync_folder.create(folder)
            self._config.update(sync_root=folder)
            return path

        def failed(exc: Exception) -> None:
            if isinstance(exc, _UnusableFolderError):
                self.save_failed.emit(str(exc))
            else:
                logger.warning("Couldn't create the sync folder %s", path, exc_info=exc)
                self.save_failed.emit(messages.message_for(exc))

        self._worker.submit(save, self.saved.emit, failed)
