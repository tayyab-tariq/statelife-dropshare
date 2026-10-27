"""Command line (OQ-13 profiles) and secret redaction in logs (architecture.md §5)."""

import logging
import sys

import pytest

from dropshare_client.app import RedactingFormatter, parse_args


def test_profile_and_background_flags() -> None:
    args = parse_args(["--profile", "bob", "--background"])

    assert (args.profile, args.background) == ("bob", True)


@pytest.mark.parametrize("profile", ["", "a b", "../x", "x" * 33])
def test_invalid_profile_names_are_refused(profile: str) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--profile", profile])


def test_profiles_are_refused_in_shipped_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    with pytest.raises(SystemExit):
        parse_args(["--profile", "bob"])


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("token eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl here", "eyJhbGciOi.eyJzdWIiOi.c2lnbmF0dXJl"),
        ('body {"refresh_token": "r-123", "x": 1}', "r-123"),
        ('{"current_password": "hunter2-hunter2"}', "hunter2-hunter2"),
    ],
)
def test_secrets_are_redacted_from_log_lines(message: str, secret: str) -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "%s", (message,), None)

    line = RedactingFormatter("%(message)s").format(record)

    assert secret not in line
    assert "[redacted]" in line


def test_secrets_in_tracebacks_are_redacted_too() -> None:
    try:
        raise ValueError('rejected {"password": "S3cretPassw0rd"} and Bearer tok.en.value')
    except ValueError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "Unexpected failure", None, sys.exc_info()
        )

    line = RedactingFormatter("%(message)s").format(record)

    assert "Traceback" in line
    assert "S3cretPassw0rd" not in line and "tok.en.value" not in line
