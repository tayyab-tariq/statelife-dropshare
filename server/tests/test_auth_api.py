"""Sign-in, sessions, lockout and password change (FR-AUTH-01..06, FR-AUDIT-01)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.models import AuditEvent, Device, RefreshToken, User
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome

type Login = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]
type MakeUser = Callable[..., tuple[str, str]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _refresh(api: TestClient, token: str) -> Response:
    response: Response = api.post("/api/v1/auth/refresh", json={"refresh_token": token})
    return response


# --- FR-AUTH-01: sign in -------------------------------------------------------------------


def test_sign_in_with_username_or_email(
    login: Login, make_user: MakeUser, audit_events: AuditEvents
) -> None:
    username, password = make_user("alice")

    by_name = login(username, password)
    by_email = login("ALICE@Example.com", password)

    for response in (by_name, by_email):
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["access_token"] and data["refresh_token"]
        assert data["expires_in"] == 15 * 60
        assert data["must_change_password"] is True
    (first, _) = audit_events(AuditAction.AUTH_LOGIN)
    assert first.outcome is AuditOutcome.SUCCESS
    assert first.device_id is not None
    assert password not in (first.details or "")


def test_wrong_password_and_unknown_user_get_the_same_answer(
    login: Login, make_user: MakeUser, audit_events: AuditEvents
) -> None:
    username, _ = make_user()

    wrong_password = login(username, "not-the-password")
    unknown_user = login("nobody", "not-the-password")

    for response in (wrong_password, unknown_user):
        assert response.status_code == 401
        assert response.json()["error"] == {
            "code": "UNAUTHENTICATED",
            "message": "Invalid username or password.",
            "details": None,
        }
    failures = audit_events(AuditAction.AUTH_LOGIN)
    assert [event.outcome for event in failures] == [AuditOutcome.FAILURE] * 2
    assert failures[1].actor_type is AuditActorType.ANONYMOUS
    # A non-email login may be a mistyped password, so it's masked (FR-AUDIT-02).
    assert failures[1].actor_label == "n…y (6 chars)"
    # Only the explicit sign-in failures: no extra generic access-denied rows.
    assert audit_events(AuditAction.ACCESS_DENIED) == []


def test_disabled_account_is_told_only_after_correct_password(
    login: Login, make_user: MakeUser, cli: Callable[..., tuple[int, str, str]]
) -> None:
    username, password = make_user()
    assert cli("users", "disable", username)[0] == 0

    assert login(username, "wrong-password").json()["error"]["code"] == "UNAUTHENTICATED"
    response = login(username, password)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"
    assert "contact your administrator" in response.json()["error"]["message"]


# --- FR-AUTH-06: lockout -------------------------------------------------------------------


def test_five_failures_lock_the_account_even_for_the_right_password(
    login: Login, make_user: MakeUser, audit_events: AuditEvents
) -> None:
    username, password = make_user()

    for _ in range(5):
        assert login(username, "wrong-password").status_code == 401
    locked = login(username, password)

    assert locked.status_code == 423
    assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"
    assert len(audit_events(AuditAction.AUTH_LOCKED)) == 1
    assert audit_events(AuditAction.AUTH_LOGIN)[-1].outcome is AuditOutcome.DENIED


def test_lock_expires_after_the_lockout_period(
    login: Login,
    make_user: MakeUser,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    username, password = make_user()
    for _ in range(5):
        login(username, "wrong-password")
    with session_factory.begin() as session:
        session.execute(update(User).values(locked_until=datetime.now(UTC) - timedelta(seconds=1)))

    assert login(username, password).status_code == 200
    assert len(audit_events(AuditAction.AUTH_UNLOCKED)) == 1


def test_successful_sign_in_resets_the_failure_count(login: Login, make_user: MakeUser) -> None:
    username, password = make_user()
    for _ in range(4):
        login(username, "wrong-password")
    assert login(username, password).status_code == 200

    for _ in range(4):
        login(username, "wrong-password")

    assert login(username, password).status_code == 200


# --- FR-AUTH-05 + first sign-in: forced password change ------------------------------------


def test_temporary_password_must_be_changed_before_using_the_api(
    api: TestClient, login: Login, make_user: MakeUser
) -> None:
    username, temporary = make_user()
    tokens = login(username, temporary).json()["data"]

    blocked = api.get("/api/v1/me", headers=_bearer(tokens["access_token"]))
    changed = api.post(
        "/api/v1/me/password",
        json={"current_password": temporary, "new_password": "a-long-new-password-42"},
        headers=_bearer(tokens["access_token"]),
    )
    me = api.get("/api/v1/me", headers=_bearer(changed.json()["data"]["access_token"]))

    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "PASSWORD_CHANGE_REQUIRED"
    assert changed.status_code == 200
    assert changed.json()["data"]["must_change_password"] is False
    assert me.status_code == 200
    assert me.json()["data"]["username"] == username
    assert "password_hash" not in me.text


def test_password_change_signs_out_every_other_session(
    api: TestClient, login: Login, signed_in: dict[str, str], audit_events: AuditEvents
) -> None:
    other = login(signed_in["username"], signed_in["password"], device="Desktop").json()["data"]

    changed = api.post(
        "/api/v1/me/password",
        json={"current_password": signed_in["password"], "new_password": "another-long-pass-99"},
        headers=_bearer(signed_in["access_token"]),
    )

    assert changed.status_code == 200
    new_tokens = changed.json()["data"]
    assert api.get("/api/v1/me", headers=_bearer(new_tokens["access_token"])).status_code == 200
    # Every earlier token, on this device and the other one, stops working.
    assert api.get("/api/v1/me", headers=_bearer(signed_in["access_token"])).status_code == 401
    assert api.get("/api/v1/me", headers=_bearer(other["access_token"])).status_code == 401
    assert _refresh(api, other["refresh_token"]).status_code == 401
    assert _refresh(api, signed_in["refresh_token"]).status_code == 401
    assert _refresh(api, new_tokens["refresh_token"]).status_code == 200
    assert login(signed_in["username"], "another-long-pass-99").status_code == 200
    changes = audit_events(AuditAction.AUTH_PASSWORD_CHANGED)
    assert changes[-1].outcome is AuditOutcome.SUCCESS


def test_password_change_needs_the_current_password(
    api: TestClient, signed_in: dict[str, str], audit_events: AuditEvents
) -> None:
    response = api.post(
        "/api/v1/me/password",
        json={"current_password": "wrong-password", "new_password": "another-long-pass-99"},
        headers=_bearer(signed_in["access_token"]),
    )

    assert response.status_code == 400
    assert response.json()["error"]["details"] == [
        {
            "loc": ["body", "current_password"],
            "msg": "Your current password is incorrect.",
            "type": "password_incorrect",
        }
    ]
    assert audit_events(AuditAction.AUTH_PASSWORD_CHANGED)[-1].outcome is AuditOutcome.FAILURE
    # The session survives a typo in the current password.
    assert api.get("/api/v1/me", headers=_bearer(signed_in["access_token"])).status_code == 200


def test_new_password_must_meet_the_length_policy(
    api: TestClient, signed_in: dict[str, str]
) -> None:
    for new_password in ("short-11chr", "x" * 129):
        response = api.post(
            "/api/v1/me/password",
            json={"current_password": signed_in["password"], "new_password": new_password},
            headers=_bearer(signed_in["access_token"]),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert new_password not in response.text


# --- FR-AUTH-03: refresh rotation and reuse detection --------------------------------------


def test_refresh_rotates_the_token(api: TestClient, signed_in: dict[str, str]) -> None:
    rotated = _refresh(api, signed_in["refresh_token"])

    assert rotated.status_code == 200
    data = rotated.json()["data"]
    assert data["refresh_token"] != signed_in["refresh_token"]
    assert api.get("/api/v1/me", headers=_bearer(data["access_token"])).status_code == 200


def test_reusing_a_rotated_token_revokes_the_whole_family(
    api: TestClient,
    signed_in: dict[str, str],
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    rotated = _refresh(api, signed_in["refresh_token"]).json()["data"]

    reused = _refresh(api, signed_in["refresh_token"])

    assert reused.status_code == 401
    assert _refresh(api, rotated["refresh_token"]).status_code == 401
    assert api.get("/api/v1/me", headers=_bearer(rotated["access_token"])).status_code == 401
    (event,) = audit_events(AuditAction.AUTH_REFRESH_REUSE)
    assert event.outcome is AuditOutcome.DENIED
    with session_factory() as session:
        assert all(row.revoked_at for row in session.scalars(select(RefreshToken)))


def test_expired_refresh_token_is_rejected(
    api: TestClient, signed_in: dict[str, str], session_factory: sessionmaker[Session]
) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(RefreshToken).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )

    assert _refresh(api, signed_in["refresh_token"]).status_code == 401


def test_rotation_never_extends_past_the_absolute_limit(
    api: TestClient, signed_in: dict[str, str], session_factory: sessionmaker[Session]
) -> None:
    rotated = _refresh(api, signed_in["refresh_token"])

    assert rotated.status_code == 200
    with session_factory() as session:
        for row in session.scalars(select(RefreshToken)):
            assert row.expires_at <= row.family_expires_at
            assert row.family_expires_at - row.created_at <= timedelta(days=90)


def test_unknown_refresh_token_is_rejected(api: TestClient) -> None:
    assert _refresh(api, "made-up-token").status_code == 401


# --- FR-AUTH-04: sign out ------------------------------------------------------------------


def test_sign_out_ends_the_device_session(
    api: TestClient,
    signed_in: dict[str, str],
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    response = api.post("/api/v1/auth/logout", headers=_bearer(signed_in["access_token"]))

    assert response.status_code == 204
    assert _refresh(api, signed_in["refresh_token"]).status_code == 401
    assert api.get("/api/v1/me", headers=_bearer(signed_in["access_token"])).status_code == 401
    assert len(audit_events(AuditAction.AUTH_LOGOUT)) == 1
    with session_factory() as session:
        assert all(device.revoked_at for device in session.scalars(select(Device)))


# --- NFR-SEC-02 and FR-AUDIT-01: authentication required, denials audited -----------------


def test_requests_without_a_valid_token_are_rejected_and_audited(
    api: TestClient, signed_in: dict[str, str], audit_events: AuditEvents
) -> None:
    tampered = signed_in["access_token"][:-4] + "AAAA"

    for headers in ({}, {"Authorization": "Basic abc"}, _bearer(tampered)):
        response = api.get("/api/v1/me", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    denied = audit_events(AuditAction.ACCESS_DENIED)
    assert len(denied) == 3
    assert all(event.outcome is AuditOutcome.DENIED for event in denied)
    assert all(event.request_id for event in denied)


def test_disabling_a_user_cuts_off_existing_tokens_immediately(
    api: TestClient, signed_in: dict[str, str], cli: Callable[..., tuple[int, str, str]]
) -> None:
    cli("users", "disable", signed_in["username"])

    assert api.get("/api/v1/me", headers=_bearer(signed_in["access_token"])).status_code == 401
    assert _refresh(api, signed_in["refresh_token"]).status_code == 401


# --- FR-UPD-04: client version -------------------------------------------------------------


def test_client_version_is_required_and_enforced(
    api: TestClient, signed_in: dict[str, str], db_settings: Settings
) -> None:
    headers = _bearer(signed_in["access_token"])

    missing = api.get("/api/v1/me", headers={**headers, "X-Client-Version": ""})
    malformed = api.get("/api/v1/me", headers={**headers, "X-Client-Version": "1.2"})
    too_old = api.get("/api/v1/me", headers={**headers, "X-Client-Version": "0.0.9"})
    newer = api.get("/api/v1/me", headers={**headers, "X-Client-Version": "0.10.0"})

    assert missing.status_code == 400
    assert malformed.status_code == 400
    assert too_old.status_code == 426
    assert too_old.json()["error"]["code"] == "UPGRADE_REQUIRED"
    assert too_old.json()["error"]["details"] == {"min_version": db_settings.min_client_version}
    assert newer.status_code == 200  # numeric comparison, not string comparison


def test_health_needs_no_client_version(api: TestClient) -> None:
    assert api.get("/health", headers={"X-Client-Version": ""}).status_code == 200


# --- FR-AUDIT-02: no secrets in audit rows -------------------------------------------------


def test_audit_rows_never_contain_passwords_or_tokens(
    api: TestClient, signed_in: dict[str, str], login: Login, audit_events: AuditEvents
) -> None:
    login(signed_in["username"], "wrong-password-xyz")
    _refresh(api, signed_in["refresh_token"])

    secrets = (signed_in["password"], "wrong-password-xyz", signed_in["refresh_token"])
    for event in audit_events():
        row = " ".join(str(value) for value in vars(event).values())
        assert not any(secret in row for secret in secrets)
        assert signed_in["access_token"] not in row
