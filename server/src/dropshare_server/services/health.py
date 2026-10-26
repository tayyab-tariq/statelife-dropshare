"""Readiness: database and storage reachable (NFR-OPS-02)."""

import logging

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.repositories import health as health_repository
from dropshare_server.storage.base import StorageBackend
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.health import CheckStatus, ReadinessOut

logger = logging.getLogger(__name__)


def check_readiness(session: Session, storage: StorageBackend) -> ReadinessOut:
    database = (
        CheckStatus.OK if health_repository.database_reachable(session) else CheckStatus.FAILED
    )
    try:
        storage.health_check()
        storage_status = CheckStatus.OK
    except OSError:
        logger.warning("Storage readiness check failed", exc_info=True)
        storage_status = CheckStatus.FAILED

    if CheckStatus.FAILED in (database, storage_status):
        raise AppError(
            503,
            ErrorCode.SERVICE_UNAVAILABLE,
            "The server isn't ready",
            {"database": database.value, "storage": storage_status.value},
        )
    return ReadinessOut(status=CheckStatus.OK, database=database, storage=storage_status)
