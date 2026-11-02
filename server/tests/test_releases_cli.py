"""Release and update-channel commands of the admin CLI (FR-UPD-05, FR-ADMIN-01, OQ-49)."""

import hashlib
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import ClientRelease, User
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome
from dropshare_shared.schemas.releases import ReleaseChannel
from dropshare_shared.signing import SignatureError, Signer

from .conftest import AuditEvents

type Cli = Callable[..., tuple[int, str, str]]

PUBLISHER = "StateLife Test"
CERT = "ab" * 32
INSTALLER = b"MZ installer bytes"


class _Verifier:
    def __init__(self, signer: str | None, version: str) -> None:
        self._signer = signer
        self._version = version

    def signer(self, path: Path) -> Signer:
        if self._signer is None:
            raise SignatureError("No signature was found.")
        return Signer(self._signer, CERT)

    def file_version(self, path: Path) -> str:
        return self._version


@pytest.fixture
def signed_by(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """signed_by(name, version="1.2.0"): what the checks report for any installer (name None:
    unsigned), with RELEASE_PUBLISHER and RELEASE_CERT_SHA256 set to match."""
    monkeypatch.setenv("RELEASE_PUBLISHER", PUBLISHER)
    monkeypatch.setenv("RELEASE_CERT_SHA256", f'["{CERT.upper()}"]')

    def set_signer(name: str | None, version: str = "1.2.0") -> None:
        monkeypatch.setattr(
            "dropshare_server.cli.system_verifier", lambda: _Verifier(name, version)
        )

    set_signer(PUBLISHER)
    return set_signer


@pytest.fixture
def installer(tmp_path: Path) -> Path:
    path = tmp_path / "DropShare-1.2.0-setup.exe"
    path.write_bytes(INSTALLER)
    return path


def _publish(
    cli: Cli, installer: Path, *extra: str, version: str = "1.2.0"
) -> tuple[int, str, str]:
    return cli(
        "releases",
        "publish",
        "--version",
        version,
        "--channel",
        "stable",
        "--min-supported",
        "1.0.0",
        "--installer",
        str(installer),
        "--sha256",
        hashlib.sha256(INSTALLER).hexdigest(),
        *extra,
    )


def _releases(session_factory: sessionmaker[Session]) -> list[ClientRelease]:
    with session_factory() as session:
        return list(session.scalars(select(ClientRelease)))


def test_publish_list_and_withdraw(
    cli: Cli,
    signed_by: Callable[..., None],
    installer: Path,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("Faster uploads.\n", encoding="utf-8")

    code, out, err = _publish(cli, installer, "--notes-file", str(notes))

    assert (code, err) == (0, "")
    assert "Published windows-x64 stable 1.2.0" in out
    (release,) = _releases(session_factory)
    assert release.notes == "Faster uploads."
    (event,) = audit_events(AuditAction.RELEASE_PUBLISHED)
    assert (event.actor_type, event.actor_label) == (AuditActorType.CLI, "it-admin")

    code, out, _ = cli("releases", "list")
    assert code == 0
    assert out.splitlines()[1].split()[:4] == ["windows-x64", "stable", "1.2.0", "1.0.0"]
    assert out.splitlines()[1].endswith("available")

    code, out, _ = cli("releases", "withdraw", "1.2.0", "--channel", "stable")
    assert code == 0
    assert "no longer offered" in out
    assert cli("releases", "list")[1].splitlines()[1].endswith("withdrawn")
    assert len(audit_events(AuditAction.RELEASE_WITHDRAWN)) == 1


@pytest.mark.parametrize("signer", [None, "Someone Else"])
def test_an_installer_not_signed_by_the_publisher_is_refused_and_audited(
    cli: Cli,
    signed_by: Callable[..., None],
    installer: Path,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
    signer: str | None,
) -> None:
    signed_by(signer)

    code, _, err = _publish(cli, installer)

    assert code == 1
    assert "isn't signed as required" in err
    assert _releases(session_factory) == []
    (event,) = audit_events(AuditAction.RELEASE_PUBLISHED)
    assert event.outcome is AuditOutcome.FAILURE
    assert event.target_name == "windows-x64 stable 1.2.0"


def test_an_installer_of_another_version_is_refused(
    cli: Cli, signed_by: Callable[..., None], installer: Path
) -> None:
    signed_by(PUBLISHER, "1.1.0")  # a validly signed older installer, labelled 1.2.0

    code, _, err = _publish(cli, installer)

    assert code == 1
    assert "version 1.1.0, not 1.2.0" in err


def test_a_wrong_checksum_is_refused(
    cli: Cli, signed_by: Callable[..., None], installer: Path
) -> None:
    code, _, err = cli(
        "releases",
        "publish",
        "--version",
        "1.2.0",
        "--channel",
        "beta",
        "--min-supported",
        "1.0.0",
        "--installer",
        str(installer),
        "--sha256",
        hashlib.sha256(b"other").hexdigest(),
    )

    assert code == 1
    assert "doesn't match" in err


def test_notes_that_are_not_utf8_are_refused(
    cli: Cli, signed_by: Callable[..., None], installer: Path, tmp_path: Path
) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_bytes(b"\xff\xfe bad")

    code, _, err = _publish(cli, installer, "--notes-file", str(notes))

    assert code == 1
    assert "UTF-8" in err


def test_a_missing_installer_is_a_usage_error(cli: Cli, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        _publish(cli, tmp_path / "missing.exe")

    assert exc.value.code == 2


def test_withdrawing_an_unknown_release_fails(cli: Cli, audit_events: AuditEvents) -> None:
    code, _, err = cli("releases", "withdraw", "9.9.9", "--channel", "beta")

    assert code == 1
    assert "No beta release 9.9.9" in err
    (event,) = audit_events(AuditAction.RELEASE_WITHDRAWN)
    assert event.outcome is AuditOutcome.FAILURE


def test_set_channel(
    cli: Cli,
    make_user: Callable[..., tuple[str, str]],
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    make_user("alice")

    code, out, _ = cli("users", "set-channel", "alice", "beta")

    assert code == 0
    assert "'alice' is now offered beta releases" in out
    with session_factory() as session:
        user = session.scalars(select(User).where(User.username == "alice")).one()
    assert user.update_channel is ReleaseChannel.BETA
    assert len(audit_events(AuditAction.USER_CHANNEL_CHANGED)) == 1

    code, _, err = cli("users", "set-channel", "nobody", "beta")
    assert code == 1
    assert "nobody" in err
