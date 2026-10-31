"""User-facing text for every failure, in one place (architecture.md §4, NFR-UX-03)."""

import math

from pydantic import ValidationError

from dropshare_client.platform.base import CredentialStoreError
from dropshare_client.services.errors import (
    ApiError,
    FileActionError,
    FileProblem,
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
    if isinstance(exc, FileActionError):
        return file_problem(exc)
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
MY_FILES = "My files"
# File actions (FR-FILE-04): they act on this computer, then sync.
FOLDER_CREATED = "Folder created. It appears here once it has synced."
RENAMED = "Renamed. The change syncs in a moment."
COPY_SAVED = "Copy saved."
NO_SYNC_FOLDER_YET = "Choose a sync folder first (Overview)."
NOT_HERE_YET = "It isn't on this computer yet. Wait until it has synced, then try again."
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


_UNITS = ("KB", "MB", "GB", "TB")


def size_text(size: int) -> str:
    """1,000-based (KB = 1,000 bytes, as on the server and in quotas), one decimal from KB up.
    Explorer counts in 1,024s, so its numbers can be a little smaller for the same file."""
    if size < 1000:
        return "1 byte" if size == 1 else f"{size} bytes"
    value = float(size)
    for unit in _UNITS:
        value /= 1000
        if value < 1000 or unit == _UNITS[-1]:
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")


def not_enough_space(needed: int, available: int, *, in_dropshare: bool) -> str:
    """FR-FILE-02: nothing is copied, and the message says how much space is needed."""
    if in_dropshare:
        where = "left in your DropShare storage"
        fix = "Ask your administrator for more space."
    else:
        where = "free on this computer"
        fix = "Free up space on this computer."
    return (
        f"This needs {size_text(needed)}, but there's only {size_text(available)} {where}. "
        f"{fix} Nothing was copied."
    )


UPLOAD_CANCELLED = "Upload cancelled. Everything copied so far was removed."
UPLOAD_ALREADY_RUNNING = "Another upload is still running. Wait for it to finish, then try again."
ACTION_ALREADY_RUNNING = "DropShare is still doing the last action. Try again in a moment."
FOLDER_TRASHED = "That folder isn't in DropShare any more (it may be in the trash)."

# File actions and uploads from the app (FR-FILE-02/03/04): what went wrong, what to do.
_FILE_PROBLEMS: dict[FileProblem, str] = {
    FileProblem.BAD_NAME: "“{name}” can't be used: {detail}",
    FileProblem.NAME_TAKEN: "There's already an item with this name here. Choose another name.",
    FileProblem.GONE: "It isn't in your DropShare folder any more. Refresh, then try again.",
    FileProblem.IN_USE: "It's open in another program. Close it, then try again.",
    FileProblem.FAILED: "That didn't work. Try again, and if it keeps happening, export "
    "diagnostics.",
    FileProblem.INTO_ITSELF: "“{name}” can't go into itself. Choose another folder.",
    FileProblem.OUTSIDE: "Choose a folder inside your DropShare folder.",
    FileProblem.COPY_EXISTS: "There's already an item called “{name}” there. Choose another place.",
    FileProblem.CHANGED: "This item changed since the list was shown. The list has been "
    "refreshed; try again.",
    FileProblem.SOURCE_GONE: "“{name}” can't be found. Choose it again.",
    FileProblem.SOURCE_LINK: "“{name}” is a shortcut or link. Choose the real file or folder.",
    FileProblem.DUPLICATE_SOURCE: "Two of the items you chose are called “{name}”. Upload them "
    "one at a time.",
    FileProblem.NOT_A_FOLDER: "“{name}” is a file here, not a folder. Choose Keep both.",
    FileProblem.NOT_A_FILE: "“{name}” is a folder here, not a file. Choose Keep both.",
    FileProblem.NO_FREE_NAME: "There's no free name for “{name}” here. Rename it first.",
    FileProblem.FOLDER_GONE: "That folder isn't on this computer any more. Refresh, then try "
    "again.",
    FileProblem.NETWORK_SOURCE: "Files from a network location can't be dropped here. Copy them "
    "to this computer first, or use Upload.",
}


def file_problem(exc: FileActionError) -> str:
    return _FILE_PROBLEMS[exc.problem].format(name=exc.name, detail=exc.detail)


def upload_taken_back(*, cancelled: bool, kept: int) -> str:
    """OQ-28: what a stopped upload left behind, so nothing is a surprise."""
    text = UPLOAD_CANCELLED if cancelled else "The upload stopped and was taken back."
    if kept:
        items = "1 item" if kept == 1 else f"{kept} items"
        text = (
            f"{text} {items} changed meanwhile or couldn't be put back, so they were left as "
            "they are; check the folder."
        )
    return text


def kept_aside(count: int) -> str:
    """Replaced files the Recycle Bin refused (they stay in DropShare's hidden folder)."""
    items = "1 replaced file" if count == 1 else f"{count} replaced files"
    return (
        f"The Recycle Bin didn't take {items}, so they're kept in the hidden .dropshare-tmp "
        "folder inside your DropShare folder."
    )


def uploaded(copied: int, skipped: int) -> str:
    """FR-FILE-02: copied into the sync folder; the sync engine uploads them."""
    items = "1 item" if copied == 1 else f"{copied} items"
    text = f"Added {items}. They upload in a moment; see Transfers." if copied else ""
    if skipped:
        left = "1 item was" if skipped == 1 else f"{skipped} items were"
        text = f"{text} {left} skipped.".strip()
    return text or "Nothing was added."


def moved(count: int) -> str:
    items = "1 item" if count == 1 else f"{count} items"
    return f"Moved {items}. The change syncs in a moment."


def recycled(count: int) -> str:
    items = "1 item" if count == 1 else f"{count} items"
    return (
        f"Moved {items} to the Recycle Bin. They go to the DropShare trash too, where you can "
        "restore them."
    )


def transfers_summary(count: int, percent: int) -> str:
    """FR-CLIENT-08: "12 items · 45%"."""
    items = "1 item" if count == 1 else f"{count} items"
    return f"{items} \N{MIDDLE DOT} {percent}%"


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


# --- sharing (FR-SHARE-01 … FR-SHARE-07) ------------------------------------------------------


def group_size(count: int) -> str:
    return "1 person" if count == 1 else f"{count} people"


def shared_with(name: str) -> str:
    return f"Shared with {name}."


def role_changed(name: str) -> str:
    return f"Changed what {name} can do."


def access_removed(name: str) -> str:
    return f"{name} no longer has access."


LINK_COPIED = "Link copied. Only people with access can open it."
ADDED_TO_DROPSHARE = "Added to your DropShare. It syncs in a moment."
LEFT_SHARED_FOLDER = "Removed from your DropShare. Others still have it."
NEW_VERSION_UPLOADED = "New version uploaded."
TRANSFER_CANCELLED = "Cancelled. Nothing was changed."


def saving_copy(name: str) -> str:
    return f"Saving a copy of “{name}”…"


def uploading_version(name: str) -> str:
    return f"Uploading a new version of “{name}”…"


def leave_folder(name: str) -> str:
    return (
        f"“{name}” is removed from your DropShare and from your computers. Files you changed "
        "and haven't uploaded yet are kept in your own folder. You can add it again from here."
    )


COPY_EXISTS_THERE = "There's already a file with that name there. Choose another name."
# FR-SHARE-07 has two messages, but the server answers 404 for both cases alike.
LINK_UNAVAILABLE = (
    "You don't have access to this item, or it is no longer available. Ask the owner to "
    "share it with you."
)
SHARED_FOLDER = "Shared folder"


def new_shares(names: list[str]) -> str:
    if len(names) == 1:
        return f"“{names[0]}” was shared with you."
    return f"{len(names)} items were shared with you."


# --- trash (FR-TRASH-01 … 03) -------------------------------------------------------------

TRASH = "Trash"
TRASH_EMPTY = (
    "Nothing in the trash. What you delete is kept here for a while, then deleted for good."
)
DELETE_FOREVER = "Delete permanently…"
ONLY_OWNER_DELETES_SELECTED = (
    "Only the owner can delete these permanently. You can restore what you deleted."
)
NOT_IN_TRASH = "It isn't in the trash any more: it was restored or deleted by someone else."
ONLY_OWNER_DELETES = "Only the owner can delete this permanently."


def restored(count: int) -> str:
    if count == 1:
        return "Restored. It's back where it was and syncs to your computers in a moment."
    return (
        f"{count} items restored. They're back where they were and sync to your computers in "
        "a moment."
    )


def restored_as(name: str | None) -> str:
    return f"Restored as “{name}”: its name was taken. It syncs to your computers in a moment."


def restoring(position: int, total: int) -> str:
    return f"Restoring {position} of {total}…"


def deleting_forever(position: int, total: int) -> str:
    return f"Deleting {position} of {total} permanently…"


def emptying(removed: int) -> str:
    return f"Emptying the trash… {removed} deleted so far."


def trash_action_stopped(done: int) -> str:
    return f"Stopped. {done} done before that; the rest is still in the trash."


def deleted_forever(count: int) -> str:
    return "Deleted permanently." if count == 1 else f"{count} items deleted permanently."


def trash_emptied(count: int, freed: int) -> str:
    if count == 0:
        return "The trash was already empty."
    return f"Trash emptied. {size_text(freed)} freed."


def confirm_delete_forever(names: list[str], kept: int = 0) -> str:
    """Names up to three items (ui.md §5), and says what stays because the user can't delete it."""
    shown = ", ".join(f"“{name}”" for name in names[:3])
    more = len(names) - 3
    what = shown if more <= 0 else f"{shown} and {more} more"
    text = f"{what} will be deleted permanently. This can't be undone."
    if kept:
        whose = "1 item you don't own stays" if kept == 1 else f"{kept} items you don't own stay"
        text += f" {whose} in the trash."
    return text


CONFIRM_EMPTY_TRASH = (
    "Everything in your trash will be deleted permanently, including what others deleted from "
    "folders you shared. This can't be undone."
)


def some_failed(failed: int, reason: str) -> str:
    what = "1 item" if failed == 1 else f"{failed} items"
    return f"{what} couldn't be done. {reason}"
