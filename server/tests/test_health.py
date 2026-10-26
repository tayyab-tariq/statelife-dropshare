from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app


@pytest.fixture
def offline_client(offline_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(offline_settings), raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def db_client(db_settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(db_settings), raise_server_exceptions=False) as client:
        yield client


def test_liveness_needs_no_database(offline_client: TestClient) -> None:
    response = offline_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok"}}
    assert response.headers["X-Request-ID"]


def test_readiness_reports_database_and_storage(db_client: TestClient) -> None:
    response = db_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"data": {"status": "ok", "database": "ok", "storage": "ok"}}


def test_readiness_fails_when_database_unreachable(offline_client: TestClient) -> None:
    response = offline_client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert body["error"]["details"] == {"database": "failed", "storage": "ok"}
    # No driver messages, hosts, or ORA-/DPY- codes reach the client (NFR-SEC-07).
    assert "DPY" not in response.text
    assert "127.0.0.1" not in response.text


def test_readiness_fails_when_storage_unwritable(db_settings: Settings, tmp_path: Path) -> None:
    storage_root = tmp_path / "readonly"
    storage_root.mkdir()
    settings = db_settings.model_copy(update={"storage_local_root": storage_root})
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        storage_root.chmod(0o500)
        try:
            response = client.get("/health/ready")
        finally:
            storage_root.chmod(0o700)

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"database": "ok", "storage": "failed"}
    assert str(storage_root) not in response.text
