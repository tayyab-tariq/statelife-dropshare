import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from dropshare_server.models import RefreshToken


def add(session: Session, token: RefreshToken) -> None:
    session.add(token)
    session.flush()


def get_by_hash_for_update(session: Session, token_hash: str) -> RefreshToken | None:
    """Row-locked, so two concurrent refreshes with the same token can't both rotate it."""
    return session.scalars(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    ).one_or_none()


def revoke_family(session: Session, family_id: uuid.UUID, now: datetime) -> None:
    session.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_for_device(session: Session, device_id: uuid.UUID, now: datetime) -> None:
    session.execute(
        update(RefreshToken)
        .where(RefreshToken.device_id == device_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_for_user(
    session: Session, user_id: uuid.UUID, now: datetime, except_device_id: uuid.UUID | None = None
) -> None:
    query = update(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
    )
    if except_device_id is not None:
        query = query.where(RefreshToken.device_id != except_device_id)
    session.execute(query.values(revoked_at=now))
