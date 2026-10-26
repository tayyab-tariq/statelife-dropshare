"""Change feed and long-poll (FR-SYNC-03). Test settings hold a long-poll for 1 s."""

import threading
import time
import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.services import nodes as node_service
from dropshare_server.services.context import Actor, CallContext
from dropshare_shared.schemas.audit import AuditActorType
from dropshare_shared.schemas.nodes import FolderCreateIn

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type UploadFile = Callable[..., Response]


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _namespace(api: TestClient, user: Account) -> str:
    namespace_id: str = api.get("/api/v1/namespaces", headers=_auth(user)).json()["data"][0]["id"]
    return namespace_id


def _mkdir(api: TestClient, user: Account, parent_id: str, name: str) -> dict[str, object]:
    response = api.post(
        "/api/v1/nodes", json={"parent_id": parent_id, "name": name}, headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    created: dict[str, object] = response.json()["data"]
    return created


def _wait(api: TestClient, user: Account, cursors: dict[str, int]) -> Response:
    response: Response = api.post(
        "/api/v1/changes/wait", json={"cursors": cursors}, headers=_auth(user)
    )
    return response


def test_changes_are_listed_after_the_cursor_in_order(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    root, namespace = root_of(alice), _namespace(api, alice)
    folder = _mkdir(api, alice, root, "A")
    file = upload_file(alice, b"data", parent_id=folder["id"], name="f.txt").json()["data"]
    api.delete(f"/api/v1/nodes/{folder['id']}?if_revision=1", headers=_auth(alice))
    url = f"/api/v1/namespaces/{namespace}/changes"

    first = api.get(f"{url}?limit=2", headers=_auth(alice)).json()
    rest = api.get(f"{url}?cursor={first['meta']['cursor']}", headers=_auth(alice)).json()
    caught_up = api.get(f"{url}?cursor={rest['meta']['cursor']}", headers=_auth(alice)).json()

    assert [(c["type"], c["node_id"]) for c in first["data"] + rest["data"]] == [
        ("created", folder["id"]),
        ("created", file["id"]),
        ("trashed", folder["id"]),
    ]
    assert first["data"][1]["content_hash"] == file["content_hash"]
    assert (first["meta"]["has_more"], rest["meta"]["has_more"]) == (True, False)
    assert caught_up == {"data": [], "meta": {"cursor": rest["meta"]["cursor"], "has_more": False}}


def test_wait_answers_at_once_when_the_client_is_behind(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    namespace = _namespace(api, alice)
    _mkdir(api, alice, root_of(alice), "A")

    response = _wait(api, alice, {namespace: 0})

    assert response.json()["data"] == {"changed": [namespace]}


def test_wait_times_out_empty_when_nothing_changes(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    namespace = _namespace(api, alice)
    _mkdir(api, alice, root_of(alice), "A")
    latest = api.get(f"/api/v1/namespaces/{namespace}/changes", headers=_auth(alice)).json()

    started = time.monotonic()
    response = _wait(api, alice, {namespace: latest["meta"]["cursor"]})

    assert response.status_code == 200
    assert response.json()["data"] == {"changed": []}
    assert time.monotonic() - started >= 0.9


def test_wait_returns_as_soon_as_a_change_lands(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    namespace, root = _namespace(api, alice), root_of(alice)
    user_id = uuid.UUID(api.get("/api/v1/me", headers=_auth(alice)).json()["data"]["id"])

    def other_device_creates_a_folder() -> None:
        time.sleep(0.3)
        with session_factory.begin() as session:
            node_service.create_folder(
                session,
                CallContext(actor=Actor(AuditActorType.USER, user_id=user_id)),
                user_id,
                FolderCreateIn(parent_id=uuid.UUID(root), name="from another device"),
            )

    writer = threading.Thread(target=other_device_creates_a_folder)
    writer.start()
    started = time.monotonic()
    response = _wait(api, alice, {namespace: 0})
    writer.join()

    assert response.json()["data"] == {"changed": [namespace]}
    assert 0.2 < time.monotonic() - started < 0.9


def test_wait_and_changes_refuse_other_users_namespaces(api: TestClient, sign_in: SignIn) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    namespace = _namespace(api, alice)

    wait = _wait(api, bob, {_namespace(api, bob): 0, namespace: 0})
    changes = api.get(f"/api/v1/namespaces/{namespace}/changes", headers=_auth(bob))

    assert (wait.status_code, changes.status_code) == (404, 404)


def test_wait_limits_how_many_namespaces_one_request_watches(
    api: TestClient, sign_in: SignIn
) -> None:
    alice = sign_in()

    response = _wait(api, alice, {str(uuid.uuid4()): 0 for _ in range(101)})

    assert response.status_code == 400
