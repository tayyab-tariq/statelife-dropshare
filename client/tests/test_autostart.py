"""Start at Windows sign-in (FR-CLIENT-04)."""

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from dropshare_client.platform.macos.autostart import MacAutostart
from dropshare_client.platform.windows.autostart import WindowsAutostart, command


def test_registered_command_starts_the_app_in_the_tray() -> None:
    assert (
        command(Path(r"C:\Program Files\StateLife DropShare\DropShare.exe"))
        == r'"C:\Program Files\StateLife DropShare\DropShare.exe" --background'
    )


def test_running_from_source_never_registers_anything() -> None:
    for autostart in (WindowsAutostart(), MacAutostart()):
        assert not autostart.available()
        autostart.set_enabled(True)
        assert not autostart.is_enabled()


@pytest.fixture
def installed_build(monkeypatch: pytest.MonkeyPatch) -> Iterator[WindowsAutostart]:
    """Pretends to be the installed app, and removes the registration afterwards."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    autostart = WindowsAutostart()
    yield autostart
    autostart.set_enabled(False)


@pytest.mark.windows_only
def test_enable_and_disable_through_the_run_key(installed_build: WindowsAutostart) -> None:
    installed_build.set_enabled(True)
    assert installed_build.is_enabled()

    installed_build.set_enabled(False)
    assert not installed_build.is_enabled()
    installed_build.set_enabled(False)  # already off: not an error
