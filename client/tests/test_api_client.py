"""API client: headers, envelope parsing, error mapping, and the one-at-a-time token refresh
(architecture.md §4, OQ-16)."""

import json
import logging
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import (
    ApiError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_client.services.tokens import TokenStore
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.users import UserOut

type Handler = Callable[[httpx.Request], httpx.Response]

_USER = {
    "id": "00000000-0000-0000-0000-000000000001",
    "username": "alice",
    "email": "alice@example.com",
    "display_name": "Alice",
    "role": "user",
    "quota_bytes": 1,
}


class _Credentials:
    def __init__(self, secret: str | None = None) -> None:
        self.secret = secret

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = None


def _pair(n: int) -> dict[str, object]:
    return {
        "data": {
            "access_token": f"access-{n}",
            "refresh_token": f"refresh-{n}",
            "token_type": "bearer",
            "expires_in": 900,
            "must_change_password": False,
        }
    }


def _client(handler: Handler, credentials: _Credentials) -> ApiClient:
    return ApiClient(
        "https://dropshare.example.com",
        TokenStore(credentials),
        client_version="1.2.3",
        user_agent="DropShare/1.2.3 (test)",
        transport=httpx.MockTransport(handler),
    )


def _me_handler(refreshes: list[str], *, valid: str = "access-1") -> Handler:
    """GET /me accepts only `valid`; refresh rotates refresh-0 -> refresh-1 once."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/refresh":
            token = json.loads(request.content)["refresh_token"]
            refreshes.append(token)
            time.sleep(0.05)  # widen the window for concurrent callers
            if token != "refresh-0":
                return httpx.Response(
                    401, json={"error": {"code": "UNAUTHENTICATED", "message": "x"}}
                )
            return httpx.Response(200, json=_pair(1))
        if request.headers.get("Authorization") != f"Bearer {valid}":
            return httpx.Response(401, json={"error": {"code": "UNAUTHENTICATED", "message": "x"}})
        return httpx.Response(200, json={"data": _USER})

    return handle


def test_requests_carry_the_client_version_and_parse_the_envelope() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": _USER, "extra": "ignored"})

    api = _client(handle, _Credentials())

    user = api.data("GET", "/api/v1/me", UserOut, authenticated=False)

    assert user.username == "alice"
    assert seen[0].headers["X-Client-Version"] == "1.2.3"
    assert seen[0].headers["User-Agent"] == "DropShare/1.2.3 (test)"
    assert "Authorization" not in seen[0].headers


def test_expired_access_token_is_refreshed_once_and_the_request_retried() -> None:
    refreshes: list[str] = []
    credentials = _Credentials("refresh-0")
    api = _client(_me_handler(refreshes), credentials)

    user = api.data("GET", "/api/v1/me", UserOut)

    assert user.username == "alice"
    assert refreshes == ["refresh-0"]
    assert credentials.secret == "refresh-1"  # the rotated token replaced the used one


def test_concurrent_requests_share_a_single_refresh() -> None:
    refreshes: list[str] = []
    api = _client(_me_handler(refreshes), _Credentials("refresh-0"))
    results: list[str] = []

    def call() -> None:
        results.append(api.data("GET", "/api/v1/me", UserOut).username)

    threads = [threading.Thread(target=call) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results == ["alice"] * 8
    assert refreshes == ["refresh-0"]  # never the same token twice (OQ-16)


def test_rejected_refresh_ends_the_session_and_forgets_the_token() -> None:
    credentials = _Credentials("refresh-revoked")
    api = _client(_me_handler([]), credentials)

    with pytest.raises(SessionExpiredError):
        api.data("GET", "/api/v1/me", UserOut)

    assert credentials.secret is None


def test_refresh_that_never_reached_the_server_keeps_the_token() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    credentials = _Credentials("refresh-0")
    api = _client(handle, credentials)

    with pytest.raises(OfflineError):
        api.ensure_access_token()

    assert credentials.secret == "refresh-0"


def test_refresh_with_a_lost_answer_is_never_resent() -> None:
    """The server may have rotated the token already; re-sending it would end the session
    for good (OQ-16), so the client signs in again instead."""
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["refresh_token"])
        raise httpx.ReadTimeout("no answer", request=request)

    credentials = _Credentials("refresh-0")
    api = _client(handle, credentials)

    with pytest.raises(SessionExpiredError):
        api.ensure_access_token()
    with pytest.raises(SessionExpiredError):
        api.ensure_access_token()

    assert sent == ["refresh-0"]
    assert credentials.secret is None


def test_the_stored_token_is_removed_before_it_is_sent() -> None:
    """A crash after sending must never leave a used token for the next launch (OQ-16)."""
    stored_while_sending: list[str | None] = []
    credentials = _Credentials("refresh-0")

    def handle(request: httpx.Request) -> httpx.Response:
        stored_while_sending.append(credentials.secret)
        return httpx.Response(200, json=_pair(1))

    _client(handle, credentials).ensure_access_token()

    assert stored_while_sending == [None]
    assert credentials.secret == "refresh-1"


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_server_error_on_refresh_ends_the_session(status: int) -> None:
    """A proxy can answer 5xx after the server already rotated the token: never resend it."""
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["refresh_token"])
        return httpx.Response(status, text="Bad gateway")

    credentials = _Credentials("refresh-0")
    api = _client(handle, credentials)

    with pytest.raises(SessionExpiredError):
        api.ensure_access_token()
    with pytest.raises(SessionExpiredError):
        api.ensure_access_token()

    assert sent == ["refresh-0"]
    assert credentials.secret is None


def test_refusals_before_the_token_is_used_keep_it() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Slow down")  # proxy rate limit, no envelope

    credentials = _Credentials("refresh-0")

    with pytest.raises(ApiError) as caught:
        _client(handle, credentials).ensure_access_token()

    assert caught.value.code == ErrorCode.RATE_LIMITED
    assert credentials.secret == "refresh-0"


def test_a_failed_save_after_rotation_leaves_nothing_stale_behind() -> None:
    class _Failing(_Credentials):
        def set(self, secret: str) -> None:
            raise OSError("credential store unavailable")

    credentials = _Failing("refresh-0")
    api = _client(lambda request: httpx.Response(200, json=_pair(1)), credentials)

    assert api.ensure_access_token() == "access-1"  # this session keeps working

    assert credentials.secret is None  # the used refresh-0 is gone: a restart asks to sign in


def test_refresh_is_logged_with_its_request_id(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_pair(1), headers={"X-Request-ID": "req-42"})

    _client(handle, _Credentials("refresh-0")).ensure_access_token()

    assert "POST /api/v1/auth/refresh -> 200" in caplog.text
    assert "req-42" in caplog.text


def test_gateway_errors_without_an_envelope_mean_offline() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="<html>Maintenance</html>")

    with pytest.raises(OfflineError):
        _client(handle, _Credentials()).data("GET", "/x", UserOut, authenticated=False)


def test_errors_keep_code_message_details_and_request_id() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = {"error": {"code": "SOMETHING_NEW", "message": "Newer server", "details": {"a": 1}}}
        return httpx.Response(409, json=body, headers={"X-Request-ID": "abc"})

    with pytest.raises(ApiError) as caught:
        _client(handle, _Credentials()).data("GET", "/x", UserOut, authenticated=False)

    error = caught.value
    assert (error.status, error.code, error.message) == (409, "SOMETHING_NEW", "Newer server")
    assert (error.details, error.request_id) == ({"a": 1}, "abc")


def test_too_old_client_raises_upgrade_required() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = {
            "error": {
                "code": "UPGRADE_REQUIRED",
                "message": "Update",
                "details": {"min_version": "2.0.0"},
            }
        }
        return httpx.Response(426, json=body)

    with pytest.raises(UpgradeRequiredError) as caught:
        _client(handle, _Credentials()).data("GET", "/x", UserOut, authenticated=False)

    assert caught.value.min_version == "2.0.0"


def test_unexpected_bodies_are_reported_not_crashed_on() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(418, text="<html>I'm a teapot</html>")

    with pytest.raises(ApiError) as caught:
        _client(handle, _Credentials()).data("GET", "/x", UserOut, authenticated=False)

    assert (caught.value.status, caught.value.code) == (418, "INVALID_RESPONSE")


def test_connection_failures_mean_offline() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with pytest.raises(OfflineError):
        _client(handle, _Credentials()).data("GET", "/x", UserOut, authenticated=False)


def test_logs_never_contain_tokens(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api = _client(_me_handler([]), _Credentials("refresh-0"))

    api.data("GET", "/api/v1/me", UserOut)

    assert "GET /api/v1/me -> 200" in caplog.text
    for secret in ("access-1", "refresh-0", "refresh-1"):
        assert secret not in caplog.text
