from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from scanner_fake import FakeScanner

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
    data = response.json()["data"]
    assert {key: data[key] for key in ("status", "database", "storage", "scanner")} == {
        "status": "ok",
        "database": "ok",
        "storage": "ok",
        "scanner": "ok",
    }
    assert 1.9 < data["signatures_age_hours"] < 2.1


def test_readiness_fails_when_database_unreachable(offline_client: TestClient) -> None:
    response = offline_client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert body["error"]["details"] == {"database": "failed", "storage": "ok", "scanner": "ok"}
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
    assert response.json()["error"]["details"] == {
        "database": "ok",
        "storage": "failed",
        "scanner": "ok",
    }
    assert str(storage_root) not in response.text


def test_readiness_fails_when_the_scanner_is_unreachable(
    db_client: TestClient, scanner: FakeScanner
) -> None:
    scanner.down = True

    response = db_client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {
        "database": "ok",
        "storage": "ok",
        "scanner": "failed",
    }


def test_old_signatures_are_reported_but_keep_the_server_ready(
    db_client: TestClient, scanner: FakeScanner, caplog: pytest.LogCaptureFixture
) -> None:
    scanner.signature_age = timedelta(days=3)

    response = db_client.get("/health/ready")

    assert response.status_code == 200  # OQ-58 (3)
    assert response.json()["data"]["signatures_age_hours"] == pytest.approx(72, abs=0.2)
    assert "freshclam" in caplog.text
