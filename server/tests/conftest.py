"""Server test fixtures.

Tests that use `db_settings` or `db_session` run against the test schema or database (TEST_DB_* in
server/.env), migrated to head once per session and emptied before each test. Without those
settings they are skipped.
"""

import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

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
_DEFAULT_PORTS = {"oracle": 1521, "postgresql": 5432}


class _TestDatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TEST_DB_", env_file=ENV_FILE, extra="ignore", hide_input_in_errors=True
    )

    dialect: Literal["oracle", "postgresql"] = "oracle"
    host: str = Field(min_length=1)
    port: int | None = None
    name: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password: SecretStr = Field(min_length=1)


class SettingsWithoutEnvFile(Settings):
    """Settings that never read server/.env, so tests don't depend on the developer's values
    (only TEST_DB_* come from there, through _TestDatabaseSettings). Validating Settings
    itself would still read the file, even through model_validate."""

    model_config = SettingsConfigDict(**{**Settings.model_config, "env_file": None})


def _settings(storage_root: Path, **database: object) -> Settings:
    return SettingsWithoutEnvFile.model_validate(
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
        db_dialect=db.dialect,
        db_host=db.host,
        db_port=db.port,
        db_name=db.name,
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


# Tiny transfer limits so chunking, quota and long-poll timeouts are quick to exercise.
TEST_CHUNK_SIZE = 4
TEST_MAX_FILE_SIZE = 4096
TEST_QUOTA = 1024  # given to every user made by `make_user`


@pytest.fixture
def db_settings(test_database: Settings, clean_db: None, storage_root: Path) -> Settings:
    return test_database.model_copy(
        update={
            "storage_local_root": storage_root,
            "upload_chunk_size_bytes": TEST_CHUNK_SIZE,
            "max_file_size_bytes": TEST_MAX_FILE_SIZE,
            "long_poll_timeout_seconds": 1.0,
            "long_poll_interval_seconds": 0.05,
        }
    )


@pytest.fixture
def api(db_settings: Settings) -> Iterator[TestClient]:
    """A client for /api/v1 that sends a supported X-Client-Version by default."""
    with TestClient(
        create_app(db_settings),
        raise_server_exceptions=False,
        headers={"X-Client-Version": "0.2.0"},
    ) as client:
        yield client


@pytest.fixture
def cli(
    db_settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str, str]]:
    """Run the admin CLI against the test schema as OS user `it-admin`; returns (code, out, err)."""
    values = {
        "DB_DIALECT": db_settings.db_dialect,
        "DB_HOST": db_settings.db_host,
        "DB_NAME": db_settings.db_name,
        "DB_USER": db_settings.db_user,
        "DB_PASSWORD": db_settings.db_password.get_secret_value(),
        "TOKEN_SECRET": TEST_TOKEN_SECRET,
        "STORAGE_LOCAL_ROOT": str(db_settings.storage_local_root),
    }
    # Always explicit: an unset DB_PORT would fall back to the developer's server/.env.
    values["DB_PORT"] = str(db_settings.db_port or _DEFAULT_PORTS[db_settings.db_dialect])
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("getpass.getuser", lambda: "it-admin")
    # The CLI builds its own Settings: keep the developer's server/.env out of those too.
    monkeypatch.setitem(Settings.model_config, "env_file", None)

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
                quota_bytes=TEST_QUOTA,
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


type SignIn = Callable[..., dict[str, str]]


@pytest.fixture
def sign_in(api: TestClient, make_user: Callable[..., tuple[str, str]], login: Login) -> SignIn:
    """sign_in(username="alice"): create a user who signed in and replaced the temporary
    password; returns username, password, tokens and ready-made `Authorization` header value."""

    def create(username: str = "alice") -> dict[str, str]:
        username, temporary = make_user(username)
        first = login(username, temporary).json()["data"]
        changed = api.post(
            "/api/v1/me/password",
            json={"current_password": temporary, "new_password": NEW_PASSWORD},
            headers={"Authorization": f"Bearer {first['access_token']}"},
        )
        assert changed.status_code == 200, changed.text
        tokens = changed.json()["data"]
        return {
            "username": username,
            "password": NEW_PASSWORD,
            "authorization": f"Bearer {tokens['access_token']}",
            **tokens,
        }

    return create


@pytest.fixture
def signed_in(sign_in: SignIn) -> dict[str, str]:
    """A user who signed in and replaced the temporary password: username, password, tokens."""
    return sign_in()


type RootOf = Callable[[dict[str, str]], str]
type UploadFile = Callable[..., Response]


@pytest.fixture
def root_of(api: TestClient) -> RootOf:
    """root_of(user): the id of the user's personal root folder."""

    def find(user: dict[str, str]) -> str:
        response = api.get("/api/v1/namespaces", headers={"Authorization": user["authorization"]})
        assert response.status_code == 200, response.text
        root: str = response.json()["data"][0]["root_node_id"]
        return root

    return find


@pytest.fixture
def upload_file(api: TestClient) -> UploadFile:
    """upload_file(user, content, parent_id=..., name=...) or (user, content, node_id=...,
    base_revision=...): start, send every chunk, complete. Returns the complete response."""

    def upload(user: dict[str, str], content: bytes, **target: object) -> Response:
        headers = {"Authorization": user["authorization"]}
        started = api.post(
            "/api/v1/uploads",
            json={
                **{
                    key: str(value) if key.endswith("_id") else value
                    for key, value in target.items()
                },
                "size_bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            headers=headers,
        )
        assert started.status_code == 201, started.text
        upload = started.json()["data"]
        size = upload["chunk_size"]
        for index in range(upload["chunk_count"]):
            chunk = content[index * size : (index + 1) * size]
            sent = api.put(
                f"/api/v1/uploads/{upload['id']}/chunks/{index}",
                content=chunk,
                headers={**headers, "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
            )
            assert sent.status_code == 204, sent.text
        completed: Response = api.post(f"/api/v1/uploads/{upload['id']}/complete", headers=headers)
        return completed

    return upload


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
        db_name="none",
        db_user="none",
        db_password="none",
    )
