import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from dropshare_client.app import configure_logging


class _Paths:
    def __init__(self, app_data: Path | None) -> None:
        self._app_data = app_data

    def app_data_dir(self) -> Path:
        if self._app_data is None:
            raise RuntimeError("LOCALAPPDATA is not set")
        return self._app_data

    def default_sync_root(self) -> Path:
        return Path("unused")


@pytest.fixture(autouse=True)
def _restore_root_handlers() -> Iterator[None]:
    root = logging.getLogger()
    saved = root.handlers[:]
    yield
    for handler in root.handlers:
        if handler not in saved:
            handler.close()
    root.handlers[:] = saved


def _new_handlers(before: list[logging.Handler]) -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h not in before]


def test_logs_rotate_in_app_data_folder(tmp_path: Path) -> None:
    before = logging.getLogger().handlers[:]

    configure_logging(_Paths(tmp_path))

    (handler,) = _new_handlers(before)
    assert isinstance(handler, RotatingFileHandler)
    assert Path(handler.baseFilename) == tmp_path / "logs" / "dropshare.log"
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5


def test_falls_back_to_stderr_when_app_data_is_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    before = logging.getLogger().handlers[:]

    configure_logging(_Paths(None))

    (handler,) = _new_handlers(before)
    assert type(handler) is logging.StreamHandler
    assert "Couldn't open the log file" in caplog.text


def test_falls_back_when_log_folder_cannot_be_created(tmp_path: Path) -> None:
    blocker = tmp_path / "file-not-folder"
    blocker.write_text("x")
    before = logging.getLogger().handlers[:]

    configure_logging(_Paths(blocker))

    (handler,) = _new_handlers(before)
    assert type(handler) is logging.StreamHandler
