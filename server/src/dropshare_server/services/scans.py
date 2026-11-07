"""The scan worker (FR-SAFE-01, OQ-55, OQ-58): scans the uploads `complete` left waiting, too large
to scan within a request or sent while the scanner was down, and finishes them.

Each upload is scanned in its own transaction, holding only its own row, so no other worker (and
no `complete`) takes it meanwhile. Nothing becomes visible unscanned, and nothing is lost:

- While the scanner is down, every upload waits, untouched, and the worker pauses longer each
  time it finds it still down.
- One file the scanner fails on (or anything else going wrong with it) is tried again later, on
  its own, while the others carry on. After `scan_max_attempts` failures it is refused as
  unscannable: fail closed, never published unscanned.
"""

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import NoReturn

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError
from dropshare_server.models import UploadSession
from dropshare_server.repositories import devices as device_repository
from dropshare_server.repositories import uploads as upload_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.scanner.base import Scanner, ScannerUnavailableError
from dropshare_server.services import audit as audit_service
from dropshare_server.services import uploads as upload_service
from dropshare_server.services.context import Actor, CallContext
from dropshare_server.storage.base import CommitResult, StorageBackend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome
from dropshare_shared.schemas.uploads import UploadStatus
from dropshare_shared.schemas.users import UserStatus

logger = logging.getLogger(__name__)

# The name a file gets when the scanner kept failing on it: refused, never published unscanned.
UNSCANNABLE = "DropShare.Unscannable"
_IDLE_SECONDS = 2.0


class ScanOutcome(StrEnum):
    CLEAN = "clean"  # the version is visible now
    INFECTED = "infected"  # refused as MALWARE_DETECTED (or unscannable), its data removed
    REFUSED = "refused"  # not published: its target changed, or its uploader can't any more
    RETRY = "retry"  # this file failed; tried again later on its own
    SCANNER_DOWN = "scanner_down"  # the scanner doesn't answer at all; nothing was changed
    NOTHING = "nothing"  # no upload is due


@dataclass
class ScanCounts:
    clean: int = 0
    infected: int = 0
    refused: int = 0
    retrying: int = 0
    scanner_down: bool = False

    @property
    def handled(self) -> int:
        return self.clean + self.infected + self.refused + self.retrying


def run_forever(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    scanner: Scanner,
    settings: Settings,
) -> NoReturn:
    """The scan worker's loop (`jobs run scan-uploads --loop`). Idle, it looks again every two
    seconds; while the scanner is down, it waits twice as long each time, up to
    `scan_retry_max_seconds`."""
    pause = _IDLE_SECONDS
    while True:
        counts = scan_due(session_factory, storage, scanner, settings)
        if counts.scanner_down:
            time.sleep(pause)
            pause = min(pause * 2, settings.scan_retry_max_seconds)
            continue
        pause = _IDLE_SECONDS
        if not counts.handled:
            time.sleep(_IDLE_SECONDS)


def scan_due(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    scanner: Scanner,
    settings: Settings,
) -> ScanCounts:
    """Scan every upload that is due, until none is or the scanner doesn't answer at all."""
    counts = ScanCounts()
    while True:
        outcome = scan_next(session_factory, storage, scanner, settings, datetime.now(UTC))
        match outcome:
            case ScanOutcome.NOTHING:
                return counts
            case ScanOutcome.SCANNER_DOWN:
                counts.scanner_down = True
                return counts
            case ScanOutcome.CLEAN:
                counts.clean += 1
            case ScanOutcome.INFECTED:
                counts.infected += 1
            case ScanOutcome.REFUSED:
                counts.refused += 1
            case ScanOutcome.RETRY:
                counts.retrying += 1


def scan_next(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    scanner: Scanner,
    settings: Settings,
    now: datetime,
) -> ScanOutcome:
    """Scan the upload due longest, if any, and record what the scan decided."""
    with session_factory() as session:
        upload = upload_repository.lock_next_to_scan(session, now)
        if upload is None:
            return ScanOutcome.NOTHING
        upload_id = upload.id
        try:
            return _process(session, storage, scanner, settings, upload, now)
        except Exception:
            logger.exception("Scanning upload %s failed unexpectedly", upload_id)
            session.rollback()
    return _postpone(session_factory, storage, settings, upload_id, now)


def _process(
    session: Session,
    storage: StorageBackend,
    scanner: Scanner,
    settings: Settings,
    upload: UploadSession,
    now: datetime,
) -> ScanOutcome:
    ctx = _uploader(upload)
    blob_key = upload.blob_key
    if blob_key is None:
        return _refuse(session, storage, upload, ctx, ErrorCode.NOT_FOUND, "no_blob", None)
    reason = _uploader_cant(session, upload)
    if reason is not None:  # disabled or revoked while it waited: nothing of theirs appears
        return _refuse(session, storage, upload, ctx, ErrorCode.NOT_FOUND, reason, blob_key)
    try:
        scanner.version()
    except ScannerUnavailableError:
        session.rollback()  # every upload waits, this one untouched (OQ-55)
        logger.warning("The malware scanner doesn't answer; uploads wait", exc_info=True)
        return ScanOutcome.SCANNER_DOWN
    try:
        scan = scanner.scan(upload_service.blob_chunks(storage, blob_key, upload.size_bytes))
    except FileNotFoundError:
        return _refuse(session, storage, upload, ctx, ErrorCode.NOT_FOUND, "blob_missing", None)
    except (ScannerUnavailableError, OSError):
        logger.warning("Scanning upload %s failed", upload.id, exc_info=True)
        return _retry_or_give_up(session, storage, settings, upload, ctx, now)

    if scan.detection is not None:
        return _infected(session, storage, upload, ctx, blob_key, scan.detection)
    stored = CommitResult(size_bytes=upload.size_bytes, sha256=upload.sha256)
    try:
        upload_service.record_version(
            session, ctx, upload.user_id, upload, blob_key, stored, scan, datetime.now(UTC)
        )
    except AppError as exc:
        session.rollback()
        locked = upload_repository.get_for_update(session, upload.id)
        if locked is None or locked.status != UploadStatus.SCANNING:
            return ScanOutcome.REFUSED  # cancelled or purged meanwhile: nothing to record
        return _refuse(session, storage, locked, ctx, exc.code, exc.code.value, blob_key)
    session.commit()
    return ScanOutcome.CLEAN


def _uploader(upload: UploadSession) -> CallContext:
    """Audit as the user who completed the upload, from the device, address and request they
    used (FR-AUDIT-02)."""
    return CallContext(
        actor=Actor(AuditActorType.USER, user_id=upload.user_id, device_id=upload.device_id),
        request_id=upload.request_id,
        source_ip=upload.source_ip,
    )


def _uploader_cant(session: Session, upload: UploadSession) -> str | None:
    """Why the uploader may no longer publish anything, if they may not: their account was
    disabled, or the device they uploaded from revoked, while the upload waited."""
    user = user_repository.get(session, upload.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        return "account_disabled"
    if upload.device_id is not None:
        device = device_repository.get(session, upload.device_id)
        if device is not None and device.revoked_at is not None:
            return "device_revoked"
    return None


def _infected(
    session: Session,
    storage: StorageBackend,
    upload: UploadSession,
    ctx: CallContext,
    blob_key: str,
    detection: str,
) -> ScanOutcome:
    target, details = upload_service.malware_audit(session, upload, detection)
    upload_service.reject(upload, ErrorCode.MALWARE_DETECTED, detection)
    audit_service.record(
        session,
        ctx,
        AuditAction.FILE_MALWARE_DETECTED,
        AuditOutcome.DENIED,
        target=target,
        details=details,
    )
    upload_id = upload.id
    session.commit()
    _remove_blob(storage, blob_key)
    logger.warning("Upload %s refused: %s", upload_id, detection)
    return ScanOutcome.INFECTED


def _refuse(
    session: Session,
    storage: StorageBackend,
    upload: UploadSession,
    ctx: CallContext,
    code: ErrorCode,
    reason: str,
    blob_key: str | None,
) -> ScanOutcome:
    """Not published, and audited (FR-AUDIT-01): `complete` answers with `code` from now on,
    and the client sends the file again if it still can."""
    target = upload_service.audit_target(session, upload)
    upload_service.reject(upload, code)
    denied = reason in ("account_disabled", "device_revoked", ErrorCode.FORBIDDEN.value)
    audit_service.record(
        session,
        ctx,
        AuditAction.FILE_UPLOADED,
        AuditOutcome.DENIED if denied else AuditOutcome.FAILURE,
        target=target,
        details={"reason": reason, "upload_id": str(upload.id), "after_scan": True},
    )
    upload_id = upload.id
    session.commit()
    if blob_key is not None:
        _remove_blob(storage, blob_key)
    logger.info("Upload %s not published: %s", upload_id, reason)
    return ScanOutcome.REFUSED


def _retry_or_give_up(
    session: Session,
    storage: StorageBackend,
    settings: Settings,
    upload: UploadSession,
    ctx: CallContext,
    now: datetime,
) -> ScanOutcome:
    """This file failed while the scanner answers otherwise: try it again later, waiting longer
    each time; after `scan_max_attempts`, refuse it as unscannable (fail closed)."""
    upload.scan_attempts += 1
    if upload.scan_attempts >= settings.scan_max_attempts and upload.blob_key is not None:
        return _infected(session, storage, upload, ctx, upload.blob_key, UNSCANNABLE)
    delay = min(30 * 2 ** (upload.scan_attempts - 1), settings.scan_retry_max_seconds)
    upload.next_scan_at = now + timedelta(seconds=delay)
    upload_id, attempts = upload.id, upload.scan_attempts
    session.commit()
    logger.warning("Upload %s: scan try %d failed; next in %d s", upload_id, attempts, delay)
    return ScanOutcome.RETRY


def _postpone(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    settings: Settings,
    upload_id: uuid.UUID,
    now: datetime,
) -> ScanOutcome:
    """After an unexpected error: count it as a failed try, in a fresh transaction."""
    with session_factory() as session:
        upload = upload_repository.get_for_update(session, upload_id, skip_locked=True)
        if upload is None or upload.status != UploadStatus.SCANNING:
            return ScanOutcome.RETRY
        return _retry_or_give_up(session, storage, settings, upload, _uploader(upload), now)


def _remove_blob(storage: StorageBackend, blob_key: str) -> None:
    try:
        storage.delete_blob(blob_key)
    except OSError:
        logger.warning("Couldn't remove blob %s; gc-blobs will", blob_key, exc_info=True)
