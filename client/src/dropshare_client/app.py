"""Application wiring: logging, Qt application, main window."""

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from types import TracebackType

from PySide6.QtWidgets import QApplication

from dropshare_client import platform
from dropshare_client.platform.base import Paths
from dropshare_client.ui.main_window import MainWindow

logger = logging.getLogger(__name__)

_LOG_FILE_BYTES = 10 * 1024 * 1024
_LOG_FILE_COUNT = 5
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(paths: Paths) -> None:
    """Log to a rotating file in the app data folder (FR-CLIENT-06), or stderr if that fails."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    try:
        log_dir = paths.app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            log_dir / "dropshare.log",
            maxBytes=_LOG_FILE_BYTES,
            backupCount=_LOG_FILE_COUNT,
            encoding="utf-8",
        )
    except (OSError, RuntimeError):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        root.addHandler(handler)
        logger.warning("Couldn't open the log file; logging to stderr instead", exc_info=True)
        return
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)


def _log_unhandled(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    logger.critical("Unhandled exception", exc_info=(exc_type, exc, tb))


def _log_unhandled_in_thread(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None:
        logger.critical(
            "Unhandled exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )


def main() -> int:
    # Hooks first, so even a failure while setting up logging is recorded somewhere.
    sys.excepthook = _log_unhandled
    threading.excepthook = _log_unhandled_in_thread
    configure_logging(platform.paths())

    app = QApplication(sys.argv)
    app.setApplicationName("StateLife DropShare")
    app.setOrganizationName("StateLife")
    window = MainWindow()
    window.show()
    logger.info("Client started")
    exit_code = app.exec()
    logger.info("Client stopped")
    return exit_code
