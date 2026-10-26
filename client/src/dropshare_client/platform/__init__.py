"""Picks the OS implementation of each platform capability."""

import sys

from dropshare_client.platform.base import Paths


def paths() -> Paths:
    if sys.platform == "win32":
        from dropshare_client.platform.windows.paths import WindowsPaths

        return WindowsPaths()
    if sys.platform == "darwin":
        from dropshare_client.platform.macos.paths import MacPaths

        return MacPaths()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
