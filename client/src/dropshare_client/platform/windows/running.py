"""A named mutex held while DropShare runs (FR-UPD-03): Setup and the uninstaller
(installer/dropshare.iss) wait until it is gone, so files are never replaced while the app is
still stopping. Windows releases it when the process ends, however it ends."""

import sys

RUNNING_MUTEX = "StateLife DropShare running"  # the same names are in installer/dropshare.iss
SETUP_MUTEX = "StateLife DropShare setup"  # held by Setup while it installs or updates
_SYNCHRONIZE = 0x00100000
_SETUP_WAIT_SECONDS = 10 * 60

_held: list[int] = []  # kept for the life of the process


def mark_running() -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel32.CreateMutexW(None, False, RUNNING_MUTEX)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    _held.append(handle)


def wait_for_setup() -> None:
    """Return once no Setup is running, or after 10 minutes whatever happens."""
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    import ctypes
    import time
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    deadline = time.monotonic() + _SETUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, SETUP_MUTEX)
        if not handle:
            return
        kernel32.CloseHandle(handle)
        time.sleep(1)
