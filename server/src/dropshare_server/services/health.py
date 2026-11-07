"""Readiness: database, storage and malware scanner reachable (NFR-OPS-02, FR-SAFE-01)."""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.repositories import health as health_repository
from dropshare_server.scanner.base import Scanner, ScannerUnavailableError
from dropshare_server.storage.base import StorageBackend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.health import CheckStatus, ReadinessOut

logger = logging.getLogger(__name__)

# Older signatures are reported and logged, but don't fail readiness (OQ-58 (3)).
_STALE_SIGNATURE_HOURS = 48


def check_readiness(session: Session, storage: StorageBackend, scanner: Scanner) -> ReadinessOut:
    database = (
        CheckStatus.OK if health_repository.database_reachable(session) else CheckStatus.FAILED
    )
    try:
        storage.health_check()
        storage_status = CheckStatus.OK
    except OSError:
        logger.warning("Storage readiness check failed", exc_info=True)
        storage_status = CheckStatus.FAILED
    scanner_status, age_hours = _scanner(scanner)

    if CheckStatus.FAILED in (database, storage_status, scanner_status):
        raise AppError(
            503,
            ErrorCode.SERVICE_UNAVAILABLE,
            "The server isn't ready",
            {
                "database": database.value,
                "storage": storage_status.value,
                "scanner": scanner_status.value,
            },
        )
    return ReadinessOut(
        status=CheckStatus.OK,
        database=database,
        storage=storage_status,
        scanner=scanner_status,
        signatures_age_hours=age_hours,
    )


def _scanner(scanner: Scanner) -> tuple[CheckStatus, float | None]:
    try:
        version = scanner.version()
    except ScannerUnavailableError:
        logger.warning("Malware scanner readiness check failed", exc_info=True)
        return CheckStatus.FAILED, None
    if version.published_at is None:
        return CheckStatus.OK, None
    age_hours = round((datetime.now(UTC) - version.published_at).total_seconds() / 3600, 1)
    if age_hours > _STALE_SIGNATURE_HOURS:
        logger.warning("Malware signatures are %.0f hours old; is freshclam running?", age_hours)
    return CheckStatus.OK, age_hours
