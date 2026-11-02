"""Client releases (FR-UPD-02, FR-UPD-05, OQ-49), published and withdrawn via the admin CLI.

Only an installer whose Authenticode signature verifies, by the configured publisher with one of
the pinned certificates, and whose version resource is the version given, is published; the
check runs where the CLI runs (the Windows server, OQ-48). The installer is
streamed into storage and its SHA-256, measured while storing, must match the one given. A
failure after the blob is stored leaves it unreferenced for gc-blobs.
"""

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.errors import AppError
from dropshare_server.models import ClientRelease, User
from dropshare_server.repositories import releases as release_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.blobs import delete_blobs
from dropshare_server.services.content import Content, resolve_range
from dropshare_server.services.context import CallContext
from dropshare_server.storage.base import StorageBackend, new_blob_key
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.releases import (
    RELEASE_NOTES_MAX_LENGTH,
    ReleaseChannel,
    ReleaseOut,
    ReleasePlatform,
)
from dropshare_shared.signing import (
    ExpectedSigner,
    SignatureError,
    SignatureVerifier,
    check_installer,
)
from dropshare_shared.version import Version, parse_version

logger = logging.getLogger(__name__)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_READ_SIZE = 1024 * 1024


@dataclass(frozen=True)
class NewRelease:
    platform: ReleasePlatform
    channel: ReleaseChannel
    version: str
    min_supported_version: str
    notes: str
    sha256: str  # hex, as published next to the installer


@dataclass(frozen=True)
class ReleaseListing:
    platform: ReleasePlatform
    channel: ReleaseChannel
    version: str
    min_supported_version: str
    size_bytes: int
    published_at: datetime
    withdrawn_at: datetime | None


def to_out(release: ClientRelease) -> ReleaseOut:
    return ReleaseOut(
        id=release.id,
        version=release.version,
        channel=release.channel,
        min_supported_version=release.min_supported_version,
        notes=release.notes or "",
        size_bytes=release.size_bytes,
        sha256=release.sha256,
        published_at=release.published_at,
    )


def _invalid(message: str) -> AppError:
    return AppError(400, ErrorCode.VALIDATION_ERROR, message)


def _version(text: str) -> Version:
    try:
        return parse_version(text)
    except ValueError as exc:
        raise _invalid(f"'{text}' isn't a version like 1.2.3.") from exc


def _target(platform: ReleasePlatform, channel: ReleaseChannel, version: str) -> AuditTarget:
    name = f"{platform.value} {channel.value} {version}"
    return AuditTarget(type="release", id=name, name=name)


def publish(
    session: Session,
    storage: StorageBackend,
    verifier: SignatureVerifier,
    expected: ExpectedSigner,
    ctx: CallContext,
    release: NewRelease,
    installer: Path,
) -> ReleaseOut:
    version = _version(release.version)
    if _version(release.min_supported_version) > version:
        raise _invalid("The minimum supported version can't be above the release's version.")
    sha256 = release.sha256.strip().lower()
    if not _SHA256.fullmatch(sha256):
        raise _invalid("The checksum must be a SHA-256 in hex (64 characters).")
    if len(release.notes) > RELEASE_NOTES_MAX_LENGTH:
        raise _invalid(f"Release notes can be at most {RELEASE_NOTES_MAX_LENGTH} characters.")
    highest = release_repository.highest_version(session, release.platform, release.channel)
    if highest is not None and version <= highest:
        raise _invalid(
            f"{release.channel.value} already has version {'.'.join(map(str, highest))}; "
            "a new release needs a higher version."
        )
    try:
        signer = check_installer(verifier, installer, expected, release.version.strip())
    except SignatureError as exc:
        raise _invalid(f"The installer isn't signed as required: {exc}") from exc

    blob_key = new_blob_key()
    with installer.open("rb") as source:
        stored = storage.write_blob(blob_key, _pieces(source.read))
    if stored.sha256 != sha256:
        delete_blobs(storage, [blob_key])
        raise AppError(
            400,
            ErrorCode.FILE_HASH_MISMATCH,
            "The installer's SHA-256 doesn't match the checksum given; nothing was published.",
        )

    row = ClientRelease(
        platform=release.platform,
        channel=release.channel,
        version=release.version.strip(),
        version_major=version[0],
        version_minor=version[1],
        version_patch=version[2],
        min_supported_version=release.min_supported_version.strip(),
        notes=release.notes or None,
        blob_key=blob_key,
        size_bytes=stored.size_bytes,
        sha256=sha256,
        published_at=datetime.now(UTC),
    )
    release_repository.add(session, row)
    audit_service.record(
        session,
        ctx,
        AuditAction.RELEASE_PUBLISHED,
        target=_target(row.platform, row.channel, row.version),
        details={
            "min_supported_version": row.min_supported_version,
            "size_bytes": row.size_bytes,
            "sha256": sha256,
            "signer": signer.name,
            "signer_sha256": signer.sha256,
        },
    )
    return to_out(row)


def _pieces(read: Callable[[int], bytes]) -> Iterator[bytes]:
    while piece := read(_READ_SIZE):
        yield piece


def withdraw(
    session: Session,
    ctx: CallContext,
    platform: ReleasePlatform,
    channel: ReleaseChannel,
    version: str,
) -> ReleaseOut:
    """Clients are no longer offered it. The row and its installer stay, for the record."""
    release = release_repository.get_by_version(session, platform, channel, version.strip())
    if release is None:
        raise AppError(
            404, ErrorCode.RELEASE_NOT_FOUND, f"No {channel.value} release {version} to withdraw."
        )
    if release.withdrawn_at is not None:
        raise _invalid(f"{channel.value} release {version} is already withdrawn.")
    release.withdrawn_at = datetime.now(UTC)
    audit_service.record(
        session,
        ctx,
        AuditAction.RELEASE_WITHDRAWN,
        target=_target(platform, channel, release.version),
    )
    return to_out(release)


def latest_for(session: Session, user: User, platform: ReleasePlatform) -> ReleaseOut:
    """The release this user is offered: beta users get the newer of beta and stable (OQ-49)."""
    channels = [ReleaseChannel.STABLE]
    if user.update_channel is ReleaseChannel.BETA:
        channels.append(ReleaseChannel.BETA)
    release = release_repository.latest(session, platform, channels)
    if release is None:
        raise _not_found()
    return to_out(release)


def open_installer(
    session: Session,
    storage: StorageBackend,
    release_id: uuid.UUID,
    byte_range: tuple[int | None, int | None] | None,
) -> Content:
    """Any signed-in user may download any release not withdrawn: installers aren't secret,
    and an unsupported client must still reach its update (OQ-49)."""
    release = release_repository.get(session, release_id)
    if release is None or release.withdrawn_at is not None:
        raise _not_found()
    size = release.size_bytes
    start, end = (0, size - 1) if byte_range is None else resolve_range(byte_range, size)
    return Content(
        name=f"DropShare-{release.version}-setup.exe",
        size_bytes=size,
        sha256=release.sha256,
        start=start,
        end=end,
        partial=byte_range is not None,
        chunks=storage.open_range(release.blob_key, start, end) if size else iter(()),
    )


def _not_found() -> AppError:
    return AppError(404, ErrorCode.RELEASE_NOT_FOUND, "No such release is available.")


class MinimumVersion:
    """The effective minimum client version (FR-UPD-04, OQ-49): the higher of
    MIN_CLIENT_VERSION and the minimum of each platform's latest stable release. Read from the
    database at most once a minute per process; while it can't be read, the last value holds."""

    def __init__(
        self,
        configured: str,
        session_factory: sessionmaker[Session],
        max_age_seconds: float = 60,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._configured = configured
        self._sessions = session_factory
        self._max_age = max_age_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._value = configured
        self._read_at: float | None = None

    def current(self) -> str:
        """One request reads the database when the value is due; the others meanwhile get the
        last value without waiting, so a slow database never holds up every request."""
        with self._lock:
            now = self._clock()
            due = self._read_at is None or now - self._read_at >= self._max_age
            if due:
                self._read_at = now  # claimed: nobody else reads until it's due again
            value = self._value
        if not due:
            return value
        try:
            value = self._read()
        except SQLAlchemyError:
            logger.warning("Couldn't read release minimums; keeping %s", value)
            return value
        with self._lock:
            self._value = value
        return value

    def _read(self) -> str:
        with self._sessions() as session:
            minimums = [
                release_repository.latest_minimum(session, platform, ReleaseChannel.STABLE)
                for platform in ReleasePlatform
            ]
        return max(
            [self._configured, *(minimum for minimum in minimums if minimum is not None)],
            key=parse_version,
        )


def list_releases(session: Session) -> list[ReleaseListing]:
    return [ReleaseListing(*row) for row in release_repository.list_all(session)]
