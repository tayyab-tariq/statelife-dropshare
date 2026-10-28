"""Server settings from environment variables, validated at startup (NFR-OPS-03, NFR-SEC-09)."""

from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from dropshare_shared.version import parse_version

# server/.env for local development; production sets real environment variables instead.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

_MIB = 1024**2
_GIB = 1024**3


class DatabaseSettings(BaseSettings):
    """What Alembic and the app need to reach the database, and nothing else."""

    # hide_input_in_errors: a validation error must never echo values such as DB_PASSWORD.
    model_config = SettingsConfigDict(
        env_file=ENV_FILE, extra="ignore", frozen=True, hide_input_in_errors=True
    )

    # Oracle 19c is the target (D3); PostgreSQL is a temporary local-dev option (requirements §15).
    db_dialect: Literal["oracle", "postgresql"] = "oracle"
    db_host: str = Field(min_length=1)
    # Unset means the driver default: 1521 for Oracle, 5432 for PostgreSQL.
    db_port: int | None = Field(default=None, ge=1, le=65535)
    # Oracle: the PDB service name. PostgreSQL: the database name.
    db_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)


class Settings(DatabaseSettings):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    storage_backend: Literal["local"] = "local"
    storage_local_root: Path

    # Auth (FR-AUTH-03, FR-AUTH-06). The secret signs access tokens; at least 32 bytes.
    token_secret: SecretStr = Field(min_length=32)
    access_token_minutes: int = Field(default=15, ge=1)
    refresh_token_inactivity_days: int = Field(default=30, ge=1)
    refresh_token_absolute_days: int = Field(default=90, ge=1)
    lockout_threshold: int = Field(default=5, ge=1)
    lockout_minutes: int = Field(default=15, ge=1)

    # Clients below this version get 426 UPGRADE_REQUIRED (FR-UPD-04).
    min_client_version: str = "0.1.0"
    default_quota_bytes: int = Field(default=50 * _GIB, ge=0)  # NFR-SCALE-03

    # Files and transfers (FR-XFER-01, NFR-SCALE-01, requirements §7.2).
    max_file_size_bytes: int = Field(default=10 * _GIB, ge=0)
    upload_chunk_size_bytes: int = Field(default=8 * _MIB, ge=1)
    upload_expiry_hours: int = Field(default=24, ge=1)

    # Long-poll (FR-SYNC-03): how long POST /changes/wait holds, and how often it re-checks.
    long_poll_timeout_seconds: float = Field(default=55, gt=0, le=60)  # clients wait 75 s
    long_poll_interval_seconds: float = Field(default=1, gt=0)

    # Reverse proxies whose X-Forwarded-For is trusted for the audit source IP.
    trusted_proxies: list[IPv4Address | IPv6Address] = []

    @field_validator("min_client_version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        parse_version(value)  # same parser as the request check, so it can never fail open
        return value.strip()
