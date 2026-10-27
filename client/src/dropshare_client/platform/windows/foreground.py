"""Let the running instance take the foreground when a second launch asks it to
(FR-CLIENT-07). Windows only lets the process that has the foreground hand it over."""

import ctypes
import sys

_ASFW_ANY = 0xFFFFFFFF


def allow_any_process_to_take_foreground() -> None:
    if sys.platform == "win32":
        ctypes.windll.user32.AllowSetForegroundWindow(_ASFW_ANY)
