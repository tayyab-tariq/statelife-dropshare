"""Server test fixtures.

Tests that use the `test_database` fixture run against the DROPSHARE_TEST schema (TEST_DB_* in
server/.env), migrated to head once per session. Without those settings they are skipped.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from dropshare_server.core.config import ENV_FILE, Settings
from dropshare_server.core.db import create_db_engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


class _TestDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TEST_DB_", env_file=ENV_FILE, extra="ignore")

    host: str = Field(min_length=1)
    port: int = 1521
    service_name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)


@pytest.fixture(scope="session")
def test_database() -> Settings:
    """Settings for the migrated test schema; skips when TEST_DB_* isn't configured."""
    try:
        db = _TestDatabaseSettings()
    except ValidationError:
        pytest.skip("TEST_DB_* settings missing (see server/.env.example)")
    settings = Settings.model_validate(
        {
            "db_host": db.host,
            "db_port": db.port,
            "db_service_name": db.service_name,
            "db_user": db.user,
            "db_password": db.password,
            "storage_local_root": Path("unused"),
        }
    )
    engine = create_db_engine(settings)
    try:
        with engine.begin() as connection:
            config = Config(str(ALEMBIC_INI))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
    finally:
        engine.dispose()
    return settings


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "storage"
    root.mkdir()
    return root


@pytest.fixture
def db_settings(test_database: Settings, storage_root: Path) -> Settings:
    return test_database.model_copy(update={"storage_local_root": storage_root})


@pytest.fixture
def offline_settings(storage_root: Path) -> Settings:
    """Settings whose database is unreachable, for tests that never need it."""
    return Settings.model_validate(
        {
            "db_host": "127.0.0.1",
            "db_port": 1,
            "db_service_name": "none",
            "db_user": "none",
            "db_password": "none",
            "storage_local_root": storage_root,
        }
    )
