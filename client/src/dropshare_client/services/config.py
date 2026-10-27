"""Client settings stored as JSON in the app data folder. Never holds secrets."""

import contextlib
import json
import logging
import os
import tempfile
import threading
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

logger = logging.getLogger(__name__)

# Replaced by the production address when installer builds exist (FR-CLIENT-05, roadmap F7).
DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
_LOCAL_HOSTS = frozenset({"localhost"})


def check_server_url(url: str) -> str:
    """The URL without a trailing slash, or ValueError saying what is wrong.

    HTTPS is required, except plain HTTP to this computer for development (OQ-12, NFR-SEC-01).
    """
    parts = urlsplit(url.strip())
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("Enter a server address like https://dropshare.example.com.")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("The server address can't contain a user name, query or fragment.")
    if parts.scheme == "http" and not _is_this_computer(parts.hostname):
        raise ValueError("The server address must start with https://.")
    return url.strip().rstrip("/")


def _is_this_computer(host: str) -> bool:
    if host.lower() in _LOCAL_HOSTS:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


class ClientConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    server_url: str = DEFAULT_SERVER_URL
    sync_root: Path | None = None

    @field_validator("server_url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        return check_server_url(value)


class ConfigStore:
    """The current settings, loaded once and saved on every change. Thread-safe."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._config = self._load()

    @property
    def config(self) -> ClientConfig:
        return self._config

    def update(self, **changes: object) -> ClientConfig:
        """Apply and save `changes` (validated); the old settings stay if saving fails."""
        with self._lock:
            updated = ClientConfig.model_validate({**self._config.model_dump(), **changes})
            self._save(updated)
            self._config = updated
            return updated

    def _load(self) -> ClientConfig:
        """Saved settings, or defaults when there are none or they can't be read."""
        try:
            return ClientConfig.model_validate_json(self._path.read_bytes())
        except FileNotFoundError:
            return ClientConfig()
        except (OSError, ValidationError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable settings file %s", self._path, exc_info=True)
            return ClientConfig()

    def _save(self, config: ClientConfig) -> None:
        """Write atomically: a crash mid-write never leaves a half-written file."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".config-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(config.model_dump_json(indent=2))
                out.flush()
                os.fsync(out.fileno())
            Path(temp_name).replace(self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(temp_name).unlink()
            raise
