"""Authentication dependencies (NFR-SEC-02)."""

from typing import Annotated

from fastapi import Depends, Request

from dropshare_server.core.errors import AppError
from dropshare_server.dependencies.resources import DbSession, Resources
from dropshare_server.services import auth as auth_service
from dropshare_server.services.auth import CurrentSession
from dropshare_shared.errors import ErrorCode


def get_current_session(request: Request, db: DbSession, resources: Resources) -> CurrentSession:
    """Any valid session, including one that must still change its temporary password."""
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AppError(401, ErrorCode.UNAUTHENTICATED, "Sign in to continue.")
    current = auth_service.authenticate(db, resources.settings, token.strip())
    request.state.actor = current.actor  # lets the error handler attribute denials
    return current


AnySession = Annotated[CurrentSession, Depends(get_current_session)]


def get_active_session(current: AnySession) -> CurrentSession:
    """A session allowed to use the API: the temporary password has been changed."""
    return auth_service.require_password_changed(current)


ActiveSession = Annotated[CurrentSession, Depends(get_active_session)]
