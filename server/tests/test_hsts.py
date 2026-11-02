"""NFR-SEC-01: HSTS on every response that reached us over HTTPS, and only then."""

from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import ip_address

import pytest
from fastapi.testclient import TestClient

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app

PROXY = "10.0.0.5"
HSTS = "Strict-Transport-Security"


@contextmanager
def _client(settings: Settings, *, base_url: str, peer: str) -> Iterator[TestClient]:
    trusting = settings.model_copy(update={"trusted_proxies": [ip_address(PROXY)]})
    with TestClient(create_app(trusting), base_url=base_url, client=(peer, 50000)) as client:
        yield client


@pytest.mark.parametrize(
    ("base_url", "peer", "proto", "expected"),
    [
        ("https://dropshare.example", "203.0.113.9", None, True),  # TLS straight to the app
        ("http://dropshare.example", PROXY, "https", True),  # TLS ended at the trusted proxy
        ("http://dropshare.example", PROXY, "HTTPS", True),
        ("http://dropshare.example", PROXY, "http", False),
        ("http://dropshare.example", PROXY, "https, http", False),  # nginx sends one value
        ("http://dropshare.example", PROXY, None, False),
        ("http://dropshare.example", "203.0.113.9", "https", False),  # header from anyone else
        ("http://localhost", "127.0.0.1", None, False),  # development (OQ-12)
    ],
)
def test_hsts_only_when_reached_over_https(
    db_settings: Settings, base_url: str, peer: str, proto: str | None, expected: bool
) -> None:
    headers = {"X-Forwarded-Proto": proto} if proto else {}
    with _client(db_settings, base_url=base_url, peer=peer) as client:
        response = client.get("/health", headers=headers)

    assert response.status_code == 200
    assert (response.headers.get(HSTS) == "max-age=31536000") is expected
    assert expected or HSTS not in response.headers


def test_error_responses_carry_hsts_too(db_settings: Settings) -> None:
    with _client(db_settings, base_url="https://dropshare.example", peer=PROXY) as client:
        response = client.get("/api/v1/me")

    assert response.is_client_error  # refused before any route code: no X-Client-Version
    assert response.headers[HSTS] == "max-age=31536000"
