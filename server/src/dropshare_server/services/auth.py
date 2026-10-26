"""Sign-in, token refresh, sign-out and password change (FR-AUTH-01..06)."""

import hashlib
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError
from dropshare_server.core.security import (
    AccessClaims,
    InvalidAccessTokenError,
    burn_password_check,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from dropshare_server.models import Device, RefreshToken, User
from dropshare_server.repositories import devices as device_repository
from dropshare_server.repositories import login_throttle
from dropshare_server.repositories import refresh_tokens as token_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.repositories.login_throttle import (
    AccountKey,
    ThrottleKey,
    UnknownLoginKey,
)
from dropshare_server.services import audit as audit_service
from dropshare_server.services import sessions as session_service
from dropshare_server.services.audit import AuditTarget, user_target
from dropshare_server.services.context import Actor, CallContext
from dropshare_server.services.users import normalize_login
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome
from dropshare_shared.schemas.auth import LoginIn, PasswordChangeIn, TokenPairOut
from dropshare_shared.schemas.envelope import ValidationIssue
from dropshare_shared.schemas.error_details import AccountLockedDetails
from dropshare_shared.schemas.users import EMAIL_MAX_LENGTH, EMAIL_PATTERN, UserStatus


@dataclass(frozen=True)
class CurrentSession:
    """The authenticated user and device behind a request."""

    user: User
    device: Device

    @property
    def actor(self) -> Actor:
        return Actor(AuditActorType.USER, user_id=self.user.id, device_id=self.device.id)


def _unauthenticated(audited: bool = False) -> AppError:
    return AppError(401, ErrorCode.UNAUTHENTICATED, "Sign in to continue.", audited=audited)


def _invalid_credentials() -> AppError:
    # FR-AUTH-01: the same message whether or not the user exists.
    return AppError(401, ErrorCode.UNAUTHENTICATED, "Invalid username or password.", audited=True)


def _locked(until: datetime, now: datetime) -> AppError:
    seconds = max(1, math.ceil((until - now).total_seconds()))
    minutes = max(1, math.ceil(seconds / 60))
    return AppError(
        423,
        ErrorCode.ACCOUNT_LOCKED,
        f"Too many failed sign-in attempts. Try again in {minutes} minutes.",
        AccountLockedDetails(locked_until=until, retry_after_seconds=seconds).model_dump(
            mode="json"
        ),
        headers={"Retry-After": str(seconds)},
        audited=True,
    )


def _field_error(field: str, message: str, error_type: str) -> AppError:
    issue = ValidationIssue(loc=["body", field], msg=message, type=error_type)
    return AppError(400, ErrorCode.VALIDATION_ERROR, message, [issue.model_dump()], audited=True)


def _login_label(login: str) -> str:
    """What to record for a sign-in attempt that matches no account (FR-AUDIT-02).

    People sometimes type their password into the username box, so only an email-shaped value
    is kept as typed; anything else is masked."""
    login = login.strip()
    if len(login) <= EMAIL_MAX_LENGTH and EMAIL_PATTERN.fullmatch(login):
        return login
    return f"{login[:1]}…{login[-1:]} ({len(login)} chars)" if len(login) > 2 else "…"


@dataclass(frozen=True)
class _Guard:
    """Whose attempts are counted, and how the related audit events are attributed."""

    key: ThrottleKey
    ctx: CallContext
    target: AuditTarget | None
    action: AuditAction  # the event a failed or refused attempt is recorded as


def _guarded_password_check(
    session_factory: sessionmaker[Session],
    settings: Settings,
    guard: _Guard,
    verify: Callable[[], bool],
    failure_reason: str,
) -> bool:
    """Check a password under the lockout rules (FR-AUTH-06).

    The attempt is reserved atomically *before* the (slow) password check, so parallel guesses
    can't get past the threshold. Each step commits in its own transaction because the request's
    transaction rolls back when the caller raises. Raises 423 while locked; returns whether the
    password matched.
    """
    now = datetime.now(UTC)
    lockout = timedelta(minutes=settings.lockout_minutes)
    with session_factory.begin() as own:
        if login_throttle.clear_expired_lock(own, guard.key, now):
            audit_service.record(
                own,
                guard.ctx,
                AuditAction.AUTH_UNLOCKED,
                target=guard.target,
                details={"reason": "expired"},
            )
        attempt = login_throttle.reserve_attempt(own, guard.key, now)
        if attempt is not None and attempt > settings.lockout_threshold:
            # Lost the race against the attempt that hit the threshold: same as locked.
            if login_throttle.lock(own, guard.key, now + lockout, now):
                _record_locked(own, guard, attempt, settings)
            attempt = None
        if attempt is None:
            until = login_throttle.locked_until(own, guard.key) or now + lockout
            audit_service.record(
                own,
                guard.ctx,
                guard.action,
                AuditOutcome.DENIED,
                guard.target,
                {"reason": "locked"},
            )
    if attempt is None:
        raise _locked(until, now)

    if verify():
        with session_factory.begin() as own:
            login_throttle.reset(own, guard.key)
        return True

    with session_factory.begin() as own:
        audit_service.record(
            own,
            guard.ctx,
            guard.action,
            AuditOutcome.FAILURE,
            guard.target,
            {"reason": failure_reason, "failed_attempts": attempt},
        )
        if attempt >= settings.lockout_threshold and login_throttle.lock(
            own, guard.key, now + lockout, now
        ):
            _record_locked(own, guard, attempt, settings)
    return False


def _record_locked(own: Session, guard: _Guard, attempts: int, settings: Settings) -> None:
    audit_service.record(
        own,
        guard.ctx,
        AuditAction.AUTH_LOCKED,
        target=guard.target,
        details={"failed_attempts": attempts, "minutes": settings.lockout_minutes},
    )


def _issue_tokens(
    session: Session,
    settings: Settings,
    user: User,
    device: Device,
    family_id: uuid.UUID,
    family_expires_at: datetime,
    now: datetime,
) -> tuple[TokenPairOut, RefreshToken]:
    token, token_hash = new_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        device_id=device.id,
        token_hash=token_hash,
        family_id=family_id,
        created_at=now,
        expires_at=min(
            now + timedelta(days=settings.refresh_token_inactivity_days), family_expires_at
        ),
        family_expires_at=family_expires_at,
    )
    token_repository.add(session, row)
    lifetime = timedelta(minutes=settings.access_token_minutes)
    access = create_access_token(
        AccessClaims(
            user_id=user.id,
            device_id=device.id,
            token_version=user.token_version,
            must_change_password=user.must_change_password,
        ),
        settings.token_secret.get_secret_value(),
        now,
        lifetime,
    )
    pair = TokenPairOut(
        access_token=access,
        refresh_token=token,
        expires_in=int(lifetime.total_seconds()),
        must_change_password=user.must_change_password,
    )
    return pair, row


def _new_family(
    session: Session, settings: Settings, user: User, device: Device, now: datetime
) -> TokenPairOut:
    family_expires_at = now + timedelta(days=settings.refresh_token_absolute_days)
    pair, _ = _issue_tokens(session, settings, user, device, uuid.uuid4(), family_expires_at, now)
    return pair


def login(
    session: Session,
    session_factory: sessionmaker[Session],
    settings: Settings,
    ctx: CallContext,
    body: LoginIn,
    client_version: str,
) -> TokenPairOut:
    login_normalized = normalize_login(body.login)
    user = user_repository.get_by_login(session, login_normalized)
    if user is None:
        # Counted and locked exactly like a real account, so neither the answer (401/423) nor
        # the work done reveals that the account doesn't exist (FR-AUTH-01, decision D1).
        unknown = _Guard(
            key=UnknownLoginKey(hashlib.sha256(login_normalized.encode("utf-8")).hexdigest()),
            ctx=ctx.as_actor(Actor(AuditActorType.ANONYMOUS, label=_login_label(body.login))),
            target=None,
            action=AuditAction.AUTH_LOGIN,
        )

        def no_account() -> bool:
            burn_password_check(body.password)
            return False

        _guarded_password_check(
            session_factory, settings, unknown, no_account, "invalid_credentials"
        )
        raise _invalid_credentials()

    guard = _Guard(
        key=AccountKey(user.id),
        ctx=ctx.as_actor(Actor(AuditActorType.USER, user_id=user.id)),
        target=user_target(user),
        action=AuditAction.AUTH_LOGIN,
    )
    if not _guarded_password_check(
        session_factory,
        settings,
        guard,
        lambda: verify_password(user.password_hash, body.password),
        "invalid_credentials",
    ):
        raise _invalid_credentials()

    if user.status is UserStatus.DISABLED:
        # Only revealed after a correct password, so it doesn't help guess accounts.
        audit_service.record_now(
            session_factory,
            guard.ctx,
            AuditAction.AUTH_LOGIN,
            AuditOutcome.FAILURE,
            guard.target,
            {"reason": "disabled"},
        )
        raise AppError(
            403,
            ErrorCode.ACCOUNT_DISABLED,
            "Account disabled, contact your administrator.",
            audited=True,
        )

    return _start_session(session, settings, ctx, user, body, client_version)


def _start_session(
    session: Session,
    settings: Settings,
    ctx: CallContext,
    user: User,
    body: LoginIn,
    client_version: str,
) -> TokenPairOut:
    now = datetime.now(UTC)
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    device = Device(
        user_id=user.id,
        name=body.device_name,
        os_name=body.device_os,
        client_version=client_version,
        created_at=now,
        last_seen_at=now,
    )
    device_repository.add(session, device)
    pair = _new_family(session, settings, user, device, now)
    audit_service.record(
        session,
        ctx.as_actor(Actor(AuditActorType.USER, user_id=user.id, device_id=device.id)),
        AuditAction.AUTH_LOGIN,
        target=user_target(user),
        details={"device_name": body.device_name, "client_version": client_version},
    )
    return pair


def refresh(
    session: Session, settings: Settings, ctx: CallContext, refresh_token: str, client_version: str
) -> TokenPairOut:
    now = datetime.now(UTC)
    row = token_repository.get_by_hash_for_update(session, hash_refresh_token(refresh_token))
    if row is None:
        raise _unauthenticated()

    if row.replaced_by_id is not None:
        # FR-AUTH-03: a rotated token came back, so it may be stolen. End the whole session.
        # Strict by decision D2: clients must never resend a refresh token they already used.
        # Done in this transaction (it holds the row lock) and committed before raising.
        session_service.end_device_session(session, row.device_id, now)
        token_repository.revoke_family(session, row.family_id, now)
        audit_service.record(
            session,
            ctx.as_actor(Actor(AuditActorType.USER, user_id=row.user_id, device_id=row.device_id)),
            AuditAction.AUTH_REFRESH_REUSE,
            AuditOutcome.DENIED,
            AuditTarget(type="device", id=str(row.device_id)),
            {"family_id": str(row.family_id)},
        )
        session.commit()
        raise _unauthenticated(audited=True)

    user = user_repository.get(session, row.user_id)
    device = device_repository.get(session, row.device_id)
    if (
        row.revoked_at is not None
        or row.expires_at <= now
        or user is None
        or user.status is not UserStatus.ACTIVE
        or device is None
        or device.revoked_at is not None
    ):
        raise _unauthenticated()

    pair, new_row = _issue_tokens(
        session, settings, user, device, row.family_id, row.family_expires_at, now
    )
    row.revoked_at = now
    row.replaced_by_id = new_row.id
    device.last_seen_at = now
    device.client_version = client_version
    return pair


def authenticate(session: Session, settings: Settings, access_token: str) -> CurrentSession:
    """Validate an access token against the current state of the user and device, so disable,
    sign-out, revoke and password changes take effect immediately (FR-AUTH-07, FR-ADMIN-01)."""
    try:
        claims = decode_access_token(access_token, settings.token_secret.get_secret_value())
    except InvalidAccessTokenError:
        raise _unauthenticated() from None
    user = user_repository.get(session, claims.user_id)
    device = device_repository.get(session, claims.device_id)
    if (
        user is None
        or device is None
        or device.user_id != user.id
        or device.revoked_at is not None
        or user.status is not UserStatus.ACTIVE
        or user.token_version != claims.token_version
    ):
        raise _unauthenticated()
    return CurrentSession(user=user, device=device)


def require_password_changed(current: CurrentSession) -> CurrentSession:
    if current.user.must_change_password:
        raise AppError(
            403,
            ErrorCode.PASSWORD_CHANGE_REQUIRED,
            "Change your temporary password to continue.",
        )
    return current


def logout(session: Session, ctx: CallContext, current: CurrentSession) -> None:
    session_service.end_device_session(session, current.device.id, datetime.now(UTC))
    audit_service.record(session, ctx, AuditAction.AUTH_LOGOUT, target=user_target(current.user))


def change_password(
    session: Session,
    session_factory: sessionmaker[Session],
    settings: Settings,
    ctx: CallContext,
    current: CurrentSession,
    body: PasswordChangeIn,
) -> TokenPairOut:
    """FR-AUTH-05: requires the current password, which is guessed under the same lockout as
    sign-in (FR-AUTH-06). Other sessions are signed out; this device keeps working with the
    token pair returned here."""
    user = current.user
    guard = _Guard(
        key=AccountKey(user.id),
        ctx=ctx,
        target=user_target(user),
        action=AuditAction.AUTH_PASSWORD_CHANGED,
    )
    if not _guarded_password_check(
        session_factory,
        settings,
        guard,
        lambda: verify_password(user.password_hash, body.current_password),
        "wrong_current_password",
    ):
        raise _field_error(
            "current_password", "Your current password is incorrect.", "password_incorrect"
        )
    if body.new_password == body.current_password:
        raise _field_error(
            "new_password", "Choose a password different from the current one.", "password_reused"
        )

    now = datetime.now(UTC)
    user.password_hash = hash_password(body.new_password)
    user.must_change_password = False
    user.updated_at = now
    user.token_version += 1  # every existing access token, this device's included, stops working
    session_service.end_all_sessions(session, user, now, except_device_id=current.device.id)
    token_repository.revoke_for_device(session, current.device.id, now)
    pair = _new_family(session, settings, user, current.device, now)
    audit_service.record(session, ctx, AuditAction.AUTH_PASSWORD_CHANGED, target=user_target(user))
    return pair
