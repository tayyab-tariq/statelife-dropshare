import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from client_fakes import Client, FakeEngines, FakeServer, FakeVolumes, MemoryCredentials
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QAbstractButton, QLabel, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client.controllers.browser_controller import BrowserController
from dropshare_client.controllers.file_actions_controller import FileActionsController
from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.share_controller import ShareController
from dropshare_client.controllers.shared_controller import SharedController
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.controllers.transfers_controller import TransfersController
from dropshare_client.controllers.upload_controller import UploadController
from dropshare_client.platform.macos.recycle_bin import MacTrash
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.auth import AuthService, Device
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.files import RemoteFiles
from dropshare_client.services.importer import Importer
from dropshare_client.services.local_files import LocalFiles
from dropshare_client.services.sharing import Sharing
from dropshare_client.services.tokens import TokenStore
from dropshare_client.ui.app_shell import AppShell

# Headless Qt so tests run without a display (CI, SSH sessions). Visual checks on the Windows
# machine run the app itself, not these tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if sys.platform == "win32":
        return
    skip = pytest.mark.skip(reason="needs Windows (run on the Windows test machine)")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip)


def _spoken_name(widget: QWidget) -> str:
    """The name assistive technology announces: explicit accessible name, else visible text."""
    if widget.accessibleName():
        return widget.accessibleName()
    if isinstance(widget, QLabel | QAbstractButton):
        return widget.text().replace("&", "")
    return ""


type FindByName = Callable[[QWidget, type[QWidget], str], QWidget]


@pytest.fixture
def find_by_name() -> FindByName:
    """Find exactly one child widget by the name a screen reader would use (ui.md §8)."""

    def find(root: QWidget, widget_type: type[QWidget], name: str) -> QWidget:
        matches = [w for w in root.findChildren(widget_type) if _spoken_name(w) == name]
        if len(matches) > 1:  # another section's page has one too: the shown one counts
            matches = [w for w in matches if w.isVisibleTo(root)]
        assert len(matches) == 1, f"expected one {widget_type.__name__} named {name!r}"
        return matches[0]

    return find


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def credentials() -> MemoryCredentials:
    return MemoryCredentials()


type MakeClient = Callable[[], Client]


@pytest.fixture
def make_client(
    tmp_path: Path, server: FakeServer, credentials: MemoryCredentials
) -> Iterator[MakeClient]:
    """make_client(): a client sharing the server, credential store and app-data folder with any
    other made here, like a relaunch of the same install."""
    made: list[ApiClient] = []
    trash = tmp_path / "trash"
    trash.mkdir()

    def make() -> Client:
        config = ConfigStore(tmp_path / "app-data" / "config.json")
        tokens = TokenStore(credentials)
        api = ApiClient(
            config.config.server_url,
            tokens,
            client_version="0.2.0",
            user_agent="DropShare/0.2.0 (test)",
            transport=httpx.MockTransport(server.handle),
        )
        made.append(api)
        auth = AuthService(api, tokens, Device(name="Test PC", os="Windows 11"))
        engines = FakeEngines(tmp_path / "app-data" / "index.db")
        sync = SyncController(config, engines, engines.forget)
        transfers = TransfersController(sync)
        revealed: list[Path] = []
        sharing = Sharing(api, RemoteFiles(api))
        return Client(
            server=server,
            credentials=credentials,
            config=config,
            tokens=tokens,
            api=api,
            auth=auth,
            session=SessionController(auth, config, MacTrash(trash), tmp_path / "app-data"),
            folders=SyncFolderController(
                FakeVolumes(tmp_path),
                config,
                tmp_path / "StateLife DropShare",
                tmp_path / "app-data",
            ),
            sync=sync,
            transfers=transfers,
            browser=BrowserController(
                RemoteFiles(api), tmp_path / "app-data" / "index.db", transfers
            ),
            file_actions=FileActionsController(
                config,
                lambda root: LocalFiles(root, MacTrash(trash), revealed.append),
                sync,
                tmp_path / "app-data" / "index.db",
            ),
            revealed=revealed,
            uploads=UploadController(
                config, lambda root: Importer(root, MacTrash(trash)), sync, auth.current_user
            ),
            share=ShareController(sharing),
            shared=SharedController(sharing),
            engines=engines,
            trash=trash,
            app_data=tmp_path / "app-data",
        )

    yield make
    for api in made:
        api.close()


type MakeShell = Callable[..., AppShell]


@pytest.fixture
def make_shell(qtbot: QtBot) -> Iterator[MakeShell]:
    shells: list[AppShell] = []
    clients: list[Client] = []

    def make(client: Client, *, tray_available: bool = True) -> AppShell:
        clients.append(client)
        shell = AppShell(
            client.session,
            client.folders,
            client.sync,
            client.transfers,
            client.browser,
            client.file_actions,
            client.uploads,
            share=client.share,
            shared=client.shared,
            tray_available=tray_available,
        )
        qtbot.addWidget(shell.window)
        qtbot.addWidget(shell.sign_in_dialog)
        shells.append(shell)
        return shell

    yield make
    for shell in shells:
        shell.tray.hide()
    # pytest-qt has deleted the widgets by now: silence what still arrives for them.
    for client in clients:
        for controller in (
            client.sync,
            client.transfers,
            client.browser,
            client.file_actions,
            client.uploads,
            client.session,
            client.folders,
        ):
            controller.blockSignals(True)
        client.sync.shutdown()
    QThreadPool.globalInstance().waitForDone(5000)
    qtbot.wait(10)
