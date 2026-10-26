import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from dropshare_server.models import User


def add(session: Session, user: User) -> None:
    session.add(user)
    session.flush()


def get(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def get_for_update(session: Session, user_id: uuid.UUID) -> User | None:
    """Lock the user row: quota usage is read and changed under this lock (database.md §7)."""
    return session.scalars(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


def get_by_username(session: Session, username_normalized: str) -> User | None:
    return session.scalars(
        select(User).where(User.username_normalized == username_normalized)
    ).one_or_none()


def get_by_login(session: Session, login_normalized: str) -> User | None:
    """Match a username or an email (both stored normalized, both unique)."""
    return session.scalars(
        select(User).where(
            or_(
                User.username_normalized == login_normalized,
                User.email_normalized == login_normalized,
            )
        )
    ).first()


def username_taken(session: Session, username_normalized: str) -> bool:
    return get_by_username(session, username_normalized) is not None


def email_taken(session: Session, email_normalized: str) -> bool:
    return (
        session.scalars(
            select(User.id).where(User.email_normalized == email_normalized)
        ).one_or_none()
        is not None
    )


def list_all(session: Session) -> list[User]:
    return list(session.scalars(select(User).order_by(User.username_normalized, User.id)))
