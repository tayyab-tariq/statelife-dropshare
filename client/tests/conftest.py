import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from client_fakes import Client, FakeServer, FakeVolumes, MemoryCredentials
from PySide6.QtWidgets import QAbstractButton, QLabel, QWidget

from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.platform.macos.recycle_bin import MacTrash
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.auth import AuthService, Device
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.tokens import TokenStore

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
            client_version="0.1.0",
            user_agent="DropShare/0.1.0 (test)",
            transport=httpx.MockTransport(server.handle),
        )
        made.append(api)
        auth = AuthService(api, tokens, Device(name="Test PC", os="Windows 11"))
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
            trash=trash,
            app_data=tmp_path / "app-data",
        )

    yield make
    for api in made:
        api.close()
