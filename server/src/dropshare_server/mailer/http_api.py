"""The organization's email API (FR-ADMIN-07): one JSON POST per email, authenticated by an
API key header."""

import logging

import httpx
from pydantic import SecretStr

from dropshare_shared.schemas.admin import EmailOutcome

logger = logging.getLogger(__name__)

# An `http_status` holding either word means the API didn't send the email.
_FAILURE_WORDS = ("fail", "error")


class HttpApiMailer:
    def __init__(
        self,
        url: str,
        api_key: SecretStr,
        *,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """`transport` replaces the network in tests."""
        self._url = url
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._transport = transport

    def send(self, to: str, subject: str, html: str) -> EmailOutcome:
        # Sent once, never retried: a retry after a timeout could deliver two different
        # messages if the first did arrive (OQ-61 (1)). The admin has the password either way.
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                response = client.post(
                    self._url,
                    # "reciever" is how the API spells it.
                    json={"subject": subject, "reciever": to, "text": html},
                    headers={"X-API-Key": self._api_key.get_secret_value()},
                )
        except httpx.HTTPError as exc:
            # The exception's text can include the URL, never the headers.
            logger.warning("Email API unreachable: %s", type(exc).__name__)
            return EmailOutcome.FAILED
        reason = _failure(response)
        if reason is not None:
            logger.warning("Email API refused the email: %s", reason)
            return EmailOutcome.FAILED
        return EmailOutcome.SENT


def _failure(response: httpx.Response) -> str | None:
    """Why the response means the email wasn't sent, or None when it was."""
    if not response.is_success:
        return f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return "the response isn't JSON"
    if not isinstance(body, dict) or "http_status" not in body:
        return "the response has no http_status"
    status = str(body["http_status"])
    if any(word in status.lower() for word in _FAILURE_WORDS):
        return f"http_status {status[:100]!r}"
    return None
