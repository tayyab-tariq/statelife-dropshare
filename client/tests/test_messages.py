"""Every failure maps to a message that says what happened and what to do (NFR-UX-03)."""

import pytest

from dropshare_client.controllers import messages
from dropshare_client.platform.base import CredentialStoreError
from dropshare_client.services.errors import (
    ApiError,
    InvalidInputError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_shared.errors import ErrorCode


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OfflineError(), messages.OFFLINE),
        (UpgradeRequiredError("2.0.0"), messages.UPGRADE),
        (SessionExpiredError(), messages.SESSION_ENDED),
        (SessionExpiredError(ErrorCode.ACCOUNT_DISABLED), messages.DISABLED),
        (ApiError(401, ErrorCode.UNAUTHENTICATED, "x"), messages.SESSION_ENDED),
        (ApiError(403, ErrorCode.ACCOUNT_DISABLED, "x"), messages.DISABLED),
        (
            ApiError(
                423,
                ErrorCode.ACCOUNT_LOCKED,
                "x",
                {"locked_until": "2026-10-26T10:00:00Z", "retry_after_seconds": 61},
            ),
            "Too many failed sign-in attempts. Try again in 2 minutes.",
        ),
        (
            ApiError(
                400,
                ErrorCode.VALIDATION_ERROR,
                "x",
                [{"loc": ["body"], "msg": "Too short.", "type": "t"}],
            ),
            "Too short.",
        ),
        (ApiError(409, "SOMETHING_NEW", "Server's own words."), "Server's own words."),
        (
            ApiError(500, ErrorCode.INTERNAL_ERROR, "x", request_id="abc"),
            "Something went wrong on the server. Try again later (reference abc).",
        ),
        (CredentialStoreError("keychain locked"), messages.CREDENTIAL_STORE),
        (
            PermissionError(13, "Access is denied", "C:\\x"),
            "DropShare doesn't have permission to use that folder. Choose another folder.",
        ),
        (
            FileNotFoundError(2, "No such file", "E:\\x"),
            "That folder or drive isn't available. Check it's connected, then try again.",
        ),
        (
            InvalidInputError("Check your username or email and password."),
            "Check your username or email and password.",
        ),
    ],
)
def test_message_for(exc: BaseException, expected: str) -> None:
    assert messages.message_for(exc) == expected


def test_only_sign_in_says_the_credentials_were_wrong() -> None:
    unauthenticated = ApiError(401, ErrorCode.UNAUTHENTICATED, "x")

    assert messages.sign_in_message_for(unauthenticated) == messages.INVALID_CREDENTIALS
    assert messages.message_for(unauthenticated) == messages.SESSION_ENDED


def test_raw_os_error_text_never_reaches_the_user() -> None:
    message = messages.message_for(OSError(5, "[WinError 5] Access is denied", "C:\\secret"))

    assert "WinError" not in message and "secret" not in message
