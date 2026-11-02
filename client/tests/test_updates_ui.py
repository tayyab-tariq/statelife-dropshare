"""Updates in the app (FR-UPD-03, FR-UPD-04, FR-CLIENT-02, OQ-41): Settings, About, the update
dialog, the notification, and installing now or at the next start."""

import hashlib
import threading
from pathlib import Path

import httpx
import pytest
from client_fakes import USER_PASSWORD, FakeUpdates, MemoryCredentials, release_out
from conftest import FindByName, MakeClient, MakeShell
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit, QPushButton, QWidget
from pytestqt.qtbot import QtBot

from dropshare_client import app
from dropshare_client.controllers import messages
from dropshare_client.controllers.update_controller import UpdateController, UpdateState
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import OfflineError
from dropshare_client.services.tokens import TokenStore
from dropshare_client.services.updater import ReadyUpdate, Updater, UpdateVerificationError
from dropshare_client.ui.app_shell import AppShell
from dropshare_client.ui.update_dialog import NOW, ON_RESTART
from dropshare_shared.signing import (
    ExpectedSigner,
    RevocationUnavailableError,
    SignatureError,
    Signer,
)

VERSION = "0.4.0"


@pytest.fixture
def quits(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    made: list[bool] = []
    monkeypatch.setattr(
        "dropshare_client.ui.app_shell.QApplication.quit", lambda: made.append(True)
    )
    return made


def _started(make_client: MakeClient, make_shell: MakeShell, **updates: object) -> AppShell:
    client = make_client()
    for name, value in updates.items():
        setattr(client.updater, name, value)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    shell = make_shell(client)
    shell.start()
    return shell


def _status(shell: AppShell) -> str:
    label = shell.settings_view.findChild(QLabel, "settingsError")
    assert label is not None
    return label.text()


def _updates_text(find_by_name: FindByName, shell: AppShell, status: str) -> None:
    assert find_by_name(shell.settings_view, QLabel, f"{messages.UPDATES}: {status}")


def _button(find_by_name: FindByName, root: QWidget, name: str) -> QPushButton:
    button = find_by_name(root, QPushButton, name)
    assert isinstance(button, QPushButton)
    return button


def _ready(qtbot: QtBot, shell: AppShell) -> UpdateController:
    updates = shell.settings_view._updates
    qtbot.waitUntil(lambda: updates.state is UpdateState.READY)
    return updates


def test_signing_in_checks_and_says_when_up_to_date(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    shell = _started(make_client, make_shell)
    updates = shell.settings_view._updates

    qtbot.waitUntil(lambda: updates.state is UpdateState.UP_TO_DATE)

    _updates_text(find_by_name, shell, messages.UPDATE_UP_TO_DATE)
    assert _button(find_by_name, shell.settings_view, "Check for updates").isEnabled()
    assert not _button(find_by_name, shell.settings_view, "Install update…").isVisibleTo(
        shell.settings_view
    )


def test_a_newer_release_downloads_in_the_background_then_notifies(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    gate = threading.Event()
    shell = _started(make_client, make_shell, release=release_out(VERSION), gate=gate)
    notes: list[str] = []
    shell._notifications.notify.connect(lambda title, text: notes.append(f"{title}: {text}"))

    # 2 GB of 4 GB: the byte count crosses the GUI thread without wrapping.
    downloading = messages.update_downloading(VERSION, 50)
    qtbot.waitUntil(lambda: shell.settings_view._update_status.text() == downloading)
    checks = _fake(shell.settings_view._updates).checks
    _button(find_by_name, shell.settings_view, "Check for updates").click()  # stays focusable
    assert _fake(shell.settings_view._updates).checks == checks  # but starts nothing new
    gate.set()

    _ready(qtbot, shell)
    _updates_text(find_by_name, shell, messages.update_ready(VERSION))
    assert _button(find_by_name, shell.settings_view, "Install update…").isVisibleTo(
        shell.settings_view
    )
    qtbot.waitUntil(lambda: bool(notes))
    assert notes == [f"{messages.NOTICE_UPDATE}: {messages.update_notice(VERSION)}"]
    assert shell.update_dialog is None  # not refused: the user installs when they choose


def test_restart_and_update_checks_once_more_then_quits_to_install(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    quits: list[bool],
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION, notes="Faster."))
    updates = _ready(qtbot, shell)
    _button(find_by_name, shell.settings_view, "Install update…").click()
    dialog = shell.update_dialog
    assert dialog is not None
    notes = find_by_name(dialog, QPlainTextEdit, "Release notes")
    assert isinstance(notes, QPlainTextEdit) and notes.toPlainText() == "Faster."

    _button(find_by_name, dialog, NOW.replace("&", "")).click()

    qtbot.waitUntil(lambda: bool(quits))
    fake = _fake(updates)
    assert (fake.verified, fake.remembered) == ([VERSION], [VERSION])
    assert updates.installer_to_run is not None
    assert updates.installer_to_run.name == f"DropShare-{VERSION}-setup.exe"


def test_update_on_next_restart_is_remembered_and_shown(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    quits: list[bool],
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    updates = _ready(qtbot, shell)
    _button(find_by_name, shell.settings_view, "Install update…").click()
    assert shell.update_dialog is not None

    _button(find_by_name, shell.update_dialog, ON_RESTART.replace("&", "")).click()

    qtbot.waitUntil(lambda: _fake(updates).remembered == [VERSION])
    _updates_text(find_by_name, shell, messages.update_on_restart(VERSION))
    assert quits == []
    assert updates.installer_to_run is None


def test_not_now_decides_nothing(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    updates = _ready(qtbot, shell)
    _button(find_by_name, shell.settings_view, "Install update…").click()
    assert shell.update_dialog is not None

    QTest.keyClick(shell.update_dialog, Qt.Key.Key_Escape)

    qtbot.waitUntil(lambda: shell.update_dialog is None)
    assert _fake(updates).remembered == []
    assert updates.state is UpdateState.READY


def test_a_refused_version_offers_the_update_at_once(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    _ready(qtbot, shell)
    assert shell.update_dialog is None

    shell._session.upgrade_required.emit(messages.UPGRADE)

    qtbot.waitUntil(lambda: shell.update_dialog is not None)
    assert shell.update_dialog is not None
    assert messages.UPDATE_REQUIRED in shell.update_dialog.accessibleDescription()


def test_a_failed_check_says_so_and_check_for_updates_tries_again(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    shell = _started(make_client, make_shell, check_error=OfflineError())
    updates = shell.settings_view._updates
    qtbot.waitUntil(lambda: updates.state is UpdateState.FAILED)
    _updates_text(find_by_name, shell, messages.UPDATE_CHECK_OFFLINE)  # why, and what to do
    assert _status(shell) == ""  # a background check: nothing announced above the sections

    _fake(updates).check_error = None
    check = _button(find_by_name, shell.settings_view, "Check for updates")
    check.click()

    qtbot.waitUntil(lambda: updates.state is UpdateState.UP_TO_DATE)
    announced = shell.settings_view.findChild(QLabel, "settingsStatus")
    assert announced is not None and announced.text() == messages.UPDATE_UP_TO_DATE


def test_an_installer_that_fails_its_last_check_is_not_installed(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    quits: list[bool],
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    updates = _ready(qtbot, shell)
    _fake(updates).verify_error = UpdateVerificationError("changed on disk")
    _button(find_by_name, shell.settings_view, "Install update…").click()
    assert shell.update_dialog is not None

    dialog = shell.update_dialog
    _button(find_by_name, dialog, NOW.replace("&", "")).click()

    qtbot.waitUntil(lambda: updates.state is UpdateState.FAILED)
    error = dialog.findChild(QLabel, "updateError")
    assert error is not None and error.text() == messages.UPDATE_NOT_VERIFIED  # in the dialog
    assert not _button(find_by_name, dialog, NOW.replace("&", "")).isEnabled()
    assert quits == []
    assert updates.installer_to_run is None


def test_running_from_source_downloads_nothing(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION), can_install=False)
    updates = shell.settings_view._updates

    qtbot.waitUntil(lambda: updates.state is UpdateState.FROM_SOURCE)

    _updates_text(find_by_name, shell, messages.update_from_source(VERSION))
    assert _fake(updates).downloaded == []


def test_the_update_notification_can_be_turned_off(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    client = make_client()
    client.config.update(
        notifications=client.config.config.notifications.model_copy(
            update={"update_available": False}
        )
    )
    client.updater.release = release_out(VERSION)
    client.auth.sign_in(client.config.config.server_url, "alice", USER_PASSWORD)
    shell = make_shell(client)
    notes: list[str] = []
    client.notifications.notify.connect(lambda title, text: notes.append(text))
    shell.start()

    _ready(qtbot, shell)
    qtbot.wait(50)

    assert notes == []


def test_checks_repeat_on_the_timer(qtbot: QtBot, tmp_path: Path) -> None:
    fake = FakeUpdates(tmp_path)
    updates = UpdateController(fake, interval_ms=20)

    updates.start()
    qtbot.waitUntil(lambda: fake.checks >= 3)
    updates.stop()


# --- installing at the next start (app.main, before any window) --------------------------


CERT = "ab" * 32


class _Verifier:
    def __init__(self) -> None:
        self.error: Exception | None = None

    def signer(self, path: Path) -> Signer:
        if self.error is not None:
            raise self.error
        return Signer("StateLife Test", CERT)

    def file_version(self, path: Path) -> str:
        return VERSION


def _remembered_update(tmp_path: Path, verifier: _Verifier) -> Updater:
    """An Updater whose folder holds a downloaded installer remembered for the next start."""
    installer_bytes = b"MZ installer"
    release = release_out(VERSION).model_copy(
        update={
            "size_bytes": len(installer_bytes),
            "sha256": hashlib.sha256(installer_bytes).hexdigest(),
        }
    )
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / f"DropShare-{VERSION}-setup.exe").write_bytes(installer_bytes)
    api = ApiClient(
        "https://dropshare.example.com",
        TokenStore(MemoryCredentials()),
        client_version="0.3.0",
        user_agent="test",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    updater = Updater(api, "0.3.0", folder, verifier, ExpectedSigner.of("StateLife Test", [CERT]))
    updater.remember(ReadyUpdate(release, folder / f"DropShare-{VERSION}-setup.exe"))
    return updater


def test_a_remembered_update_is_installed_once_at_the_next_start(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Path] = []
    monkeypatch.setattr("dropshare_client.platform.launch_installer", launched.append)
    updater = _remembered_update(tmp_path, _Verifier())

    assert app._install_pending(updater) is True
    assert app._install_pending(updater) is False
    assert [path.name for path in launched] == [f"DropShare-{VERSION}-setup.exe"]


def test_a_remembered_update_that_fails_its_check_is_not_run(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Path] = []
    monkeypatch.setattr("dropshare_client.platform.launch_installer", launched.append)
    verifier = _Verifier()
    updater = _remembered_update(tmp_path, verifier)
    verifier.error = SignatureError("tampered")

    assert app._install_pending(updater) is False
    assert launched == []
    assert list((tmp_path / "updates").iterdir()) == []


def test_an_installer_that_cannot_start_lets_the_app_start(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_: Path) -> None:
        raise OSError("blocked")

    monkeypatch.setattr("dropshare_client.platform.launch_installer", fail)
    updater = _remembered_update(tmp_path, _Verifier())

    assert app._install_pending(updater) is False


def test_an_installer_that_cant_be_read_lets_the_app_start(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antivirus still scanning it, say: DropShare starts as usual (audit B1)."""
    launched: list[Path] = []
    monkeypatch.setattr("dropshare_client.platform.launch_installer", launched.append)
    verifier = _Verifier()
    updater = _remembered_update(tmp_path, verifier)
    verifier.error = PermissionError("in use")

    assert app._install_pending(updater) is False
    assert launched == []


def test_an_update_that_cant_be_checked_offline_waits_for_the_next_start(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[Path] = []
    monkeypatch.setattr("dropshare_client.platform.launch_installer", launched.append)
    verifier = _Verifier()
    updater = _remembered_update(tmp_path, verifier)
    verifier.error = RevocationUnavailableError("offline")

    assert app._install_pending(updater) is False
    assert launched == []
    assert updater.pending() is not None  # kept, and remembered again


def test_signing_out_forgets_the_update_and_what_was_still_arriving(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """The next account may be on another channel (OQ-49, audit B2)."""
    fake = FakeUpdates(tmp_path)
    fake.release = release_out(VERSION)
    updates = UpdateController(fake)
    updates.check(urgent=True)
    qtbot.waitUntil(lambda: updates.state is UpdateState.READY)

    updates.stop()
    assert (updates.state, updates.release, updates.urgent) == (UpdateState.IDLE, None, False)

    fake.gate = threading.Event()  # a download for the old account still running
    updates.check()
    qtbot.waitUntil(lambda: updates.state is UpdateState.DOWNLOADING)
    updates.stop()
    fake.gate.set()
    qtbot.wait(100)
    assert updates.state is UpdateState.IDLE


def test_a_release_withdrawn_after_download_is_not_installed(
    qtbot: QtBot,
    make_client: MakeClient,
    make_shell: MakeShell,
    find_by_name: FindByName,
    quits: list[bool],
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    updates = _ready(qtbot, shell)
    _fake(updates).release = None  # withdrawn
    _button(find_by_name, shell.settings_view, "Install update…").click()
    assert shell.update_dialog is not None

    _button(find_by_name, shell.update_dialog, NOW.replace("&", "")).click()

    qtbot.waitUntil(lambda: updates.state is UpdateState.UP_TO_DATE)
    assert quits == []
    assert _fake(updates).forgotten == 1  # nor at the next start


def test_a_ready_update_superseded_by_a_newer_one_is_replaced(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    updates = _ready(qtbot, shell)
    _fake(updates).release = release_out("0.5.0")

    updates.check()  # the 6-hourly check

    qtbot.waitUntil(lambda: _fake(updates).downloaded == [VERSION, "0.5.0"])
    qtbot.waitUntil(lambda: updates.state is UpdateState.READY)
    assert updates.release is not None and updates.release.version == "0.5.0"


def test_a_refused_version_with_nothing_to_install_says_so(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell
) -> None:
    shell = _started(make_client, make_shell)
    updates = shell.settings_view._updates
    qtbot.waitUntil(lambda: updates.state is UpdateState.UP_TO_DATE)
    said: list[str] = []
    updates.urgent_status.connect(said.append)

    shell._session.upgrade_required.emit(messages.UPGRADE)

    qtbot.waitUntil(lambda: bool(said))
    assert said == [f"{messages.UPDATE_REQUIRED} {messages.UPDATE_NONE_AVAILABLE}"]
    assert shell.window.view.current_state() == "error"


def test_update_on_next_restart_that_cant_be_saved_keeps_the_update_ready(
    qtbot: QtBot, make_client: MakeClient, make_shell: MakeShell, find_by_name: FindByName
) -> None:
    shell = _started(make_client, make_shell, release=release_out(VERSION))
    updates = _ready(qtbot, shell)
    _fake(updates).remember_error = OSError("disk full")
    _button(find_by_name, shell.settings_view, "Install update…").click()
    dialog = shell.update_dialog
    assert dialog is not None

    _button(find_by_name, dialog, ON_RESTART.replace("&", "")).click()

    error = dialog.findChild(QLabel, "updateError")
    qtbot.waitUntil(lambda: error is not None and error.text() == messages.UPDATE_NOT_SAVED)
    assert updates.state is UpdateState.READY  # still ready: try again
    assert _button(find_by_name, dialog, ON_RESTART.replace("&", "")).isEnabled()
    _fake(updates).remember_error = None
    _button(find_by_name, dialog, ON_RESTART.replace("&", "")).click()
    qtbot.waitUntil(lambda: updates.on_restart)


def _fake(updates: UpdateController) -> FakeUpdates:
    fake = updates._updates
    assert isinstance(fake, FakeUpdates)
    return fake
