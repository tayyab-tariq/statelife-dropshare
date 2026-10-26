"""Every error leaves the server in the standard envelope (requirements §9, NFR-SEC-07)."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError
from dropshare_server.main import create_app
from dropshare_shared.errors import ErrorCode


def _app_error() -> None:
    raise AppError(409, ErrorCode.NAME_CONFLICT, "Name taken", {"name": "a.txt"})


def _crash() -> None:
    raise RuntimeError("secret internal detail")


def _needs_int(count: int) -> None:
    del count


def _unauthenticated() -> None:
    raise HTTPException(401, headers={"WWW-Authenticate": "Bearer"})


def _rate_limited() -> None:
    raise HTTPException(429)


def _teapot() -> None:
    raise HTTPException(418)


def _bad_gateway() -> None:
    raise HTTPException(502, detail="upstream db at 10.0.0.5 failed")


@pytest.fixture
def client(offline_settings: Settings) -> Iterator[TestClient]:
    app: FastAPI = create_app(offline_settings)
    app.add_api_route("/test/app-error", _app_error)
    app.add_api_route("/test/crash", _crash)
    app.add_api_route("/test/validation", _needs_int)
    app.add_api_route("/test/401", _unauthenticated)
    app.add_api_route("/test/429", _rate_limited)
    app.add_api_route("/test/418", _teapot)
    app.add_api_route("/test/502", _bad_gateway)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/test/401", 401, "UNAUTHENTICATED"),
        ("/test/429", 429, "RATE_LIMITED"),
        ("/test/418", 418, "VALIDATION_ERROR"),
        ("/test/502", 500, "INTERNAL_ERROR"),
    ],
)
def test_framework_http_errors_map_to_stable_codes(
    client: TestClient, path: str, status: int, code: str
) -> None:
    response = client.get(path)

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["X-Request-ID"]
    assert "10.0.0.5" not in response.text


def test_http_error_headers_are_kept(client: TestClient) -> None:
    assert client.get("/test/401").headers["WWW-Authenticate"] == "Bearer"
    assert client.post("/health").headers["Allow"] == "GET"


def test_app_error_uses_its_status_code_and_details(client: TestClient) -> None:
    response = client.get("/test/app-error")

    assert response.status_code == 409
    assert response.json() == {
        "error": {"code": "NAME_CONFLICT", "message": "Name taken", "details": {"name": "a.txt"}}
    }


def test_validation_error_is_400_without_echoing_input(client: TestClient) -> None:
    response = client.get("/test/validation", params={"count": "my-secret-value"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"][0]["loc"] == ["query", "count"]
    assert "my-secret-value" not in response.text


def test_unknown_route_is_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_wrong_method_is_method_not_allowed(client: TestClient) -> None:
    response = client.post("/health")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unexpected_error_is_generic_500_with_request_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = client.get("/test/crash")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "Something went wrong", "details": None}
    }
    assert response.headers["X-Request-ID"]
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text
    assert any(record.exc_info for record in caplog.records if record.message == "Unhandled error")
