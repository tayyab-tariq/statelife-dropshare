"""Process-wide resources created in the app lifespan and handed to requests."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.rate_limit import SlidingWindowLimiter
from dropshare_server.mailer.base import Mailer
from dropshare_server.scanner.base import Scanner
from dropshare_server.services.file_policy import FileTypePolicy
from dropshare_server.services.releases import MinimumVersion
from dropshare_server.storage.base import StorageBackend


@dataclass(frozen=True)
class AppResources:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    storage: StorageBackend
    minimum_version: MinimumVersion  # the effective minimum client version (FR-UPD-04)
    file_types: FileTypePolicy  # banned file types (FR-SAFE-02)
    scanner: Scanner  # the malware scanner for scans within a request (FR-SAFE-01)
    scanner_probe: Scanner  # the same, answering within seconds, for /health/ready
    mailer: Mailer  # temporary-password emails (FR-ADMIN-07)
    admin_limiter: SlidingWindowLimiter  # admin write actions per admin (NFR-SEC-13)


def get_resources(request: Request) -> AppResources:
    resources = request.app.state.resources
    if not isinstance(resources, AppResources):
        raise TypeError("App resources are not initialised")
    return resources


Resources = Annotated[AppResources, Depends(get_resources)]


def get_db(resources: Resources) -> Iterator[Session]:
    """One session per request: commit on success, roll back on error, always close."""
    with resources.session_factory() as session:
        try:
            yield session
        except BaseException:
            session.rollback()
            raise
        session.commit()


def get_storage(resources: Resources) -> StorageBackend:
    return resources.storage


# scope="function": commit before the response is sent, so a failed commit is never reported
# to the client as success (the default "request" scope runs cleanup after sending).
DbSession = Annotated[Session, Depends(get_db, scope="function")]
Storage = Annotated[StorageBackend, Depends(get_storage)]
