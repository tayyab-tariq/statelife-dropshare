from dropshare_server.core.config import Settings
from dropshare_server.mailer.base import DisabledMailer, Mailer
from dropshare_server.mailer.http_api import HttpApiMailer


def create_mailer(settings: Settings) -> Mailer:
    """The email API when configured; otherwise nothing is sent (FR-ADMIN-07)."""
    if settings.email_api_url is None or settings.email_api_key is None:
        return DisabledMailer()
    return HttpApiMailer(
        str(settings.email_api_url),
        settings.email_api_key,
        timeout_seconds=settings.email_timeout_seconds,
    )
