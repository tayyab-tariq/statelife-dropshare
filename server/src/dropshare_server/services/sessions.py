"""Ending sign-in sessions: shared by auth (sign-out, password change) and admin actions."""

import uuid
from datetime import datetime

from sqlalchemy.orm import Session

from dropshare_server.models import User
from dropshare_server.repositories import devices as device_repository
from dropshare_server.repositories import refresh_tokens as token_repository


def end_device_session(session: Session, device_id: uuid.UUID, now: datetime) -> None:
    token_repository.revoke_for_device(session, device_id, now)
    device_repository.revoke(session, device_id, now)


def end_all_sessions(
    session: Session, user: User, now: datetime, except_device_id: uuid.UUID | None = None
) -> None:
    """Revoke refresh tokens and devices. Unless a device is kept, also invalidate every access
    token at once by bumping the token version (access tokens are otherwise valid for 15 min)."""
    token_repository.revoke_for_user(session, user.id, now, except_device_id)
    device_repository.revoke_all_for_user(session, user.id, now, except_device_id)
    if except_device_id is None:
        user.token_version += 1
