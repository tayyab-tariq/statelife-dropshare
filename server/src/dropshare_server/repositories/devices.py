import uuid
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from dropshare_server.models import Device


def add(session: Session, device: Device) -> None:
    session.add(device)
    session.flush()


def get(session: Session, device_id: uuid.UUID) -> Device | None:
    return session.get(Device, device_id)


def revoke(session: Session, device_id: uuid.UUID, now: datetime) -> None:
    session.execute(
        update(Device)
        .where(Device.id == device_id, Device.revoked_at.is_(None))
        .values(revoked_at=now)
    )


def revoke_all_for_user(
    session: Session, user_id: uuid.UUID, now: datetime, except_device_id: uuid.UUID | None = None
) -> None:
    query = update(Device).where(Device.user_id == user_id, Device.revoked_at.is_(None))
    if except_device_id is not None:
        query = query.where(Device.id != except_device_id)
    session.execute(query.values(revoked_at=now))
