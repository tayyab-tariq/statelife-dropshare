"""Failed sign-in counting and lockout (FR-AUTH-06), for accounts and for unknown logins alike.

Every statement is a single atomic UPDATE so concurrent attempts can't overtake the lock.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, CursorResult, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dropshare_server.models import LoginThrottle, User


@dataclass(frozen=True)
class AccountKey:
    user_id: uuid.UUID


@dataclass(frozen=True)
class UnknownLoginKey:
    login_hash: str  # SHA-256 of the normalized login, never the login itself


type ThrottleKey = AccountKey | UnknownLoginKey


def _locate(key: ThrottleKey) -> tuple[type[User] | type[LoginThrottle], ColumnElement[bool]]:
    match key:
        case AccountKey(user_id):
            return User, User.id == user_id
        case UnknownLoginKey(login_hash):
            return LoginThrottle, LoginThrottle.login_hash == login_hash


def _not_locked(model: type[User] | type[LoginThrottle], now: datetime) -> ColumnElement[bool]:
    return or_(model.locked_until.is_(None), model.locked_until <= now)


def _rowcount(result: object) -> int:
    """Rows matched by an UPDATE (Session.execute is typed as returning a generic Result)."""
    if not isinstance(result, CursorResult):
        raise TypeError("Expected the result of an UPDATE statement")
    return result.rowcount


def clear_expired_lock(session: Session, key: ThrottleKey, now: datetime) -> bool:
    """Lift a lock whose time is up. True only for the one caller that actually lifted it."""
    model, match = _locate(key)
    result = session.execute(
        update(model)
        .where(match, model.locked_until.is_not(None), model.locked_until <= now)
        .values(failed_login_count=0, locked_until=None)
    )
    return _rowcount(result) == 1


def reserve_attempt(session: Session, key: ThrottleKey, now: datetime) -> int | None:
    """Count an attempt before the password is checked. Returns the attempt number, or None
    when the subject is currently locked."""
    model, match = _locate(key)
    result = session.execute(
        update(model)
        .where(match, _not_locked(model, now))
        .values(failed_login_count=model.failed_login_count + 1)
    )
    if _rowcount(result) == 0:
        if isinstance(key, UnknownLoginKey) and locked_until(session, key) is None:
            if not _insert_first_attempt(session, key, now):
                return reserve_attempt(session, key, now)  # a parallel attempt created the row
            return 1
        return None
    return session.scalars(select(model.failed_login_count).where(match)).one()


def _insert_first_attempt(session: Session, key: UnknownLoginKey, now: datetime) -> bool:
    try:
        with session.begin_nested():
            session.execute(
                insert(LoginThrottle).values(
                    login_hash=key.login_hash, failed_login_count=1, updated_at=now
                )
            )
    except IntegrityError:
        return False
    return True


def locked_until(session: Session, key: ThrottleKey) -> datetime | None:
    model, match = _locate(key)
    return session.scalars(select(model.locked_until).where(match)).one_or_none()


def lock(session: Session, key: ThrottleKey, until: datetime, now: datetime) -> bool:
    """Lock unless already locked. True only for the caller that set the lock."""
    model, match = _locate(key)
    result = session.execute(
        update(model).where(match, _not_locked(model, now)).values(locked_until=until)
    )
    return _rowcount(result) == 1


def reset(session: Session, key: ThrottleKey) -> None:
    model, match = _locate(key)
    session.execute(update(model).where(match).values(failed_login_count=0, locked_until=None))
