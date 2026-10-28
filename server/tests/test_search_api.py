"""Finding people and groups to share with: `GET /users/search`, `GET /groups/search`
(FR-SHARE-08, OQ-24)."""

from collections.abc import Callable

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.services import groups as group_service
from dropshare_server.services import users as user_service
from dropshare_server.services.context import cli_context
from dropshare_shared.schemas.users import UserRole

type User = dict[str, str]
type SignIn = Callable[..., User]
type MakeUser = Callable[..., tuple[str, str]]


def _search(api: TestClient, user: User, what: str, q: str) -> list[dict[str, object]]:
    response = api.get(
        f"/api/v1/{what}/search", params={"q": q}, headers={"Authorization": user["authorization"]}
    )
    assert response.status_code == 200, response.text
    hits: list[dict[str, object]] = response.json()["data"]
    return hits


def _person(sessions: sessionmaker[Session], username: str, display_name: str, email: str) -> None:
    with sessions.begin() as session:
        user_service.create_user(
            session,
            cli_context("it-admin"),
            username=username,
            email=email,
            display_name=display_name,
            role=UserRole.USER,
            quota_bytes=0,
        )


def test_users_match_the_start_of_name_username_or_email_ignoring_case(
    api: TestClient, sign_in: SignIn, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    _person(session_factory, "jdoe", "Jane Doe", "jane.doe@example.com")
    _person(session_factory, "bsmith", "Bob Smith", "rob@example.com")

    assert [h["display_name"] for h in _search(api, alice, "users", "JA")] == ["Jane Doe"]
    assert [h["display_name"] for h in _search(api, alice, "users", "bsm")] == ["Bob Smith"]
    assert [h["display_name"] for h in _search(api, alice, "users", "Rob@")] == ["Bob Smith"]
    assert _search(api, alice, "users", "oe") == []  # the start only, not anywhere


def test_hits_carry_only_id_name_and_email(
    api: TestClient, sign_in: SignIn, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    _person(session_factory, "jdoe", "Jane Doe", "jane@example.com")

    [hit] = _search(api, alice, "users", "jane")

    assert set(hit) == {"id", "display_name", "email"}


def test_disabled_users_are_never_found(
    api: TestClient, sign_in: SignIn, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    _person(session_factory, "jdoe", "Jane Doe", "jane@example.com")
    with session_factory.begin() as session:
        user_service.set_disabled(session, cli_context("it-admin"), "jdoe", True)

    assert _search(api, alice, "users", "jane") == []


def test_at_most_twenty_results(
    api: TestClient, sign_in: SignIn, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    for number in range(25):
        _person(session_factory, f"temp{number:02}", f"Temp {number:02}", f"t{number}@example.com")

    assert len(_search(api, alice, "users", "temp")) == 20


def test_like_wildcards_in_the_query_match_literally(
    api: TestClient, sign_in: SignIn, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    _person(session_factory, "jdoe", "Jane Doe", "jane@example.com")
    _person(session_factory, "a_b", "A_B Team", "ab@example.com")

    assert _search(api, alice, "users", "%a") == []
    assert [h["display_name"] for h in _search(api, alice, "users", "a_")] == ["A_B Team"]


def test_groups_match_by_name_with_their_member_count(
    api: TestClient, sign_in: SignIn, make_user: MakeUser, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    make_user("bob")
    ctx = cli_context("it-admin")
    with session_factory.begin() as session:
        group_service.create_group(session, ctx, "Finance")
        group_service.create_group(session, ctx, "Field team")
        group_service.create_group(session, ctx, "Sales")
        group_service.add_member(session, ctx, "Finance", "bob")
        group_service.add_member(session, ctx, "Finance", "alice")

    hits = _search(api, alice, "groups", "fi")

    assert [(h["name"], h["member_count"]) for h in hits] == [("Field team", 0), ("Finance", 2)]
    assert set(hits[0]) == {"id", "name", "member_count"}


def test_a_query_shorter_than_two_characters_is_refused(api: TestClient, sign_in: SignIn) -> None:
    alice = sign_in()
    headers = {"Authorization": alice["authorization"]}

    for what in ("users", "groups"):
        for q in ("a", " a "):
            response = api.get(f"/api/v1/{what}/search", params={"q": q}, headers=headers)
            assert response.status_code == 400, (what, q)
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_needs_a_signed_in_user(api: TestClient) -> None:
    for what in ("users", "groups"):
        response = api.get(f"/api/v1/{what}/search", params={"q": "ja"})
        assert response.status_code == 401
