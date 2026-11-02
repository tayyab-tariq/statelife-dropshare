"""Start at Windows sign-in through the per-user Run key (FR-CLIENT-04).

Type-checked only when mypy runs on Windows (the bodies are unreachable for mypy elsewhere).
"""

import subprocess
import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "StateLife DropShare"


def command(executable: Path) -> str:
    """What Windows runs at sign-in: the installed app, starting in the tray."""
    return subprocess.list2cmdline([str(executable), "--background"])


class WindowsAutostart:
    def available(self) -> bool:
        # Only an installed build has a stable path to register; running from source, the
        # interpreter would be registered instead of the app.
        return bool(getattr(sys, "frozen", False))

    def is_enabled(self) -> bool:
        if sys.platform != "win32" or not self.available():
            return False
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, _VALUE_NAME)
        except OSError:  # FileNotFoundError when not registered
            return False
        return isinstance(value, str) and value == command(Path(sys.executable))

    def set_enabled(self, enabled: bool) -> None:
        if sys.platform != "win32" or not self.available():
            return
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, command(Path(sys.executable)))
                return
            try:
                winreg.DeleteValue(key, _VALUE_NAME)
            except FileNotFoundError:
                return
