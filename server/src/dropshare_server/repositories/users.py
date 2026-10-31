import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from dropshare_server.models import User
from dropshare_shared.schemas.users import UserStatus


def add(session: Session, user: User) -> None:
    session.add(user)
    session.flush()


def get(session: Session, user_id: uuid.UUID) -> User | None:
    return session.get(User, user_id)


def get_for_update(session: Session, user_id: uuid.UUID) -> User | None:
    """Lock the user row: quota usage is read and changed under this lock (database.md §7).

    `key_share`: FOR NO KEY UPDATE on PostgreSQL (the key never changes), which doesn't wait
    for the key-share locks rows referencing this user take, such as a new upload session's.
    With FOR UPDATE, starting and completing two uploads at once deadlocked (M10). Oracle
    writes FOR UPDATE; its foreign-key checks don't lock the referenced row."""
    return session.scalars(
        select(User)
        .where(User.id == user_id)
        .with_for_update(key_share=True)
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


def search_active(session: Session, login_prefix: str, name_prefix: str, limit: int) -> list[User]:
    """Active users whose username or email starts with `login_prefix` (normalized), or whose
    display name starts with `name_prefix` (lower-cased); both LIKE-escaped with a backslash.
    The display name has no normalized column, so LOWER() scans: fine at our user counts."""
    return list(
        session.scalars(
            select(User)
            .where(
                User.status == UserStatus.ACTIVE,
                or_(
                    User.username_normalized.like(f"{login_prefix}%", escape="\\"),
                    User.email_normalized.like(f"{login_prefix}%", escape="\\"),
                    func.lower(User.display_name).like(f"{name_prefix}%", escape="\\"),
                ),
            )
            .order_by(User.display_name, User.id)
            .limit(limit)
        )
    )
