"""The email API client (FR-ADMIN-07), against a fake transport: no network."""

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from dropshare_server.mailer.base import DisabledMailer
from dropshare_server.mailer.http_api import HttpApiMailer
from dropshare_shared.schemas.admin import EmailOutcome

_URL = "https://mail.example.com/send_email"
_KEY = "s3cret-api-key"


def _mailer(handler: httpx.MockTransport) -> HttpApiMailer:
    return HttpApiMailer(_URL, SecretStr(_KEY), timeout_seconds=1, transport=handler)


def _answer(status: int = 200, body: object = None, text: str | None = None) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handle)


def test_sends_the_fields_and_key_the_api_expects() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"http_status": "200 OK"})

    outcome = _mailer(httpx.MockTransport(handle)).send("bob@example.com", "Hello", "<p>Hi</p>")

    assert outcome is EmailOutcome.SENT
    (request,) = seen
    assert (request.method, str(request.url)) == ("POST", _URL)
    assert request.headers["X-API-Key"] == _KEY
    assert json.loads(request.content) == {
        "subject": "Hello",
        "reciever": "bob@example.com",
        "text": "<p>Hi</p>",
    }


@pytest.mark.parametrize(
    "transport",
    [
        _answer(500, {"http_status": "ok"}),
        _answer(401, {"http_status": "ok"}),
        _answer(200, text="not json"),
        _answer(200, ["a", "list"]),
        _answer(200, {"status": "ok"}),
        _answer(200, {"http_status": "FAILED to send"}),
        _answer(200, {"http_status": "Internal Error"}),
    ],
    ids=["5xx", "4xx", "not-json", "not-an-object", "no-http-status", "fail", "error"],
)
def test_anything_but_a_clear_success_is_a_failure(transport: httpx.MockTransport) -> None:
    assert _mailer(transport).send("bob@example.com", "s", "t") is EmailOutcome.FAILED


@pytest.mark.parametrize("error", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError])
def test_network_problems_are_failures_never_exceptions(
    error: type[httpx.TransportError], caplog: pytest.LogCaptureFixture
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise error("down", request=request)

    with caplog.at_level(logging.DEBUG):
        outcome = _mailer(httpx.MockTransport(handle)).send("bob@example.com", "s", "<p>pw</p>")

    assert outcome is EmailOutcome.FAILED
    assert _KEY not in caplog.text
    assert "<p>pw</p>" not in caplog.text


def test_the_key_never_reaches_the_log_on_a_refusal(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        _mailer(_answer(200, {"http_status": "error"})).send("bob@example.com", "s", "secret-pw")

    assert _KEY not in caplog.text
    assert "secret-pw" not in caplog.text


def test_unconfigured_email_sends_nothing() -> None:
    assert DisabledMailer().send("bob@example.com", "s", "t") is EmailOutcome.NOT_CONFIGURED
