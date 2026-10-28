"""Application wiring: arguments, logging, single instance, services, controllers, windows."""

import argparse
import logging
import re
import sys
import threading
import uuid
from collections.abc import Sequence
from importlib.metadata import version
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from dropshare_client import platform
from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.controllers.transfers_controller import TransfersController
from dropshare_client.platform.base import Paths
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.auth import AuthService, this_device
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.files import RemoteFiles
from dropshare_client.services.tokens import TokenStore
from dropshare_client.single_instance import SingleInstance, instance_name
from dropshare_client.sync import binding
from dropshare_client.sync.engine import SyncEngine, SyncListener
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.theme import Theme

logger = logging.getLogger(__name__)

_LOG_FILE_BYTES = 10 * 1024 * 1024
_LOG_FILE_COUNT = 5
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_QUIT_WAIT_MS = 15_000
_PROFILE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
# Bearer values, JWTs and JSON token fields: never written to a log (architecture.md §5).
_SECRETS = re.compile(
    r"(Bearer\s+)\S+"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]+"
    r"|(\"(?:access_token|refresh_token|password|current_password|new_password)\"\s*:\s*\")[^\"]*",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    return _SECRETS.sub(lambda m: (m.group(1) or m.group(2) or "") + "[redacted]", text)


class RedactingFormatter(logging.Formatter):
    """Redacts the whole formatted record, tracebacks included: an exception's text can
    carry a secret even when the log message doesn't."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(paths: Paths) -> None:
    """Log to a rotating file in the app data folder (FR-CLIENT-06), or stderr if that fails."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Their INFO lines include full request URLs; our API client logs what's needed.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
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
        handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
        root.addHandler(handler)
        logger.warning("Couldn't open the log file; logging to stderr instead", exc_info=True)
        return
    handler.setFormatter(RedactingFormatter(_LOG_FORMAT))
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


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dropshare")
    parser.add_argument(
        "--background", action="store_true", help="Start in the tray (used at Windows sign-in)"
    )
    parser.add_argument(
        "--profile",
        help="Development only: run a separate client with its own data and sign-in (OQ-13)",
    )
    args = parser.parse_args(argv)
    if args.profile is not None:
        if getattr(sys, "frozen", False):
            parser.error("--profile is only available when running from source")
        if not _PROFILE.fullmatch(args.profile):
            parser.error("--profile must be 1-32 letters, digits, '-' or '_'")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    # Hooks first, so even a failure while setting up logging is recorded somewhere.
    sys.excepthook = _log_unhandled
    threading.excepthook = _log_unhandled_in_thread
    args = parse_args(sys.argv[1:] if argv is None else argv)
    system = platform.current(args.profile)
    configure_logging(system.paths)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("StateLife DropShare")
    app.setOrganizationName("StateLife")
    app.setQuitOnLastWindowClosed(False)  # closing the window hides it to the tray
    Theme(app)  # before any window, so every widget is polished with it

    app_data = system.paths.app_data_dir()
    instance = SingleInstance(
        instance_name(args.profile),
        app_data / "instance.lock",
        before_waking=platform.allow_foreground_handover,
    )
    if not instance.claim():
        logger.info("Already running; asked the running instance to show its window")
        return 0

    client_version = version("dropshare-client")
    config = ConfigStore(app_data / "config.json")
    tokens = TokenStore(system.credentials)
    api = ApiClient(
        config.config.server_url,
        tokens,
        client_version=client_version,
        user_agent=f"DropShare/{client_version} ({sys.platform})",
    )
    session = SessionController(
        AuthService(api, tokens, this_device()), config, system.recycle_bin, app_data
    )
    folders = SyncFolderController(
        system.volumes, config, system.paths.default_sync_root(), app_data
    )
    files = RemoteFiles(api)
    index_path = app_data / "index.db"

    def make_engine(
        root: Path, user_id: uuid.UUID, name: str, server_url: str, listener: SyncListener
    ) -> SyncEngine:
        index = binding.open_index(index_path, root, server_url, user_id, system.file_attributes)
        return SyncEngine(
            root,
            index,
            files,
            system.recycle_bin,
            system.file_identity,
            system.file_attributes,
            listener,
            display_name=name,
            max_path=platform.max_path_length(),
        )

    sync = SyncController(config, make_engine, lambda: binding.forget(index_path))
    shell = AppShell(
        session,
        folders,
        sync,
        TransfersController(sync),
        background=args.background,
        tray_available=QSystemTrayIcon.isSystemTrayAvailable(),
    )
    instance.activated.connect(shell.open_app)
    shell.start()

    logger.info(
        "Client %s started%s", client_version, f" (profile {args.profile})" if args.profile else ""
    )
    try:
        return app.exec()
    finally:
        sync.shutdown()  # at a safe point: the journal and index are consistent
        # Let running requests finish before their HTTP client goes away (architecture §2).
        if not QThreadPool.globalInstance().waitForDone(_QUIT_WAIT_MS):
            logger.warning("Quit while work was still running")
        api.close()
        instance.release()
        logger.info("Client stopped")
