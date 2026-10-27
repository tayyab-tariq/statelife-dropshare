"""User-facing text for every failure, in one place (architecture.md §4, NFR-UX-03)."""

import math

from pydantic import ValidationError

from dropshare_client.platform.base import CredentialStoreError
from dropshare_client.services.errors import (
    ApiError,
    InvalidInputError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import ValidationIssue
from dropshare_shared.schemas.error_details import AccountLockedDetails

OFFLINE = "Can't reach the DropShare server. Check your internet connection, then try again."
DISABLED = "Your account is disabled. Contact your administrator."
INVALID_CREDENTIALS = "Invalid username or password."
SESSION_ENDED = "Your session has ended. Sign in again."
UPGRADE = "This version of DropShare is no longer supported. Install the latest version."
CREDENTIAL_STORE = "DropShare couldn't use the Windows credential store. Try again."

_BY_CODE: dict[ErrorCode | str, str] = {
    ErrorCode.UNAUTHENTICATED: SESSION_ENDED,  # sign-in has its own wording: see below
    ErrorCode.ACCOUNT_DISABLED: DISABLED,
    ErrorCode.RATE_LIMITED: "Too many attempts. Wait a moment, then try again.",
    ErrorCode.PASSWORD_CHANGE_REQUIRED: "Choose a new password to continue.",
}


def message_for(exc: BaseException) -> str:
    """What to tell the user about `exc`: what happened and what to do next."""
    if isinstance(exc, OfflineError):
        return OFFLINE
    if isinstance(exc, UpgradeRequiredError):
        return UPGRADE
    if isinstance(exc, SessionExpiredError):
        return DISABLED if exc.code == ErrorCode.ACCOUNT_DISABLED else SESSION_ENDED
    if isinstance(exc, ApiError):
        return _api_message(exc)
    if isinstance(exc, InvalidInputError):
        return str(exc)
    if isinstance(exc, CredentialStoreError):
        return CREDENTIAL_STORE
    if isinstance(exc, OSError):
        return folder_message_for(exc)
    return "Something went wrong. Try again, and if it keeps happening, export diagnostics."


def sign_in_message_for(exc: BaseException) -> str:
    """At sign-in a 401 means the credentials were wrong (FR-AUTH-01: never which part)."""
    if isinstance(exc, ApiError) and exc.code == ErrorCode.UNAUTHENTICATED:
        return INVALID_CREDENTIALS
    return message_for(exc)


def folder_message_for(exc: OSError) -> str:
    """A file-system failure in words, without raw error codes or paths."""
    if isinstance(exc, PermissionError):
        return "DropShare doesn't have permission to use that folder. Choose another folder."
    if isinstance(exc, FileNotFoundError):
        return "That folder or drive isn't available. Check it's connected, then try again."
    if isinstance(exc, FileExistsError | NotADirectoryError):
        return "There's a file in the way of that folder. Choose another folder."
    return "DropShare couldn't use that folder. Choose another folder, or try again."


def _api_message(exc: ApiError) -> str:
    if exc.code == ErrorCode.ACCOUNT_LOCKED:
        return _locked_message(exc.details)
    if exc.code == ErrorCode.VALIDATION_ERROR and isinstance(exc.details, list) and exc.details:
        try:
            return ValidationIssue.model_validate(exc.details[0]).msg
        except ValidationError:
            return exc.message
    if exc.code in _BY_CODE:
        return _BY_CODE[exc.code]
    if exc.status >= 500:
        reference = f" (reference {exc.request_id})" if exc.request_id else ""
        return f"Something went wrong on the server. Try again later{reference}."
    return exc.message


def _locked_message(details: object) -> str:
    try:
        seconds = AccountLockedDetails.model_validate(details).retry_after_seconds
    except ValidationError:
        return "Too many failed sign-in attempts. Try again later."
    minutes = max(1, math.ceil(seconds / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"Too many failed sign-in attempts. Try again in {minutes} {unit}."


# --- syncing ---------------------------------------------------------------------------------

SYNC_FOLDER_MISSING = (
    "The sync folder can't be found. Reconnect its drive, or sign out and choose a new folder."
)
NO_PERSONAL_FOLDER = "Your account has no DropShare storage yet. Contact your administrator."
SYNC_PROBLEM = "Syncing ran into a problem and will try again shortly."
OTHER_ACCOUNT_FOLDER = (
    "The sync folder holds files from another DropShare account. Choose a new, empty folder."
)


def _files(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def items_left(count: int) -> str:
    """FR-CLIENT-01 "Syncing (n items)"; empty once nothing is left."""
    if count <= 0:
        return ""
    return "1 item" if count == 1 else f"{count} items"


def conflicts_message(count: int) -> str:
    """Several conflicts at once, as one notification (ui.md §7)."""
    return (
        f"{count} files were changed on this computer and on the server. The server's "
        "versions kept their names; yours were saved as conflicted copies next to them."
    )


def conflict_message(path: str, copy_path: str) -> str:
    """FR-CONF-01: which file, and where the user's own version went."""
    name = path.rpartition("/")[2]
    copy_name = copy_path.rpartition("/")[2]
    return (
        f'"{name}" was changed on this computer and on the server. The server\'s version '
        f'kept the name; yours was saved as "{copy_name}".'
    )


def mass_delete_message(here: int, on_server: int) -> str:
    """FR-SYNC-08: what would be deleted where, and where it would go."""
    parts = []
    if here:
        parts.append(
            f"{_files(here)} deleted on the server would be removed from this computer "
            "(to the Recycle Bin)."
        )
    if on_server:
        parts.append(
            f"{_files(on_server)} deleted on this computer would be removed from the server "
            "(to your DropShare trash)."
        )
    return " ".join(parts) + " Delete them, or pause syncing to check first?"
