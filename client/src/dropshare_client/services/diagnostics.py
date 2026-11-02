"""Logs without secrets, and "Export diagnostics" (FR-CLIENT-06): a zip of the logs, the settings
and a few facts about this computer, for support. Never tokens, passwords or file content (the
index, which names every file, isn't included either). Blocking: run it on the pool."""

import contextlib
import logging
import os
import platform
import re
import sys
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from dropshare_client.services.config import ClientConfig

# Bearer values, JWTs and JSON token fields: never written to a log (architecture.md §5).
_SECRETS = re.compile(
    r"(Bearer\s+)\S+"
    r"|eyJ[\w-]+\.[\w-]+\.[\w-]+"
    r"|(\"(?:access_token|refresh_token|password|current_password|new_password)\"\s*:\s*\")[^\"]*",
    re.IGNORECASE,
)
LOG_NAME = "dropshare.log"


def redact(text: str) -> str:
    return _SECRETS.sub(lambda m: (m.group(1) or m.group(2) or "") + "[redacted]", text)


class RedactingFormatter(logging.Formatter):
    """Redacts the whole formatted record, tracebacks included: an exception's text can
    carry a secret even when the log message doesn't."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def export(
    destination: Path, log_dir: Path, config: ClientConfig, version: str, qt_version: str
) -> Path:
    """Write the zip to `destination` (replacing it: the user chose to). Written next to it
    first, so a failure never leaves half a zip under that name."""
    handle, temp_name = tempfile.mkstemp(dir=destination.parent, prefix=".diagnostics-")
    os.close(handle)
    temp = Path(temp_name)
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("about.txt", _about(version, qt_version))
            archive.writestr("config.json", config.model_dump_json(indent=2))
            for log in sorted(log_dir.glob(f"{LOG_NAME}*")):
                # Redacted again: lines written before a pattern was added stay safe too.
                text = log.read_text(encoding="utf-8", errors="replace")
                archive.writestr(f"logs/{log.name}", redact(text))
        temp.replace(destination)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    return destination


def _about(version: str, qt_version: str) -> str:
    return "\n".join(
        [
            f"DropShare {version}",
            f"Exported {datetime.now(UTC).isoformat(timespec='seconds')}",
            f"OS {platform.platform()}",
            f"Python {sys.version.split()[0]}",
            f"Qt {qt_version}",
            "",
        ]
    )


def default_name(now: datetime | None = None) -> str:
    return f"DropShare diagnostics {(now or datetime.now(UTC)).astimezone():%Y-%m-%d %H-%M}.zip"
