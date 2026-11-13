"""The only way the server sends email (FR-ADMIN-07)."""

from typing import Protocol

from dropshare_shared.schemas.admin import EmailOutcome


class Mailer(Protocol):
    def send(self, to: str, subject: str, html: str) -> EmailOutcome:
        """Send one HTML email. Never raises for a delivery problem: the caller has already
        committed the change the email is about, so it only reports what happened."""


class DisabledMailer:
    """Used when EMAIL_API_URL and EMAIL_API_KEY aren't set: nothing is sent."""

    def send(self, to: str, subject: str, html: str) -> EmailOutcome:
        return EmailOutcome.NOT_CONFIGURED
