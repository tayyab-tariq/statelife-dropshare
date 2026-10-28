"""What can go wrong talking to the server. Controllers map these to messages."""

from enum import StrEnum

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


class FileProblem(StrEnum):
    """Why a file action or an upload from the app (FR-FILE-02/03/04) can't be done. The
    words for each are in controllers/messages.py; `FileActionError.name` fills them in."""

    BAD_NAME = "bad_name"  # `detail` says why (NFR-COMPAT-02)
    NAME_TAKEN = "name_taken"
    GONE = "gone"
    IN_USE = "in_use"
    FAILED = "failed"
    INTO_ITSELF = "into_itself"
    OUTSIDE = "outside"
    COPY_EXISTS = "copy_exists"
    CHANGED = "changed"  # the item isn't the one the list showed any more
    SOURCE_GONE = "source_gone"
    SOURCE_LINK = "source_link"
    DUPLICATE_SOURCE = "duplicate_source"
    NOT_A_FOLDER = "not_a_folder"
    NOT_A_FILE = "not_a_file"
    NO_FREE_NAME = "no_free_name"
    FOLDER_GONE = "folder_gone"
    NETWORK_SOURCE = "network_source"


class FileActionError(InvalidInputError):
    def __init__(self, problem: FileProblem, name: str = "", detail: str = "") -> None:
        super().__init__(problem.value)  # for logs; the user's words come from messages.py
        self.problem, self.name, self.detail = problem, name, detail


class TransferCancelledError(Exception):
    """The caller asked the transfer to stop. A partial download is kept to resume from."""


class SourceChangedError(Exception):
    """The file changed while it was being uploaded; hash it again and start over."""


class RemoteContentChangedError(Exception):
    """The server's content isn't the version asked for any more; fetch the changes first."""


class ContentMismatchError(Exception):
    """A download didn't match its SHA-256 even after a fresh attempt (NFR-REL-02)."""
