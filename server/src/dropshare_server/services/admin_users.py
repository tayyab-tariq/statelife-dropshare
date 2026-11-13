"""User administration from the app (FR-ADMIN-02, FR-ADMIN-07).

Built on the services the CLI uses (`users`), so validation, hashing and audit events are the
same; this module adds what the app needs: the caller must be an admin, users are addressed by
ID, an admin can't change their own status or role, an enabled admin always remains, and the
temporary password is emailed once the change is committed.
"""

import html
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.mailer.base import Mailer
from dropshare_server.models import User
from dropshare_server.repositories import users as user_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services import users as user_service
from dropshare_server.services.audit import user_target
from dropshare_server.services.context import CallContext
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.admin import (
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserUpdateIn,
    EmailOutcome,
    TemporaryPasswordOut,
)
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.users import UserRole, UserStatus

_PRODUCT = "StateLife DropShare"


def to_admin_out(user: User) -> AdminUserOut:
    return AdminUserOut.model_validate(user, from_attributes=True)


def list_users(session: Session, ctx: CallContext, admin: User) -> list[AdminUserOut]:
    """Every account, enabled and disabled, by username. No paging: a few hundred at most."""
    _require_admin(admin)
    users = [to_admin_out(user) for user in user_repository.list_all(session)]
    audit_service.record(session, ctx, AuditAction.USER_LISTED, details={"count": len(users)})
    return users


def create_user(
    session: Session, ctx: CallContext, mailer: Mailer, admin: User, body: AdminUserCreateIn
) -> TemporaryPasswordOut:
    """Create the account, commit, then email the temporary password (FR-ADMIN-07)."""
    _require_admin(admin)
    created = user_service.create_user(
        session,
        ctx,
        username=body.username,
        email=body.email,
        display_name=body.display_name,
        role=body.role,
        quota_bytes=body.quota_bytes,
    )
    user = _get(session, created.user.id)
    return _commit_and_email(session, ctx, mailer, user, created.temporary_password, _welcome_email)


def reset_password(
    session: Session, ctx: CallContext, mailer: Mailer, admin: User, user_id: uuid.UUID
) -> TemporaryPasswordOut:
    """New temporary password, every session ended, lockout cleared (as the CLI does); then
    commit and email it. Allowed on any account, the admin's own included: it only signs them
    out and makes them choose a new password."""
    _require_admin(admin)
    user = _get(session, user_id)
    temporary_password = user_service.reset_password(session, ctx, user.username)
    return _commit_and_email(session, ctx, mailer, user, temporary_password, _reset_email)


def update_user(
    session: Session, ctx: CallContext, admin: User, user_id: uuid.UUID, body: AdminUserUpdateIn
) -> AdminUserOut:
    """Change status, role and/or quota in one transaction: all of them or none."""
    _require_admin(admin)
    user = _get(session, user_id)
    status_changes = body.status is not None and body.status != user.status
    role_changes = body.role is not None and body.role != user.role
    if (status_changes or role_changes) and user.id == admin.id:
        raise AppError(
            409,
            ErrorCode.SELF_CHANGE_NOT_ALLOWED,
            "You can't change your own status or role. Ask another administrator.",
            denied=True,
            target=("user", str(user.id)),
        )
    if _removes_an_enabled_admin(user, body):
        _confirm_still_admin_under_lock(session, admin)

    if status_changes and body.status is not None:
        user_service.set_disabled(
            session, ctx, user.username, disabled=body.status is UserStatus.DISABLED
        )
    if role_changes and body.role is not None:
        user_service.set_role(session, ctx, user.username, body.role)
    if body.quota_bytes is not None and body.quota_bytes != user.quota_bytes:
        user_service.set_quota(session, ctx, user.username, body.quota_bytes)
    return to_admin_out(user)


def _require_admin(admin: User) -> None:
    """The router's dependency checks this too; services never rely on the caller alone."""
    if admin.role is not UserRole.ADMIN or admin.status is not UserStatus.ACTIVE:
        raise AppError(403, ErrorCode.FORBIDDEN, "Only administrators can do that.")


def _get(session: Session, user_id: uuid.UUID) -> User:
    user = user_repository.get(session, user_id)
    if user is None:
        raise AppError(404, ErrorCode.NOT_FOUND, "That user doesn't exist.")
    return user


def _removes_an_enabled_admin(user: User, body: AdminUserUpdateIn) -> bool:
    if user.role is not UserRole.ADMIN or user.status is not UserStatus.ACTIVE:
        return False
    return body.status is UserStatus.DISABLED or body.role is UserRole.USER


def _confirm_still_admin_under_lock(session: Session, admin: User) -> None:
    """Keep an enabled admin, always (FR-ADMIN-02). Only an enabled admin may disable or demote
    another, never themselves, so the caller remains one, provided they still are one now.
    Every admin row is locked and re-read first: when two admins demote each other at once,
    the second waits here, finds they were demoted, and is refused."""
    admins = user_repository.lock_admins(session)
    if not any(other.id == admin.id and other.status is UserStatus.ACTIVE for other in admins):
        raise AppError(403, ErrorCode.FORBIDDEN, "Only administrators can do that.")


@dataclass(frozen=True)
class _Email:
    subject: str
    html: str


def _commit_and_email(
    session: Session,
    ctx: CallContext,
    mailer: Mailer,
    user: User,
    temporary_password: str,
    compose: Callable[[User, str], _Email],
) -> TemporaryPasswordOut:
    """Commit the change first, so a failed or slow email never undoes it (FR-ADMIN-07); then
    record whether it was sent. That event commits with the request."""
    out = to_admin_out(user)
    email = compose(user, temporary_password)
    session.commit()
    outcome = mailer.send(out.email, email.subject, email.html)
    audit_service.record(
        session,
        ctx,
        AuditAction.USER_PASSWORD_EMAILED,
        AuditOutcome.SUCCESS if outcome is EmailOutcome.SENT else AuditOutcome.FAILURE,
        target=user_target(user),
        details={"email": outcome.value},
    )
    return TemporaryPasswordOut(user=out, temporary_password=temporary_password, email=outcome)


def _welcome_email(user: User, temporary_password: str) -> _Email:
    return _Email(
        subject=f"Your {_PRODUCT} account",
        html=_body(
            user,
            temporary_password,
            f"An administrator has created a {_PRODUCT} account for you.",
        ),
    )


def _reset_email(user: User, temporary_password: str) -> _Email:
    return _Email(
        subject=f"Your {_PRODUCT} password was reset",
        html=_body(
            user,
            temporary_password,
            f"An administrator has reset your {_PRODUCT} password. You have been signed out "
            "on every device.",
        ),
    )


def _body(user: User, temporary_password: str, opening: str) -> str:
    """Only the username and the temporary password; every value escaped."""
    username = html.escape(user.username)
    password = html.escape(temporary_password)
    return (
        "<p>Hello,</p>"
        f"<p>{html.escape(opening)}</p>"
        f"<p>Username: <strong>{username}</strong><br>"
        f"Temporary password: <strong><code>{password}</code></strong></p>"
        f"<p>Sign in to the {_PRODUCT} app with these details. You will be asked to choose "
        "a new password straight away; the temporary one stops working once you do.</p>"
        "<p>If you weren't expecting this email, tell your IT administrator.</p>"
    )
