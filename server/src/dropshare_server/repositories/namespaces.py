import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from dropshare_server.models import Namespace
from dropshare_shared.schemas.nodes import NamespaceKind


def add(session: Session, namespace: Namespace) -> None:
    session.add(namespace)
    session.flush()


def get(session: Session, namespace_id: uuid.UUID) -> Namespace | None:
    return session.get(Namespace, namespace_id)


def get_for_update(session: Session, namespace_id: uuid.UUID) -> Namespace | None:
    """FOR NO KEY UPDATE, as users.get_for_update: rows referencing the namespace (new nodes,
    upload sessions, journal rows) take key-share locks on it that must not deadlock."""
    return session.scalars(
        select(Namespace)
        .where(Namespace.id == namespace_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    ).one_or_none()


def list_owned(session: Session, owner_user_id: uuid.UUID) -> list[Namespace]:
    return list(
        session.scalars(
            select(Namespace)
            .where(Namespace.owner_user_id == owner_user_id)
            .order_by(Namespace.created_at, Namespace.id)
        )
    )


def get_personal(session: Session, owner_user_id: uuid.UUID) -> Namespace | None:
    return session.scalars(
        select(Namespace).where(
            Namespace.owner_user_id == owner_user_id, Namespace.kind == NamespaceKind.PERSONAL
        )
    ).one_or_none()
