"""Namespaces, folders and delete to trash (FR-FILE-04, FR-TRASH-01, FR-AUDIT-01)."""

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, Change, Node
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome

type User = dict[str, str]
type SignIn = Callable[..., User]
type RootOf = Callable[[User], str]
type UploadFile = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _mkdir(api: TestClient, user: User, parent_id: str, name: str) -> Response:
    body = {"parent_id": parent_id, "name": name}
    response: Response = api.post("/api/v1/nodes", json=body, headers=_auth(user))
    return response


def _changes(session_factory: sessionmaker[Session]) -> list[Change]:
    with session_factory() as session:
        return list(session.scalars(select(Change).order_by(Change.id)))


def test_personal_namespace_has_an_empty_root_folder(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()

    namespaces = api.get("/api/v1/namespaces", headers=_auth(alice)).json()["data"]
    root = api.get(f"/api/v1/nodes/{root_of(alice)}", headers=_auth(alice)).json()["data"]
    children = api.get(f"/api/v1/nodes/{root['id']}/children", headers=_auth(alice)).json()

    assert [(ns["kind"], ns["role"]) for ns in namespaces] == [("personal", "owner")]
    assert (root["kind"], root["name"], root["parent_id"]) == ("folder", None, None)
    assert children == {"data": [], "meta": {"page": 1, "limit": 50, "total": 0}}


def test_create_folder_writes_a_change_and_an_audit_event(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()

    response = _mkdir(api, alice, root_of(alice), "Reports")

    assert response.status_code == 201, response.text
    folder = response.json()["data"]
    assert (folder["name"], folder["kind"], folder["revision"]) == ("Reports", "folder", 1)
    [change] = _changes(session_factory)
    assert (str(change.node_id), change.change_type, change.name) == (
        folder["id"],
        "created",
        "Reports",
    )
    [event] = audit_events(AuditAction.FOLDER_CREATED)
    assert (event.target_id, event.target_name, event.outcome) == (
        folder["id"],
        "Reports",
        AuditOutcome.SUCCESS,
    )


def test_names_collide_case_insensitively(
    api: TestClient, sign_in: SignIn, root_of: RootOf, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    first = _mkdir(api, alice, root_of(alice), "Reports").json()["data"]

    response = _mkdir(api, alice, root_of(alice), "REPORTS")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NAME_CONFLICT"
    # Lets a retried create recognise its own earlier success (NFR-REL-04).
    assert response.json()["error"]["details"] == {"node_id": first["id"], "kind": "folder"}
    assert len(_changes(session_factory)) == 1


def test_invalid_names_are_rejected_with_the_reason(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()

    response = _mkdir(api, alice, root_of(alice), "CON.txt")

    assert response.status_code == 400
    [issue] = response.json()["error"]["details"]
    assert (issue["loc"], issue["type"]) == (["body", "name"], "RESERVED")


def test_children_list_folders_first_then_by_name_with_paging(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    root = root_of(alice)
    upload_file(alice, b"a", parent_id=root, name="a.txt")
    _mkdir(api, alice, root, "zeta")
    _mkdir(api, alice, root, "Beta")

    first = api.get(f"/api/v1/nodes/{root}/children?limit=2", headers=_auth(alice)).json()
    second = api.get(f"/api/v1/nodes/{root}/children?limit=2&page=2", headers=_auth(alice)).json()

    assert [node["name"] for node in first["data"] + second["data"]] == ["Beta", "zeta", "a.txt"]
    assert first["meta"] == {"page": 1, "limit": 2, "total": 3}


def test_folder_can_only_be_created_inside_a_folder(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    file = upload_file(alice, b"x", parent_id=root_of(alice), name="notes.txt").json()["data"]

    response = _mkdir(api, alice, file["id"], "inside")

    assert response.status_code == 400


def test_another_users_node_is_not_found_and_the_denial_is_audited(
    api: TestClient, sign_in: SignIn, root_of: RootOf, audit_events: AuditEvents
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    folder = _mkdir(api, alice, root_of(alice), "Private").json()["data"]

    responses = [
        api.get(f"/api/v1/nodes/{folder['id']}", headers=_auth(bob)),
        api.get(f"/api/v1/nodes/{folder['id']}/children", headers=_auth(bob)),
        _mkdir(api, bob, folder["id"], "sneaky"),
        api.delete(f"/api/v1/nodes/{folder['id']}?if_revision=1", headers=_auth(bob)),
    ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    # Same answer as for an id that doesn't exist, so nothing leaks.
    missing = api.get(f"/api/v1/nodes/{uuid.uuid4()}", headers=_auth(bob))
    assert missing.json()["error"] == responses[0].json()["error"]
    denied = audit_events(AuditAction.ACCESS_DENIED)
    assert len(denied) == 4
    actors = {event.actor_user_id for event in denied}
    assert len(actors) == 1 and None not in actors  # all bob


def test_requests_without_a_token_are_rejected(api: TestClient) -> None:
    assert api.get("/api/v1/namespaces").status_code == 401
    assert api.get(f"/api/v1/nodes/{uuid.uuid4()}").status_code == 401


def test_delete_needs_the_current_revision(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Old").json()["data"]

    stale = api.delete(f"/api/v1/nodes/{folder['id']}?if_revision=7", headers=_auth(alice))
    missing = api.delete(f"/api/v1/nodes/{folder['id']}", headers=_auth(alice))

    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"
    assert stale.json()["error"]["details"] == {"current_revision": 1}
    assert missing.status_code == 400


def test_delete_moves_the_whole_folder_to_the_trash(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    root = root_of(alice)
    folder = _mkdir(api, alice, root, "Projects").json()["data"]
    inner = _mkdir(api, alice, folder["id"], "2026").json()["data"]
    file = upload_file(alice, b"plan", parent_id=inner["id"], name="plan.txt").json()["data"]

    response = api.delete(f"/api/v1/nodes/{folder['id']}?if_revision=1", headers=_auth(alice))

    assert response.status_code == 204
    for node_id in (folder["id"], inner["id"], file["id"]):
        assert api.get(f"/api/v1/nodes/{node_id}", headers=_auth(alice)).status_code == 404
    with session_factory() as session:
        trashed = session.scalars(select(Node).where(Node.trash_root_id == uuid.UUID(folder["id"])))
        assert {str(node.id) for node in trashed} == {folder["id"], inner["id"], file["id"]}
    # One journal row for the subtree, with the bumped revision.
    last = _changes(session_factory)[-1]
    assert (str(last.node_id), last.change_type, last.revision) == (folder["id"], "trashed", 2)
    [event] = audit_events(AuditAction.NODE_TRASHED)
    assert (event.target_id, event.details) == (
        folder["id"],
        '{"descendants": 2, "kind": "folder"}',
    )
    # The name is free again for a new item.
    assert _mkdir(api, alice, root, "projects").status_code == 201


def test_root_folder_cannot_be_deleted(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()

    response = api.delete(f"/api/v1/nodes/{root_of(alice)}?if_revision=1", headers=_auth(alice))

    assert response.status_code == 400


def _cursor(api: TestClient, user: User) -> int:
    namespace = api.get("/api/v1/namespaces", headers=_auth(user)).json()["data"][0]["id"]
    page = api.get(f"/api/v1/namespaces/{namespace}/changes", headers=_auth(user)).json()
    cursor: int = page["meta"]["cursor"]
    return cursor


def test_delete_with_a_cursor_is_refused_if_something_inside_changed_since(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    """FR-CONF-03: another device's new file inside isn't taken to the trash unseen."""
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Shared").json()["data"]
    seen = _cursor(api, alice)
    added = upload_file(alice, b"new", parent_id=folder["id"], name="new.txt")
    assert added.status_code == 200

    refused = api.delete(
        f"/api/v1/nodes/{folder['id']}?if_revision=1&if_cursor={seen}", headers=_auth(alice)
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "REVISION_CONFLICT"

    current = _cursor(api, alice)
    allowed = api.delete(
        f"/api/v1/nodes/{folder['id']}?if_revision=1&if_cursor={current}", headers=_auth(alice)
    )
    assert allowed.status_code == 204


def test_delete_with_a_cursor_ignores_changes_elsewhere(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    root = root_of(alice)
    folder = _mkdir(api, alice, root, "A").json()["data"]
    seen = _cursor(api, alice)
    _mkdir(api, alice, root, "B")  # outside the folder

    response = api.delete(
        f"/api/v1/nodes/{folder['id']}?if_revision=1&if_cursor={seen}", headers=_auth(alice)
    )

    assert response.status_code == 204


def test_probing_another_users_trashed_item_is_audited_with_its_target(
    api: TestClient, sign_in: SignIn, root_of: RootOf, audit_events: AuditEvents
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    folder = _mkdir(api, alice, root_of(alice), "Gone").json()["data"]
    api.delete(f"/api/v1/nodes/{folder['id']}?if_revision=1", headers=_auth(alice))

    response = api.get(f"/api/v1/nodes/{folder['id']}", headers=_auth(bob))

    assert response.status_code == 404
    [event] = audit_events(AuditAction.ACCESS_DENIED)
    assert (event.target_type, event.target_id) == ("node", folder["id"])
