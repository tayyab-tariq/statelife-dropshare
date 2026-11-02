"""Running an update installer (FR-UPD-03): Inno Setup, silently, detached from the app."""

import subprocess
import sys
from pathlib import Path

# /VERYSILENT: no wizard; the installer closes DropShare if it still runs (AppMutex) and starts
# it again when done. /SUPPRESSMSGBOXES answers its prompts with the defaults.
_ARGUMENTS = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")


def launch(installer: Path) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Windows only")
    subprocess.Popen(  # noqa: S603 - a verified, signed installer with fixed arguments
        [str(installer), *_ARGUMENTS],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
