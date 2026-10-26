import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dropshare_server.models import Namespace, UploadSession
from dropshare_shared.schemas.uploads import UploadStatus


def add(session: Session, upload: UploadSession) -> None:
    session.add(upload)
    session.flush()


def get(session: Session, upload_id: uuid.UUID) -> UploadSession | None:
    return session.get(UploadSession, upload_id)


def get_for_update(session: Session, upload_id: uuid.UUID) -> UploadSession | None:
    """Lock the session so two concurrent `complete` calls can't both create a version."""
    return session.scalars(
        select(UploadSession)
        .where(UploadSession.id == upload_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def reserved_bytes(session: Session, owner_user_id: uuid.UUID, now: datetime) -> int:
    """Bytes promised to unexpired pending uploads into namespaces this user owns.

    Every pending upload reserves its full size, new versions included: that is what it stages
    on disk until it completes or expires (audit decision D1).
    """
    query = (
        select(func.coalesce(func.sum(UploadSession.size_bytes), 0))
        .join(Namespace, Namespace.id == UploadSession.namespace_id)
        .where(
            Namespace.owner_user_id == owner_user_id,
            UploadSession.status == UploadStatus.PENDING,
            UploadSession.expires_at > now,
        )
    )
    return int(session.scalar(query) or 0)


def lock_expired(session: Session, now: datetime, limit: int) -> list[UploadSession]:
    """Up to `limit` pending uploads past their expiry, locked; rows another worker holds are
    skipped. Two steps because Oracle can't combine FOR UPDATE with FETCH FIRST (ORA-02014)."""
    expired = (UploadSession.status == UploadStatus.PENDING, UploadSession.expires_at <= now)
    ids = list(
        session.scalars(
            select(UploadSession.id)
            .where(*expired)
            .order_by(UploadSession.expires_at, UploadSession.id)
            .limit(limit)
        )
    )
    if not ids:
        return []
    return list(
        session.scalars(
            select(UploadSession)
            .where(UploadSession.id.in_(ids), *expired)
            .with_for_update(skip_locked=True)
        )
    )


def pending_ids(session: Session, upload_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these uploads are still pending (callers pass at most 1000 ids)."""
    return set(
        session.scalars(
            select(UploadSession.id).where(
                UploadSession.id.in_(upload_ids), UploadSession.status == UploadStatus.PENDING
            )
        )
    )
