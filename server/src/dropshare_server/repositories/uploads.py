import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from dropshare_server.models import Namespace, UploadSession
from dropshare_shared.schemas.uploads import UploadStatus

_IN_LIST_LIMIT = 1000  # Oracle ORA-01795
# Rows the scan worker looks at to find one no other worker holds.
_SCAN_CANDIDATES = 20


def add(session: Session, upload: UploadSession) -> None:
    session.add(upload)
    session.flush()


def get(session: Session, upload_id: uuid.UUID) -> UploadSession | None:
    return session.get(UploadSession, upload_id)


def get_for_update(
    session: Session, upload_id: uuid.UUID, *, skip_locked: bool = False
) -> UploadSession | None:
    """Lock the session so two concurrent `complete` calls can't both create a version. With
    `skip_locked`, None if someone else holds it (the scan worker scanning it)."""
    return session.scalars(
        select(UploadSession)
        .where(UploadSession.id == upload_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    ).one_or_none()


def lock_next_to_scan(session: Session, now: datetime) -> UploadSession | None:
    """The scanning upload due longest, locked; ones another worker holds are skipped. Exactly
    one row is locked: the worker holds it for the whole scan, and any other it held would block
    cancelling that upload or purging its folder meanwhile. Candidates are read first because
    Oracle can't combine FOR UPDATE with FETCH FIRST (ORA-02014)."""
    due = (UploadSession.status == UploadStatus.SCANNING, UploadSession.next_scan_at <= now)
    candidates = session.scalars(
        select(UploadSession.id)
        .where(*due)
        .order_by(UploadSession.next_scan_at, UploadSession.id)
        .limit(_SCAN_CANDIDATES)
    ).all()
    for upload_id in candidates:
        upload = session.scalars(
            select(UploadSession)
            .where(UploadSession.id == upload_id, *due)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).one_or_none()
        if upload is not None:
            return upload
    return None


def scanning_blob_keys(session: Session, blob_keys: Sequence[str]) -> set[str]:
    """Which of these blobs belong to uploads waiting for their scan: gc-blobs keeps them,
    however long the scanner is down (OQ-55)."""
    found: set[str] = set()
    for start in range(0, len(blob_keys), _IN_LIST_LIMIT):
        found.update(
            key
            for key in session.scalars(
                select(UploadSession.blob_key).where(
                    UploadSession.blob_key.in_(blob_keys[start : start + _IN_LIST_LIMIT]),
                    UploadSession.status == UploadStatus.SCANNING,
                )
            )
            if key is not None
        )
    return found


def reserved_bytes(session: Session, owner_user_id: uuid.UUID, now: datetime) -> int:
    """Bytes promised to unexpired pending uploads, and to uploads waiting for their malware
    scan, into namespaces this user owns.

    Every pending upload reserves its full size, new versions included: that is what it stages
    on disk until it completes or expires (audit decision D1). A scanning one holds its blob
    until the scan decides, however long that takes (OQ-55).
    """
    query = (
        select(func.coalesce(func.sum(UploadSession.size_bytes), 0))
        .join(Namespace, Namespace.id == UploadSession.namespace_id)
        .where(
            Namespace.owner_user_id == owner_user_id,
            or_(
                and_(UploadSession.status == UploadStatus.PENDING, UploadSession.expires_at > now),
                UploadSession.status == UploadStatus.SCANNING,
            ),
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


def _for_nodes(batch: Sequence[uuid.UUID]) -> ColumnElement[bool]:
    return or_(UploadSession.node_id.in_(batch), UploadSession.parent_id.in_(batch))


def lock_for_nodes(session: Session, node_ids: Sequence[uuid.UUID]) -> bool:
    """Lock the uploads into or onto these nodes without waiting; False if any is locked by
    someone else (an upload completing right now), so a purge can back off instead of
    deadlocking with it."""
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        batch = node_ids[start : start + _IN_LIST_LIMIT]
        every = set(session.scalars(select(UploadSession.id).where(_for_nodes(batch))))
        if not every:
            continue
        locked = set(
            session.scalars(
                select(UploadSession.id)
                .where(UploadSession.id.in_(every))
                .with_for_update(skip_locked=True)
            )
        )
        if locked != every:
            return False
    return True


def delete_for_nodes(session: Session, node_ids: Sequence[uuid.UUID]) -> None:
    """These nodes are being purged: remove the uploads onto them and the unfinished ones into
    them. A finished upload whose file was moved out keeps its row, without the folder, so a
    repeated `complete` still answers with its file (NFR-REL-04). Chunks staged for a removed
    pending upload are left to expire-uploads, which removes what no pending upload owns."""
    for start in range(0, len(node_ids), _IN_LIST_LIMIT):
        batch = node_ids[start : start + _IN_LIST_LIMIT]
        session.execute(
            delete(UploadSession)
            .where(
                or_(
                    UploadSession.node_id.in_(batch),
                    and_(
                        UploadSession.parent_id.in_(batch),
                        UploadSession.status != UploadStatus.COMPLETED,
                    ),
                )
            )
            .execution_options(synchronize_session=False)
        )
        session.execute(
            update(UploadSession)
            .where(UploadSession.parent_id.in_(batch))
            .values(parent_id=None)
            .execution_options(synchronize_session=False)
        )
