from dropshare_server.core.config import Settings
from dropshare_server.scanner.base import Scanner
from dropshare_server.scanner.clamd import ClamdScanner


def create_scanner(settings: Settings, *, reply_timeout: float | None = None) -> Scanner:
    """The scan worker's scanner, which waits as long as the largest file takes; pass a short
    `reply_timeout` for scans within a request (OQ-58)."""
    return ClamdScanner(
        settings.clamd_host,
        settings.clamd_port,
        connect_timeout=settings.clamd_connect_timeout_seconds,
        reply_timeout=reply_timeout or settings.clamd_reply_timeout_seconds,
    )
