"""Structured logs (NFR-OPS-01): JSON lines, request ID, no secrets."""

import json
import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from dropshare_server.core.config import Settings
from dropshare_server.core.db import create_db_engine
from dropshare_server.core.logging import configure_logging
from dropshare_server.main import create_app


@pytest.fixture(autouse=True)
def _restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    yield
    root.handlers[:], level = saved
    root.setLevel(level)


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def test_request_log_is_json_with_request_id(
    offline_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(offline_settings)) as client:
        response = client.get("/health", params={"token": "secret-query-value"})

    # Only server-side lines: the test's own HTTP client (httpx) logs full URLs itself.
    lines = [
        line
        for line in _json_lines(capsys.readouterr().out)
        if not str(line["logger"]).startswith("httpx")
    ]
    request_line = next(line for line in lines if line["message"] == "Request handled")
    assert request_line["request_id"] == response.headers["X-Request-ID"]
    assert request_line["path"] == "/health"
    assert request_line["status"] == 200
    assert "secret-query-value" not in json.dumps(lines)


def test_only_allow_listed_extra_fields_are_logged(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")

    logging.getLogger("test").info("hello", extra={"status": 200, "password": "hunter2"})

    (line,) = _json_lines(capsys.readouterr().out)
    assert line["status"] == 200
    assert "password" not in line


def test_uvicorn_logs_go_through_the_json_handler() -> None:
    logging.getLogger("uvicorn.error").addHandler(logging.NullHandler())

    configure_logging("INFO")

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert logging.getLogger(name).handlers == []
        assert logging.getLogger(name).propagate
    assert not logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)


def test_sql_errors_do_not_contain_bound_values(test_database: Settings) -> None:
    engine = create_db_engine(test_database)
    try:
        with engine.connect() as connection, pytest.raises(DatabaseError) as caught:
            connection.execute(
                text("SELECT :value FROM table_that_does_not_exist"),
                {"value": "secret-bound-value"},
            )
    finally:
        engine.dispose()

    assert "secret-bound-value" not in str(caught.value)
