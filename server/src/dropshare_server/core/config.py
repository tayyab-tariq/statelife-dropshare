"""Server settings from environment variables, validated at startup (NFR-OPS-03, NFR-SEC-09)."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# server/.env for local development; production sets real environment variables instead.
ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore", frozen=True)

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    db_host: str = Field(min_length=1)
    db_port: int = Field(default=1521, ge=1, le=65535)
    db_service_name: str = Field(min_length=1)
    db_user: str = Field(min_length=1)
    db_password: SecretStr = Field(min_length=1)
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    storage_backend: Literal["local"] = "local"
    storage_local_root: Path
