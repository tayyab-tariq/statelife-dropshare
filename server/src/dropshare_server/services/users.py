"""Account administration used by the admin CLI (FR-ADMIN-01). No HTTP endpoints in v1."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.core.security import generate_temporary_password, hash_password
from dropshare_server.models import Namespace, Node, User
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services import sessions as session_service
from dropshare_server.services.audit import user_target
from dropshare_server.services.context import CallContext
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.nodes import NamespaceKind, NodeKind
from dropshare_shared.schemas.users import (
    DISPLAY_NAME_MAX_LENGTH,
    EMAIL_MAX_LENGTH,
    EMAIL_PATTERN,
    USERNAME_PATTERN,
    UserOut,
    UserRole,
    UserStatus,
)


@dataclass(frozen=True)
class CreatedUser:
    user: UserOut
    temporary_password: str  # shown once by the CLI, never stored or logged


def normalize_login(value: str) -> str:
    """Usernames and emails are unique case-insensitively (FR-ADMIN-01)."""
    return normalize_name(value.strip())


def to_out(user: User) -> UserOut:
    return UserOut.model_validate(user, from_attributes=True)


def _invalid(message: str) -> AppError:
    return AppError(400, ErrorCode.VALIDATION_ERROR, message)


def create_user(
    session: Session,
    ctx: CallContext,
    *,
    username: str,
    email: str,
    display_name: str,
    role: UserRole,
    quota_bytes: int,
) -> CreatedUser:
    username, email, display_name = username.strip(), email.strip(), display_name.strip()
    if not USERNAME_PATTERN.fullmatch(username):
        raise _invalid("Username must be 3-64 characters: letters, digits, '.', '_' or '-'.")
    if len(email) > EMAIL_MAX_LENGTH or not EMAIL_PATTERN.fullmatch(email):
        raise _invalid("Email address is not valid.")
    if not display_name or len(display_name) > DISPLAY_NAME_MAX_LENGTH:
        raise _invalid(f"Display name must be 1-{DISPLAY_NAME_MAX_LENGTH} characters.")
    if quota_bytes < 0:
        raise _invalid("Quota can't be negative.")
    if user_repository.username_taken(session, normalize_login(username)):
        raise AppError(409, ErrorCode.NAME_CONFLICT, f"Username '{username}' is already taken.")
    if user_repository.email_taken(session, normalize_login(email)):
        raise AppError(409, ErrorCode.NAME_CONFLICT, f"Email '{email}' is already in use.")

    now = datetime.now(UTC)
    temporary_password = generate_temporary_password()
    user = User(
        username=username,
        username_normalized=normalize_login(username),
        email=email,
        email_normalized=normalize_login(email),
        display_name=display_name,
        password_hash=hash_password(temporary_password),
        role=role,
        status=UserStatus.ACTIVE,
        quota_bytes=quota_bytes,
        failed_login_count=0,
        must_change_password=True,
        token_version=0,
        created_at=now,
        updated_at=now,
    )
    user_repository.add(session, user)
    namespace = Namespace(kind=NamespaceKind.PERSONAL, owner_user_id=user.id, created_at=now)
    namespace_repository.add(session, namespace)
    node_repository.add(
        session,
        Node(
            namespace_id=namespace.id,
            kind=NodeKind.FOLDER,
            size_bytes=0,
            revision=1,
            created_at=now,
            created_by_user_id=user.id,
            modified_at=now,
            modified_by_user_id=user.id,
        ),
    )
    audit_service.record(
        session,
        ctx,
        AuditAction.USER_CREATED,
        target=user_target(user),
        details={"role": role.value, "quota_bytes": quota_bytes},
    )
    return CreatedUser(user=to_out(user), temporary_password=temporary_password)


def _require(session: Session, username: str) -> User:
    user = user_repository.get_by_username(session, normalize_login(username))
    if user is None:
        raise AppError(404, ErrorCode.NOT_FOUND, f"No user named '{username}'.")
    return user


def find_user_id(session: Session, username: str) -> uuid.UUID:
    return _require(session, username).id


def reset_password(session: Session, ctx: CallContext, username: str) -> str:
    """Set a new temporary password, end every session, and clear any lockout."""
    user = _require(session, username)
    now = datetime.now(UTC)
    was_locked = user.locked_until is not None and user.locked_until > now
    temporary_password = generate_temporary_password()
    user.password_hash = hash_password(temporary_password)
    user.must_change_password = True
    user.failed_login_count = 0
    user.locked_until = None
    user.updated_at = now
    session_service.end_all_sessions(session, user, now)
    audit_service.record(session, ctx, AuditAction.AUTH_PASSWORD_RESET, target=user_target(user))
    if was_locked:  # FR-AUTH-06: unlocks are audited too
        audit_service.record(
            session,
            ctx,
            AuditAction.AUTH_UNLOCKED,
            target=user_target(user),
            details={"reason": "password_reset"},
        )
    return temporary_password


def set_disabled(session: Session, ctx: CallContext, username: str, disabled: bool) -> UserOut:
    """Disabling ends all sessions immediately; files and shares are kept (FR-ADMIN-01)."""
    user = _require(session, username)
    now = datetime.now(UTC)
    user.status = UserStatus.DISABLED if disabled else UserStatus.ACTIVE
    user.updated_at = now
    if disabled:
        session_service.end_all_sessions(session, user, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.USER_DISABLED if disabled else AuditAction.USER_ENABLED,
        target=user_target(user),
    )
    return to_out(user)


def list_users(session: Session, ctx: CallContext) -> list[tuple[UserOut, UserStatus]]:
    users = [(to_out(user), user.status) for user in user_repository.list_all(session)]
    audit_service.record(session, ctx, AuditAction.USER_LISTED, details={"count": len(users)})
    return users
