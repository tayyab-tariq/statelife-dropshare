"""Application wiring: arguments, logging, single instance, services, controllers, windows."""

import argparse
import contextlib
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
from dropshare_client.build_info import BUILD
from dropshare_client.controllers.admin_controller import AdminController
from dropshare_client.controllers.browser_controller import BrowserController
from dropshare_client.controllers.devices_controller import DevicesController
from dropshare_client.controllers.diagnostics_controller import DiagnosticsController
from dropshare_client.controllers.file_actions_controller import FileActionsController
from dropshare_client.controllers.folder_move_controller import FolderMoveController
from dropshare_client.controllers.notification_controller import NotificationController
from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.settings_controller import SettingsController
from dropshare_client.controllers.share_controller import ShareController
from dropshare_client.controllers.shared_controller import SharedController
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.controllers.sync_log_controller import ActivityController, CantSyncController
from dropshare_client.controllers.transfers_controller import TransfersController
from dropshare_client.controllers.trash_controller import TrashController
from dropshare_client.controllers.update_controller import UpdateController
from dropshare_client.controllers.upload_controller import UploadController
from dropshare_client.platform.base import Paths
from dropshare_client.services.admin import AdminUsers
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.app_data import remove_app_data
from dropshare_client.services.auth import AuthService, this_device
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.devices import Devices
from dropshare_client.services.diagnostics import LOG_NAME, RedactingFormatter
from dropshare_client.services.files import RemoteFiles
from dropshare_client.services.importer import Importer
from dropshare_client.services.local_files import LocalFiles
from dropshare_client.services.sharing import Sharing
from dropshare_client.services.tokens import TokenStore
from dropshare_client.services.trash import Trash
from dropshare_client.services.updater import (
    UpdateDeferredError,
    Updater,
    UpdateVerificationError,
)
from dropshare_client.single_instance import SingleInstance, instance_name
from dropshare_client.sync import binding
from dropshare_client.sync.engine import SyncEngine, SyncListener
from dropshare_client.sync.folder_move import FolderMover
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.installing_notice import InstallingNotice
from dropshare_client.ui.theme import Theme

logger = logging.getLogger(__name__)

_LOG_FILE_BYTES = 10 * 1024 * 1024
_LOG_FILE_COUNT = 5
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_QUIT_WAIT_MS = 15_000
_PROFILE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


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
            log_dir / LOG_NAME,
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


def _install_pending(updater: Updater) -> bool:
    """ "Update on next restart" (FR-UPD-03): installed before the main window shows or syncing
    starts, so the check runs on this thread (no event loop yet) with a small window saying
    what's happening. One attempt per remembered update, so an installer that keeps failing
    can't stop DropShare starting; one that couldn't be fully checked (offline) is kept for the
    next start. Whatever goes wrong, DropShare then starts as usual."""
    try:
        update = updater.pending() if updater.can_install else None
        if update is None:
            return False
        updater.forget()
    except OSError:
        logger.warning("Couldn't read the update remembered for this start", exc_info=True)
        return False
    notice = InstallingNotice()
    try:
        updater.verify(update)
    except UpdateDeferredError:
        logger.warning("The update remembered for this start can't be checked now; kept")
        with contextlib.suppress(OSError):
            updater.remember(update)
        return False
    except (UpdateVerificationError, OSError):
        logger.warning("The update remembered for this start didn't verify", exc_info=True)
        return False
    finally:
        notice.close()
    return _launch_installer(update.installer)


def _launch_installer(installer: Path) -> bool:
    try:
        platform.launch_installer(installer)
    except OSError:
        logger.warning("Couldn't start the update installer", exc_info=True)
        return False
    logger.info("Started the update installer %s", installer.name)
    return True


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="dropshare")
    parser.add_argument(
        "--background", action="store_true", help="Start in the tray (used at Windows sign-in)"
    )
    parser.add_argument(
        "link",
        nargs="?",
        help="A statelife-dropshare:// link to open (passed by Windows when one is clicked)",
    )
    # Run by the uninstaller when the user chooses to remove local data (OQ-49).
    parser.add_argument("--remove-app-data", action="store_true", help=argparse.SUPPRESS)
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
    if args.remove_app_data:  # before logging opens a file there
        remove_app_data(system.paths.app_data_dir(), system.credentials)
        return 0
    configure_logging(system.paths)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("StateLife DropShare")
    app.setOrganizationName("StateLife")
    app.setQuitOnLastWindowClosed(False)  # closing the window hides it to the tray
    Theme(app)  # before any window, so every widget is polished with it

    # An update installing now replaces this app and starts it again when done (FR-UPD-03).
    platform.wait_for_setup()
    app_data = system.paths.app_data_dir()
    instance = SingleInstance(
        instance_name(args.profile),
        app_data / "instance.lock",
        before_waking=platform.allow_foreground_handover,
    )
    if not instance.claim(args.link):
        logger.info("Already running; asked the running instance to show its window")
        return 0
    try:
        platform.mark_running()
    except OSError:
        logger.warning("Couldn't mark DropShare as running for Setup", exc_info=True)

    client_version = version("dropshare-client")
    config = ConfigStore(app_data / "config.json")
    tokens = TokenStore(system.credentials)
    api = ApiClient(
        config.config.server_url,
        tokens,
        client_version=client_version,
        user_agent=f"DropShare/{client_version} ({sys.platform})",
    )
    updater = Updater(
        api, client_version, app_data / "updates", system.signatures, BUILD.update_signer()
    )
    if _install_pending(updater):
        api.close()
        instance.release()
        return 0
    auth = AuthService(api, tokens, this_device())
    session = SessionController(auth, config, system.recycle_bin, app_data)
    folders = SyncFolderController(
        system.volumes, config, system.paths.default_sync_root(), app_data
    )
    files = RemoteFiles(api)
    sharing = Sharing(api, files)
    # Signed out: the next user never resumes this one's uploads (FR-XFER-01).
    session.signed_out.connect(lambda _: files.forget_uploads())
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
            max_transfers=config.config.max_transfers,
        )

    def switch_folder(root: Path) -> None:
        binding.rebind(index_path, root)
        config.update(sync_root=root)

    mover = FolderMover(
        app_data / "folder-move.json",
        system.recycle_bin,
        switch_folder,
        hide=system.file_attributes.hide,
    )
    sync = SyncController(config, make_engine, lambda: binding.forget(index_path))
    transfers = TransfersController(sync)
    trash = TrashController(Trash(api))

    def local_files(root: Path) -> LocalFiles:
        return LocalFiles(root, system.recycle_bin, platform.reveal)

    updates = UpdateController(updater)
    shell = AppShell(
        session,
        folders,
        sync,
        transfers,
        BrowserController(files, index_path, transfers),
        FileActionsController(
            config,
            local_files,
            sync,
            index_path,
        ),
        UploadController(
            config,
            lambda root: Importer(root, system.recycle_bin, files.banned_extensions),
            sync,
            auth.current_user,
        ),
        share=ShareController(sharing),
        shared=SharedController(sharing),
        trash=trash,
        notifications=NotificationController(config, auth.current_user),
        activity=ActivityController(config, sync, index_path, local_files),
        cant_sync=CantSyncController(config, sync, index_path, local_files),
        settings=SettingsController(config, system.autostart, sync, client_version),
        devices=DevicesController(Devices(api)),
        folder_move=FolderMoveController(mover, sync, config, system.volumes, app_data),
        diagnostics=DiagnosticsController(config, app_data / "logs", client_version),
        updates=updates,
        admin=AdminController(AdminUsers(api)),
        background=args.background,
        tray_available=QSystemTrayIcon.isSystemTrayAvailable(),
    )
    instance.activated.connect(shell.open_app)
    instance.link_received.connect(shell.open_link)
    shell.start()
    if args.link:
        shell.open_link(args.link)

    logger.info(
        "Client %s started%s", client_version, f" (profile {args.profile})" if args.profile else ""
    )
    try:
        return app.exec()
    finally:
        sync.shutdown()  # at a safe point: the journal and index are consistent
        trash.cancel()  # a long restore or delete stops after the item in progress
        updates.shutdown()  # a download stops; it resumes next time
        # Let running requests finish before their HTTP client goes away (architecture §2).
        if not QThreadPool.globalInstance().waitForDone(_QUIT_WAIT_MS):
            logger.warning("Quit while work was still running")
        api.close()
        instance.release()
        if updates.installer_to_run is not None:  # "Restart and update", once syncing stopped
            _launch_installer(updates.installer_to_run)
        logger.info("Client stopped")
