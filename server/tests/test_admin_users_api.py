"""User administration from the app (FR-ADMIN-02, FR-ADMIN-07, NFR-SEC-13)."""

import json
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.core.errors import AppError
from dropshare_server.main import create_app
from dropshare_server.models import AuditEvent, User
from dropshare_server.repositories import users as user_repository
from dropshare_server.services import admin_users as admin_service
from dropshare_server.services.context import Actor, CallContext
from dropshare_shared.schemas.admin import AdminUserUpdateIn, EmailOutcome
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome
from dropshare_shared.schemas.users import UserRole, UserStatus

USERS = "/api/v1/admin/users"
GIB = 1024**3
NEW_PASSWORD = "a-long-new-password-42"

type Login = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]


class FakeMailer:
    def __init__(self) -> None:
        self.outcome = EmailOutcome.SENT
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, html: str) -> EmailOutcome:
        self.sent.append((to, subject, html))
        return self.outcome


@pytest.fixture(autouse=True)
def mailer(monkeypatch: pytest.MonkeyPatch) -> FakeMailer:
    """Every app built in these tests sends email through this fake."""
    fake = FakeMailer()
    monkeypatch.setattr("dropshare_server.main.create_mailer", lambda _settings: fake)
    return fake


type MakeUser = Callable[..., tuple[str, str]]
type SignInAs = Callable[..., dict[str, str]]


@pytest.fixture
def sign_in_as(api: TestClient, make_user: MakeUser, login: Login) -> SignInAs:
    """sign_in_as(username, role): a user who signed in and set their own password."""

    def create(username: str, role: UserRole = UserRole.USER) -> dict[str, str]:
        username, temporary = make_user(username, role)
        first = login(username, temporary).json()["data"]
        changed = api.post(
            "/api/v1/me/password",
            json={"current_password": temporary, "new_password": NEW_PASSWORD},
            headers={"Authorization": f"Bearer {first['access_token']}"},
        )
        assert changed.status_code == 200, changed.text
        tokens = changed.json()["data"]
        return {"username": username, "authorization": f"Bearer {tokens['access_token']}", **tokens}

    return create


@pytest.fixture
def admin(sign_in_as: SignInAs) -> dict[str, str]:
    return sign_in_as("root", UserRole.ADMIN)


def _auth(user: dict[str, str]) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _id_of(api: TestClient, admin: dict[str, str], username: str) -> str:
    users = api.get(USERS, headers=_auth(admin)).json()["data"]
    return str(next(user["id"] for user in users if user["username"] == username))


def _new_user(**changes: object) -> dict[str, object]:
    return {
        "username": "bob",
        "email": "bob@example.com",
        "display_name": "Bob",
        "role": "user",
        "quota_bytes": 50 * GIB,
        **changes,
    }


# --- Who may use it ----------------------------------------------------------------------------


def test_admin_endpoints_need_sign_in(api: TestClient) -> None:
    some_id = uuid.uuid4()
    for method, path in [
        ("GET", USERS),
        ("POST", USERS),
        ("PATCH", f"{USERS}/{some_id}"),
        ("POST", f"{USERS}/{some_id}/password-reset"),
    ]:
        response = api.request(method, path, json={})
        assert response.status_code == 401, (method, path)
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_a_plain_user_is_refused_and_it_is_audited(
    api: TestClient, sign_in_as: SignInAs, admin: dict[str, str], audit_events: AuditEvents
) -> None:
    alice = sign_in_as("alice")
    target = _id_of(api, admin, "root")
    for method, path, body in [
        ("GET", USERS, None),
        ("POST", USERS, _new_user()),
        ("PATCH", f"{USERS}/{target}", {"status": "disabled"}),
        ("POST", f"{USERS}/{target}/password-reset", None),
    ]:
        response = api.request(method, path, json=body, headers=_auth(alice))
        assert response.status_code == 403, (method, path)
        assert response.json()["error"]["code"] == "FORBIDDEN"

    denied = audit_events(AuditAction.ACCESS_DENIED)
    assert len(denied) == 4
    assert {event.outcome for event in denied} == {AuditOutcome.DENIED}


def test_a_demoted_admin_loses_access_at_the_next_request(
    api: TestClient, sign_in_as: SignInAs, admin: dict[str, str]
) -> None:
    other = sign_in_as("other", UserRole.ADMIN)
    assert api.get(USERS, headers=_auth(other)).status_code == 200

    demoted = api.patch(
        f"{USERS}/{_id_of(api, admin, 'other')}", json={"role": "user"}, headers=_auth(admin)
    )

    assert demoted.status_code == 200, demoted.text
    assert api.get(USERS, headers=_auth(other)).status_code == 403  # same access token


# --- List --------------------------------------------------------------------------------------


def test_the_list_shows_every_account_with_its_status(
    api: TestClient, admin: dict[str, str], make_user: MakeUser, audit_events: AuditEvents
) -> None:
    make_user("carol")
    api.patch(
        f"{USERS}/{_id_of(api, admin, 'carol')}", json={"status": "disabled"}, headers=_auth(admin)
    )

    response = api.get(USERS, headers=_auth(admin))

    assert response.status_code == 200
    users = {user["username"]: user for user in response.json()["data"]}
    assert set(users) == {"carol", "root"}
    assert (users["carol"]["status"], users["root"]["role"]) == ("disabled", "admin")
    assert {"quota_bytes", "used_bytes", "email", "display_name"} <= set(users["carol"])
    assert not any("password" in key for key in users["carol"])
    listed = audit_events(AuditAction.USER_LISTED)[-1]
    assert (listed.actor_type, listed.actor_label) == (AuditActorType.USER, None)


# --- Create ------------------------------------------------------------------------------------


def test_creating_a_user_shows_and_emails_a_one_time_password(
    api: TestClient,
    admin: dict[str, str],
    login: Login,
    mailer: FakeMailer,
    audit_events: AuditEvents,
) -> None:
    response = api.post(USERS, json=_new_user(), headers=_auth(admin))

    assert response.status_code == 201, response.text
    created = response.json()["data"]
    password = created["temporary_password"]
    assert created["email"] == "sent"
    assert (created["user"]["username"], created["user"]["status"]) == ("bob", "active")
    assert created["user"]["quota_bytes"] == 50 * GIB

    (to, subject, html) = mailer.sent[0]
    assert to == "bob@example.com"
    assert "account" in subject
    assert "bob" in html and password in html

    signed_in = login("bob", password)
    assert signed_in.status_code == 200
    assert signed_in.json()["data"]["must_change_password"] is True

    root_id = _id_of(api, admin, "root")
    created_event = audit_events(AuditAction.USER_CREATED)[-1]
    assert (str(created_event.actor_user_id), created_event.target_name) == (root_id, "bob")
    emailed = audit_events(AuditAction.USER_PASSWORD_EMAILED)[-1]
    assert emailed.outcome is AuditOutcome.SUCCESS
    for event in audit_events():
        assert password not in (event.details or "")


def test_the_email_escapes_what_it_inserts(
    api: TestClient, admin: dict[str, str], mailer: FakeMailer
) -> None:
    response = api.post(
        USERS, json=_new_user(display_name="<script>x</script>"), headers=_auth(admin)
    )

    assert response.status_code == 201
    html = mailer.sent[0][2]
    assert "<script>" not in html  # the display name isn't part of the email at all
    assert "&lt;script&gt;" not in html


@pytest.mark.parametrize("outcome", [EmailOutcome.FAILED, EmailOutcome.NOT_CONFIGURED])
def test_the_account_stands_when_the_email_does_not_go(
    api: TestClient,
    admin: dict[str, str],
    login: Login,
    mailer: FakeMailer,
    audit_events: AuditEvents,
    outcome: EmailOutcome,
) -> None:
    mailer.outcome = outcome

    response = api.post(USERS, json=_new_user(), headers=_auth(admin))

    assert response.status_code == 201
    created = response.json()["data"]
    assert created["email"] == outcome.value
    assert login("bob", created["temporary_password"]).status_code == 200
    emailed = audit_events(AuditAction.USER_PASSWORD_EMAILED)[-1]
    assert emailed.outcome is AuditOutcome.FAILURE
    assert json.loads(emailed.details or "{}") == {"email": outcome.value}


@pytest.mark.parametrize(
    ("changes", "status", "code"),
    [
        ({"username": "ROOT"}, 409, "NAME_CONFLICT"),
        ({"email": "ROOT@example.com"}, 409, "NAME_CONFLICT"),
        ({"username": "no spaces"}, 400, "VALIDATION_ERROR"),
        ({"email": "not-an-email"}, 400, "VALIDATION_ERROR"),
        ({"role": "superuser"}, 400, "VALIDATION_ERROR"),
        ({"quota_bytes": -1}, 400, "VALIDATION_ERROR"),
        ({"password": "chosen"}, 400, "VALIDATION_ERROR"),
    ],
)
def test_create_refuses_bad_or_taken_details(
    api: TestClient,
    admin: dict[str, str],
    mailer: FakeMailer,
    changes: dict[str, object],
    status: int,
    code: str,
) -> None:
    response = api.post(USERS, json=_new_user(**changes), headers=_auth(admin))

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert mailer.sent == []


def test_an_admin_can_create_another_admin(api: TestClient, admin: dict[str, str]) -> None:
    response = api.post(USERS, json=_new_user(role="admin"), headers=_auth(admin))

    assert response.json()["data"]["user"]["role"] == "admin"


# --- Reset password ----------------------------------------------------------------------------


def test_reset_signs_the_user_out_and_emails_a_new_password(
    api: TestClient,
    admin: dict[str, str],
    sign_in_as: SignInAs,
    login: Login,
    mailer: FakeMailer,
    audit_events: AuditEvents,
) -> None:
    alice = sign_in_as("alice")

    response = api.post(
        f"{USERS}/{_id_of(api, admin, 'alice')}/password-reset", headers=_auth(admin)
    )

    assert response.status_code == 200, response.text
    reset = response.json()["data"]
    assert reset["email"] == "sent"
    assert "reset" in mailer.sent[0][1]
    assert api.get("/api/v1/me", headers=_auth(alice)).status_code == 401
    refreshed = api.post("/api/v1/auth/refresh", json={"refresh_token": alice["refresh_token"]})
    assert refreshed.status_code == 401
    assert login("alice", NEW_PASSWORD).status_code == 401
    assert login("alice", reset["temporary_password"]).json()["data"]["must_change_password"]
    assert audit_events(AuditAction.AUTH_PASSWORD_RESET)[-1].target_name == "alice"


def test_reset_of_an_unknown_user_is_not_found(api: TestClient, admin: dict[str, str]) -> None:
    response = api.post(f"{USERS}/{uuid.uuid4()}/password-reset", headers=_auth(admin))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# --- Update ------------------------------------------------------------------------------------


def test_disable_signs_the_user_out_and_enable_lets_them_back(
    api: TestClient,
    admin: dict[str, str],
    sign_in_as: SignInAs,
    login: Login,
    audit_events: AuditEvents,
) -> None:
    alice = sign_in_as("alice")
    alice_id = _id_of(api, admin, "alice")

    disabled = api.patch(f"{USERS}/{alice_id}", json={"status": "disabled"}, headers=_auth(admin))

    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["data"]["status"] == "disabled"
    assert api.get("/api/v1/me", headers=_auth(alice)).status_code == 401
    assert login("alice", NEW_PASSWORD).status_code == 403

    enabled = api.patch(f"{USERS}/{alice_id}", json={"status": "active"}, headers=_auth(admin))

    assert enabled.json()["data"]["status"] == "active"
    assert login("alice", NEW_PASSWORD).status_code == 200
    actions = [event.action for event in audit_events()]
    assert AuditAction.USER_DISABLED in actions and AuditAction.USER_ENABLED in actions


def test_role_change_is_audited_with_old_and_new_role(
    api: TestClient, admin: dict[str, str], sign_in_as: SignInAs, audit_events: AuditEvents
) -> None:
    alice = sign_in_as("alice")

    promoted = api.patch(
        f"{USERS}/{_id_of(api, admin, 'alice')}", json={"role": "admin"}, headers=_auth(admin)
    )

    assert promoted.json()["data"]["role"] == "admin"
    assert api.get(USERS, headers=_auth(alice)).status_code == 200
    event = audit_events(AuditAction.USER_ROLE_CHANGED)[-1]
    assert json.loads(event.details or "{}") == {"old_role": "user", "role": "admin"}


def test_quota_follows_the_cli_rule(
    api: TestClient, admin: dict[str, str], make_user: MakeUser
) -> None:
    make_user("carol")
    carol = _id_of(api, admin, "carol")

    changed = api.patch(f"{USERS}/{carol}", json={"quota_bytes": 2 * GIB}, headers=_auth(admin))
    refused = api.patch(f"{USERS}/{carol}", json={"quota_bytes": -5}, headers=_auth(admin))

    assert changed.json()["data"]["quota_bytes"] == 2 * GIB
    assert refused.status_code == 400


def test_quota_below_what_the_user_stores_is_refused(
    api: TestClient,
    admin: dict[str, str],
    sign_in_as: SignInAs,
    root_of: Callable[[dict[str, str]], str],
    upload_file: Callable[..., object],
) -> None:
    alice = sign_in_as("alice")
    upload_file(alice, b"twelve bytes", parent_id=root_of(alice), name="a.txt")

    response = api.patch(
        f"{USERS}/{_id_of(api, admin, 'alice')}", json={"quota_bytes": 5}, headers=_auth(admin)
    )

    assert response.status_code == 400
    assert "already uses" in response.json()["error"]["message"]


def test_a_failed_part_undoes_the_whole_change(
    api: TestClient, admin: dict[str, str], make_user: MakeUser
) -> None:
    make_user("carol")
    carol = _id_of(api, admin, "carol")

    response = api.patch(
        f"{USERS}/{carol}", json={"status": "disabled", "quota_bytes": -1}, headers=_auth(admin)
    )

    assert response.status_code == 400
    users = {user["username"]: user for user in api.get(USERS, headers=_auth(admin)).json()["data"]}
    assert users["carol"]["status"] == "active"


@pytest.mark.parametrize("change", [{"status": "disabled"}, {"role": "user"}])
def test_an_admin_cannot_change_their_own_status_or_role(
    api: TestClient, admin: dict[str, str], audit_events: AuditEvents, change: dict[str, str]
) -> None:
    response = api.patch(f"{USERS}/{_id_of(api, admin, 'root')}", json=change, headers=_auth(admin))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SELF_CHANGE_NOT_ALLOWED"
    assert audit_events(AuditAction.ACCESS_DENIED)[-1].outcome is AuditOutcome.DENIED
    assert api.get(USERS, headers=_auth(admin)).status_code == 200  # still an enabled admin


def test_an_admin_may_change_their_own_quota(api: TestClient, admin: dict[str, str]) -> None:
    response = api.patch(
        f"{USERS}/{_id_of(api, admin, 'root')}", json={"quota_bytes": GIB}, headers=_auth(admin)
    )

    assert response.status_code == 200


def test_an_empty_or_unknown_change_is_a_validation_error(
    api: TestClient, admin: dict[str, str]
) -> None:
    root = _id_of(api, admin, "root")
    for body in ({}, {"username": "renamed"}):
        response = api.patch(f"{USERS}/{root}", json=body, headers=_auth(admin))
        assert response.status_code == 400, body


def test_an_admin_demoted_meanwhile_cannot_demote_the_other(
    session_factory: sessionmaker[Session], make_user: MakeUser
) -> None:
    """Two admins demoting each other at once: the second, re-checked under the lock, is
    refused, so an enabled admin remains."""
    make_user("anne", UserRole.ADMIN)
    make_user("ben", UserRole.ADMIN)
    with session_factory() as session:
        # Both requests loaded their caller before either change committed.
        anne = _user(session, "anne")
        ben = _user(session, "ben")
    with session_factory.begin() as session:
        ctx = CallContext(actor=Actor(AuditActorType.USER, user_id=ben.id))
        admin_service.update_user(session, ctx, ben, anne.id, AdminUserUpdateIn(role=UserRole.USER))

    with session_factory() as session:
        ctx = CallContext(actor=Actor(AuditActorType.USER, user_id=anne.id))
        with pytest.raises(AppError) as refused:
            admin_service.update_user(
                session, ctx, anne, ben.id, AdminUserUpdateIn(status=UserStatus.DISABLED)
            )
        session.rollback()

    assert refused.value.status_code == 403
    with session_factory() as session:
        assert _user(session, "ben").status is UserStatus.ACTIVE


def _user(session: Session, username: str) -> User:
    user = user_repository.get_by_username(session, username)
    assert user is not None
    return user


# --- Rate limit --------------------------------------------------------------------------------


@pytest.fixture
def limited_api(db_settings: Settings) -> Iterator[TestClient]:
    settings = db_settings.model_copy(update={"admin_rate_limit_per_minute": 2})
    with TestClient(
        create_app(settings), raise_server_exceptions=False, headers={"X-Client-Version": "0.2.0"}
    ) as client:
        yield client


def test_admin_writes_are_rate_limited_but_reading_is_not(
    limited_api: TestClient, make_user: MakeUser, audit_events: AuditEvents
) -> None:
    username, temporary = make_user("root", UserRole.ADMIN)
    first = limited_api.post(
        "/api/v1/auth/login",
        json={"login": username, "password": temporary, "device_name": "PC", "device_os": "Win"},
    ).json()["data"]
    tokens = limited_api.post(
        "/api/v1/me/password",
        json={"current_password": temporary, "new_password": NEW_PASSWORD},
        headers={"Authorization": f"Bearer {first['access_token']}"},
    ).json()["data"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    statuses = [
        limited_api.post(
            USERS, json=_new_user(username=f"u{n}", email=f"u{n}@x.org"), headers=headers
        ).status_code
        for n in range(3)
    ]

    assert statuses == [201, 201, 429]
    refused = limited_api.post(USERS, json=_new_user(), headers=headers)
    assert refused.json()["error"]["code"] == "RATE_LIMITED"
    assert int(refused.headers["Retry-After"]) >= 1
    assert limited_api.get(USERS, headers=headers).status_code == 200
    assert audit_events(AuditAction.ACCESS_DENIED)[-1].outcome is AuditOutcome.DENIED
