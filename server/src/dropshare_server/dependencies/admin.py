"""Admin-only routes (FR-ADMIN-02, NFR-SEC-13). The role comes from the user row loaded for
this request, never from the token, so a demoted admin is refused at their next request. The
services check the role again."""

from typing import Annotated

from fastapi import Depends

from dropshare_server.core.errors import AppError
from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.resources import Resources
from dropshare_server.services.auth import CurrentSession
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.users import UserRole


def get_admin_session(current: ActiveSession) -> CurrentSession:
    if current.user.role is not UserRole.ADMIN:
        raise AppError(403, ErrorCode.FORBIDDEN, "Only administrators can do that.")
    return current


AdminSession = Annotated[CurrentSession, Depends(get_admin_session)]


def get_admin_write_session(current: AdminSession, resources: Resources) -> CurrentSession:
    """An admin session within its write limit. Reading the user list isn't counted."""
    wait = resources.admin_limiter.acquire(current.user.id)
    if wait is not None:
        raise AppError(
            429,
            ErrorCode.RATE_LIMITED,
            f"Too many changes in a short time. Try again in {wait} seconds.",
            headers={"Retry-After": str(wait)},
            denied=True,
        )
    return current


AdminWriteSession = Annotated[CurrentSession, Depends(get_admin_write_session)]
