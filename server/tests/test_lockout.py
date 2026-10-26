"""Lockout (FR-AUTH-06): race-free, identical for unknown logins (FR-AUTH-01), and covering
password change (FR-AUTH-05)."""

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError
from dropshare_server.models import AuditEvent, LoginThrottle
from dropshare_server.services import auth as auth_service
from dropshare_server.services.context import ANONYMOUS, CallContext
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.auth import LoginIn

type Login = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]
type MakeUser = Callable[..., tuple[str, str]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_parallel_guesses_get_exactly_the_threshold_of_password_checks(
    db_settings: Settings,
    session_factory: sessionmaker[Session],
    make_user: MakeUser,
    audit_events: AuditEvents,
) -> None:
    username, _ = make_user()
    body = LoginIn(login=username, password="wrong-password", device_name="d", device_os="o")

    def attempt(_: int) -> int:
        with session_factory() as session:
            try:
                auth_service.login(
                    session, session_factory, db_settings, CallContext(ANONYMOUS), body, "0.1.0"
                )
            except AppError as exc:
                return exc.status_code
        return 200

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(attempt, range(8)))

    assert sorted(codes) == [401] * 5 + [423] * 3
    checked = [e for e in audit_events(AuditAction.AUTH_LOGIN) if e.outcome is AuditOutcome.FAILURE]
    assert len(checked) == 5
    assert len(audit_events(AuditAction.AUTH_LOCKED)) == 1


def test_unknown_logins_lock_exactly_like_real_accounts(login: Login, make_user: MakeUser) -> None:
    username, _ = make_user()

    real = [login(username, "wrong-password") for _ in range(6)]
    unknown = [login("no.such.user", "wrong-password") for _ in range(6)]

    assert [r.status_code for r in real] == [401] * 5 + [423]
    assert [r.status_code for r in unknown] == [401] * 5 + [423]
    for first, second in zip(real, unknown, strict=True):
        assert first.json()["error"]["message"] == second.json()["error"]["message"]
        assert first.json()["error"]["code"] == second.json()["error"]["code"]


def test_lock_response_tells_the_client_when_to_retry(login: Login, make_user: MakeUser) -> None:
    username, password = make_user()
    for _ in range(5):
        login(username, "wrong-password")

    locked = login(username, password)

    details = locked.json()["error"]["details"]
    assert locked.status_code == 423
    assert 0 < details["retry_after_seconds"] <= 15 * 60
    assert datetime.fromisoformat(details["locked_until"]).tzinfo is not None
    assert locked.headers["Retry-After"] == str(details["retry_after_seconds"])


def test_unknown_logins_are_stored_only_as_hashes(
    login: Login, session_factory: sessionmaker[Session]
) -> None:
    login("Mystery.Person", "wrong-password")

    with session_factory() as session:
        (row,) = session.scalars(select(LoginThrottle))
    assert row.login_hash == hashlib.sha256(b"mystery.person").hexdigest()
    assert row.failed_login_count == 1


def test_wrong_current_passwords_count_toward_the_lockout(
    api: TestClient, signed_in: dict[str, str], login: Login
) -> None:
    headers = _bearer(signed_in["access_token"])

    def change(current: str) -> Response:
        response: Response = api.post(
            "/api/v1/me/password",
            json={"current_password": current, "new_password": "another-long-pass-99"},
            headers=headers,
        )
        return response

    attempts = [change("wrong-password").status_code for _ in range(6)]

    assert attempts == [400] * 5 + [423]
    # Even the right current password is refused while locked, and so is signing in.
    assert change(signed_in["password"]).status_code == 423
    assert login(signed_in["username"], signed_in["password"]).status_code == 423


def test_new_password_equal_to_current_points_at_the_field(
    api: TestClient, signed_in: dict[str, str]
) -> None:
    response = api.post(
        "/api/v1/me/password",
        json={"current_password": signed_in["password"], "new_password": signed_in["password"]},
        headers=_bearer(signed_in["access_token"]),
    )

    assert response.status_code == 400
    (issue,) = response.json()["error"]["details"]
    assert (issue["loc"], issue["type"]) == (["body", "new_password"], "password_reused")


def test_email_shaped_unknown_login_is_kept_for_the_audit(
    login: Login, audit_events: AuditEvents
) -> None:
    login("someone@example.com", "wrong-password")

    (event,) = audit_events(AuditAction.AUTH_LOGIN)
    assert event.actor_label == "someone@example.com"
