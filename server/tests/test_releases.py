"""Publishing and withdrawing client releases (FR-UPD-02, FR-UPD-05, OQ-49) at the service level;
the CLI and the endpoints are covered with them."""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.errors import AppError
from dropshare_server.models import ClientRelease
from dropshare_server.services import blobs as blob_service
from dropshare_server.services import releases as release_service
from dropshare_server.services import users as user_service
from dropshare_server.services.context import cli_context
from dropshare_server.services.releases import NewRelease
from dropshare_server.storage.local import LocalFilesystemBackend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.releases import ReleaseChannel, ReleasePlatform
from dropshare_shared.signing import ExpectedSigner, SignatureError, Signer

from .conftest import AuditEvents

PUBLISHER = "StateLife Test"
CERT = "ab" * 32
EXPECTED = ExpectedSigner.of(PUBLISHER, [CERT])
INSTALLER = b"MZ installer bytes"


class _Verifier:
    """Signed as given; its version resource is whatever is being published unless `version`
    says otherwise."""

    def __init__(
        self, signer: str | None = PUBLISHER, cert: str = CERT, version: str | None = None
    ) -> None:
        self._signer = signer
        self._cert = cert
        self._version = version
        self.publishing = ""

    def signer(self, path: Path) -> Signer:
        if self._signer is None:
            raise SignatureError("not signed")
        return Signer(self._signer, self._cert)

    def file_version(self, path: Path) -> str:
        return self._version or self.publishing


type Publish = Callable[..., object]


@pytest.fixture
def storage(storage_root: Path) -> LocalFilesystemBackend:
    return LocalFilesystemBackend(storage_root)


@pytest.fixture
def installer(tmp_path: Path) -> Path:
    path = tmp_path / "DropShare-setup.exe"
    path.write_bytes(INSTALLER)
    return path


@pytest.fixture
def publish(
    session_factory: sessionmaker[Session], storage: LocalFilesystemBackend, installer: Path
) -> Publish:
    def run(
        version: str = "1.2.0",
        *,
        channel: ReleaseChannel = ReleaseChannel.STABLE,
        minimum: str = "1.0.0",
        sha256: str = hashlib.sha256(INSTALLER).hexdigest(),
        verifier: _Verifier | None = None,
        expected: ExpectedSigner = EXPECTED,
    ) -> object:
        checks = verifier or _Verifier()
        checks.publishing = version
        release = NewRelease(
            platform=ReleasePlatform.WINDOWS_X64,
            channel=channel,
            version=version,
            min_supported_version=minimum,
            notes="Fixes",
            sha256=sha256,
        )
        with session_factory.begin() as session:
            return release_service.publish(
                session,
                storage,
                checks,
                expected,
                cli_context("it-admin"),
                release,
                installer,
            )

    return run


def _rows(session_factory: sessionmaker[Session]) -> list[ClientRelease]:
    with session_factory() as session:
        return list(session.scalars(select(ClientRelease)))


def _code(exc: pytest.ExceptionInfo[AppError]) -> ErrorCode:
    return exc.value.code


def test_a_signed_installer_is_stored_and_audited(
    publish: Publish,
    session_factory: sessionmaker[Session],
    storage: LocalFilesystemBackend,
    audit_events: AuditEvents,
) -> None:
    published = publish()

    (row,) = _rows(session_factory)
    assert (row.version, row.version_major, row.version_minor, row.version_patch) == (
        "1.2.0",
        1,
        2,
        0,
    )
    assert row.size_bytes == len(INSTALLER)
    assert b"".join(storage.open_range(row.blob_key, 0, row.size_bytes - 1)) == INSTALLER
    assert published == release_service.to_out(row)
    (event,) = audit_events(AuditAction.RELEASE_PUBLISHED)
    assert event.target_name == "windows-x64 stable 1.2.0"
    assert event.details is not None and PUBLISHER in event.details and CERT in event.details


@pytest.mark.parametrize("version", ["1.2.0", "1.1.9", "0.9.0"])
def test_a_new_release_needs_a_higher_version_on_its_channel(
    publish: Publish, session_factory: sessionmaker[Session], version: str
) -> None:
    publish("1.2.0")

    with pytest.raises(AppError) as exc:
        publish(version)

    assert _code(exc) is ErrorCode.VALIDATION_ERROR
    assert len(_rows(session_factory)) == 1


def test_versions_compare_as_numbers_and_channels_are_separate(
    publish: Publish, session_factory: sessionmaker[Session]
) -> None:
    publish("1.9.0")
    publish("1.10.0")  # string order would call this lower
    publish("1.10.0", channel=ReleaseChannel.BETA)  # a beta later published as stable, or back

    assert len(_rows(session_factory)) == 3


def test_a_withdrawn_version_is_never_reused(
    publish: Publish, session_factory: sessionmaker[Session]
) -> None:
    publish("1.2.0")
    with session_factory.begin() as session:
        release_service.withdraw(
            session,
            cli_context("it-admin"),
            ReleasePlatform.WINDOWS_X64,
            ReleaseChannel.STABLE,
            "1.2.0",
        )

    with pytest.raises(AppError):
        publish("1.2.0")
    publish("1.2.1")


@pytest.mark.parametrize(
    ("version", "minimum", "sha256"),
    [
        ("1.2.0", "1.3.0", hashlib.sha256(INSTALLER).hexdigest()),  # minimum above the release
        ("1.2.0-beta.1", "1.0.0", hashlib.sha256(INSTALLER).hexdigest()),
        ("1.2.0", "1.0.0", "not-a-hash"),
    ],
)
def test_bad_release_details_are_refused_before_storing(
    publish: Publish, storage: LocalFilesystemBackend, version: str, minimum: str, sha256: str
) -> None:
    with pytest.raises(AppError) as exc:
        publish(version, minimum=minimum, sha256=sha256)

    assert _code(exc) is ErrorCode.VALIDATION_ERROR
    assert list(storage.blobs()) == []


@pytest.mark.parametrize(
    ("verifier", "expected"),
    [
        (_Verifier(signer=None), EXPECTED),  # unsigned, tampered, or untrusted
        (_Verifier(signer="Someone Else"), EXPECTED),
        (_Verifier(cert="cd" * 32), EXPECTED),  # same name, a certificate that isn't pinned
        (_Verifier(version="1.1.0"), EXPECTED),  # an older installer published as 1.2.0
        (_Verifier(), ExpectedSigner.of("", [CERT])),  # RELEASE_PUBLISHER not configured
        (_Verifier(), ExpectedSigner.of(PUBLISHER, [])),  # RELEASE_CERT_SHA256 not configured
    ],
)
def test_only_installers_signed_by_the_publisher_are_published(
    publish: Publish,
    session_factory: sessionmaker[Session],
    storage: LocalFilesystemBackend,
    verifier: _Verifier,
    expected: ExpectedSigner,
) -> None:
    with pytest.raises(AppError) as exc:
        publish(verifier=verifier, expected=expected)

    assert _code(exc) is ErrorCode.VALIDATION_ERROR
    assert _rows(session_factory) == []
    assert list(storage.blobs()) == []


def test_a_checksum_mismatch_publishes_nothing_and_keeps_no_blob(
    publish: Publish, session_factory: sessionmaker[Session], storage: LocalFilesystemBackend
) -> None:
    with pytest.raises(AppError) as exc:
        publish(sha256=hashlib.sha256(b"something else").hexdigest())

    assert _code(exc) is ErrorCode.FILE_HASH_MISMATCH
    assert _rows(session_factory) == []
    assert list(storage.blobs()) == []


def test_withdrawing_is_audited_once_and_unknown_versions_are_not_found(
    publish: Publish, session_factory: sessionmaker[Session], audit_events: AuditEvents
) -> None:
    publish("1.2.0")
    ctx = cli_context("it-admin")
    platform, stable = ReleasePlatform.WINDOWS_X64, ReleaseChannel.STABLE

    with session_factory.begin() as session:
        release_service.withdraw(session, ctx, platform, stable, "1.2.0")
    with pytest.raises(AppError) as again, session_factory.begin() as session:
        release_service.withdraw(session, ctx, platform, stable, "1.2.0")
    with pytest.raises(AppError) as unknown, session_factory.begin() as session:
        release_service.withdraw(session, ctx, platform, ReleaseChannel.BETA, "1.2.0")

    assert _code(again) is ErrorCode.VALIDATION_ERROR
    assert (unknown.value.status_code, _code(unknown)) == (404, ErrorCode.RELEASE_NOT_FOUND)
    (row,) = _rows(session_factory)
    assert row.withdrawn_at is not None
    assert len(audit_events(AuditAction.RELEASE_WITHDRAWN)) == 1
    with session_factory() as session:
        (listing,) = release_service.list_releases(session)
    assert (listing.version, listing.withdrawn_at) == ("1.2.0", row.withdrawn_at)


def test_gc_blobs_keeps_installers(
    publish: Publish, session_factory: sessionmaker[Session], storage: LocalFilesystemBackend
) -> None:
    publish()
    long_after = datetime.now(UTC) + timedelta(days=365)

    result = blob_service.collect(session_factory, storage, long_after, grace_hours=24)

    assert result.orphans == 0
    assert len(list(storage.blobs())) == 1


def test_a_users_channel_is_set_and_audited(
    make_user: Callable[..., tuple[str, str]],
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    make_user("alice")

    with session_factory.begin() as session:
        user = user_service.set_update_channel(
            session, cli_context("it-admin"), "alice", ReleaseChannel.BETA
        )

    assert user.update_channel is ReleaseChannel.BETA
    (event,) = audit_events(AuditAction.USER_CHANNEL_CHANGED)
    assert event.details is not None and '"beta"' in event.details
