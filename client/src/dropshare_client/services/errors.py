"""What can go wrong talking to the server. Controllers map these to messages."""

from dropshare_shared.errors import ErrorCode


class ApiError(Exception):
    """The server answered with the standard error envelope."""

    def __init__(
        self,
        status: int,
        code: ErrorCode | str,
        message: str,
        details: dict[str, object] | list[object] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id


class OfflineError(Exception):
    """The server couldn't be reached (connection failed or timed out)."""


class UpgradeRequiredError(Exception):
    """426: this client version is no longer accepted (FR-UPD-04)."""

    def __init__(self, min_version: str | None) -> None:
        super().__init__(f"Client upgrade required (minimum {min_version})")
        self.min_version = min_version


class SessionExpiredError(Exception):
    """The stored sign-in can't be used any more; the user must sign in again.

    `code` says why when the server told us (e.g. ACCOUNT_DISABLED); None for an expired,
    revoked or possibly-used refresh token.
    """

    def __init__(self, code: ErrorCode | str | None = None, request_id: str | None = None) -> None:
        super().__init__(f"Session expired ({code or 'sign in again'})")
        self.code = code
        self.request_id = request_id


class InvalidInputError(ValueError):
    """What the user typed can't be sent (e.g. too long). The message never includes it."""


class TransferCancelledError(Exception):
    """The caller asked the transfer to stop. A partial download is kept to resume from."""


class SourceChangedError(Exception):
    """The file changed while it was being uploaded; hash it again and start over."""


class RemoteContentChangedError(Exception):
    """The server's content isn't the version asked for any more; fetch the changes first."""


class ContentMismatchError(Exception):
    """A download didn't match its SHA-256 even after a fresh attempt (NFR-REL-02)."""
