"""Process-wide resources created in the app lifespan and handed to requests."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.storage.base import StorageBackend


@dataclass(frozen=True)
class AppResources:
    settings: Settings
    engine: Engine
    session_factory: sessionmaker[Session]
    storage: StorageBackend


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
