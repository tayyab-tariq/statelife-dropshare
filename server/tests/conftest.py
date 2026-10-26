"""Server test fixtures.

Tests that use `db_settings` or `db_session` run against the DROPSHARE_TEST schema (TEST_DB_* in
server/.env), migrated to head once per session and emptied before each test. Without those
settings they are skipped.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.cli import main as cli_main
from dropshare_server.core.config import ENV_FILE, Settings
from dropshare_server.core.db import create_db_engine, create_session_factory
from dropshare_server.main import create_app
from dropshare_server.models import AuditEvent, Base
from dropshare_server.services import users as user_service
from dropshare_server.services.context import cli_context
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.users import UserRole

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
TEST_TOKEN_SECRET = "test-only-token-secret-0123456789abcdef"


class _TestDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEST_DB_", env_file=ENV_FILE, extra="ignore", hide_input_in_errors=True
    )

    host: str = Field(min_length=1)
    port: int = 1521
    service_name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)


def _settings(storage_root: Path, **database: object) -> Settings:
    # Explicit values for everything, so tests never depend on the developer's server/.env.
    return Settings.model_validate(
        {**database, "storage_local_root": storage_root, "token_secret": TEST_TOKEN_SECRET}
    )


@pytest.fixture(scope="session")
def test_database() -> Settings:
    """Settings for the migrated test schema; skips when TEST_DB_* isn't configured."""
    try:
        db = _TestDatabaseSettings()
    except ValidationError:
        pytest.skip("TEST_DB_* settings missing (see server/.env.example)")
    settings = _settings(
        Path("unused"),
        db_host=db.host,
        db_port=db.port,
        db_service_name=db.service_name,
        db_user=db.user,
        db_password=db.password,
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


@pytest.fixture(scope="session")
def test_engine(test_database: Settings) -> Iterator[Engine]:
    engine = create_db_engine(test_database)
    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(test_engine: Engine) -> None:
    """Empty every table (children first) so each test starts from a known state."""
    with test_engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(delete(table))


@pytest.fixture
def session_factory(test_engine: Engine, clean_db: None) -> sessionmaker[Session]:
    return create_session_factory(test_engine)


@pytest.fixture
def db_session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "storage"
    root.mkdir()
    return root


@pytest.fixture
def db_settings(test_database: Settings, clean_db: None, storage_root: Path) -> Settings:
    return test_database.model_copy(update={"storage_local_root": storage_root})


@pytest.fixture
def api(db_settings: Settings) -> Iterator[TestClient]:
    """A client for /api/v1 that sends a supported X-Client-Version by default."""
    with TestClient(
        create_app(db_settings),
        raise_server_exceptions=False,
        headers={"X-Client-Version": "0.1.0"},
    ) as client:
        yield client


@pytest.fixture
def cli(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str, str]]:
    """Run the admin CLI against the test schema as OS user `it-admin`; returns (code, out, err)."""
    values = {
        "DB_HOST": db_settings.db_host,
        "DB_PORT": str(db_settings.db_port),
        "DB_SERVICE_NAME": db_settings.db_service_name,
        "DB_USER": db_settings.db_user,
        "DB_PASSWORD": db_settings.db_password.get_secret_value(),
        "TOKEN_SECRET": TEST_TOKEN_SECRET,
        "STORAGE_LOCAL_ROOT": str(db_settings.storage_local_root),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("getpass.getuser", lambda: "it-admin")

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli_main(list(argv))
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture
def make_user(
    session_factory: sessionmaker[Session],
) -> Callable[..., tuple[str, str]]:
    """Create a user through the service (as the CLI would); returns (username, temp password)."""

    def create(username: str = "alice", role: UserRole = UserRole.USER) -> tuple[str, str]:
        with session_factory.begin() as session:
            created = user_service.create_user(
                session,
                cli_context("it-admin"),
                username=username,
                email=f"{username}@example.com",
                display_name=username.title(),
                role=role,
                quota_bytes=1024,
            )
        return created.user.username, created.temporary_password

    return create


NEW_PASSWORD = "a-long-new-password-42"

type Login = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]


@pytest.fixture
def login(api: TestClient) -> Login:
    """POST /auth/login: login(username, password, device="Laptop")."""

    def post(username: str, password: str, device: str = "Laptop") -> Response:
        response: Response = api.post(
            "/api/v1/auth/login",
            json={
                "login": username,
                "password": password,
                "device_name": device,
                "device_os": "Windows 11",
            },
        )
        return response

    return post


@pytest.fixture
def signed_in(
    api: TestClient, make_user: Callable[..., tuple[str, str]], login: Login
) -> dict[str, str]:
    """A user who signed in and replaced the temporary password: username, password, tokens."""
    username, temporary = make_user()
    first = login(username, temporary).json()["data"]
    changed = api.post(
        "/api/v1/me/password",
        json={"current_password": temporary, "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {first['access_token']}"},
    )
    assert changed.status_code == 200, changed.text
    return {"username": username, "password": NEW_PASSWORD, **changed.json()["data"]}


@pytest.fixture
def audit_events(session_factory: sessionmaker[Session]) -> AuditEvents:
    """audit_events(action=None): the audit rows written so far, oldest first."""

    def fetch(action: AuditAction | None = None) -> list[AuditEvent]:
        with session_factory() as session:
            query = select(AuditEvent).order_by(AuditEvent.id)
            if action is not None:
                query = query.where(AuditEvent.action == action)
            return list(session.scalars(query))

    return fetch


@pytest.fixture
def offline_settings(storage_root: Path) -> Settings:
    """Settings whose database is unreachable, for tests that never need it."""
    return _settings(
        storage_root,
        db_host="127.0.0.1",
        db_port=1,
        db_service_name="none",
        db_user="none",
        db_password="none",
    )
