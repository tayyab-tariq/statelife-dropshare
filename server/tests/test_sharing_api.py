"""Sharing folders: members, Shared with me, mounts, and the one effective-role rule
(FR-SHARE-01/02/04/06, FR-AUDIT-01, OQ-30, OQ-31, OQ-33)."""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, Node, User
from dropshare_server.services import groups as group_service
from dropshare_server.services.context import cli_context
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.sharing import SharingOut

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type UploadFile = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]
type Json = dict[str, object]


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _ok(response: Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    if status == 204:
        return {}
    data: Json = response.json()["data"]
    return data


def _items(response: Response) -> list[Json]:
    assert response.status_code == 200, response.text
    items: list[Json] = response.json()["data"]
    return items


def _members(api: TestClient, user: Account, node_id: object) -> list[Json]:
    response = api.get(f"/api/v1/nodes/{node_id}/members", headers=_auth(user))
    assert response.status_code == 200, response.text
    members: list[Json] = response.json()["data"]["members"]
    return members


def _mkdir(api: TestClient, user: Account, parent_id: object, name: str) -> Json:
    body = {"parent_id": str(parent_id), "name": name}
    return _ok(api.post("/api/v1/nodes", json=body, headers=_auth(user)), 201)


def _user_id(api: TestClient, user: Account) -> str:
    return str(_ok(api.get("/api/v1/me", headers=_auth(user)))["id"])


def _share(
    api: TestClient, owner: Account, node_id: object, role: str = "editor", **grantee: str
) -> Response:
    response: Response = api.post(
        f"/api/v1/nodes/{node_id}/members", json={**grantee, "role": role}, headers=_auth(owner)
    )
    return response


def _namespaces(api: TestClient, user: Account) -> list[Json]:
    data: list[Json] = api.get("/api/v1/namespaces", headers=_auth(user)).json()["data"]
    return data


def _children(api: TestClient, user: Account, node_id: object) -> Response:
    response: Response = api.get(f"/api/v1/nodes/{node_id}/children", headers=_auth(user))
    return response


def _changes(api: TestClient, user: Account, namespace_id: object, cursor: int = 0) -> list[Json]:
    response = api.get(
        f"/api/v1/namespaces/{namespace_id}/changes",
        params={"cursor": cursor},
        headers=_auth(user),
    )
    return _items(response)


def _mount(api: TestClient, user: Account, namespace_id: object, **body: str) -> Response:
    response: Response = api.post(
        "/api/v1/mounts", json={"namespace_id": str(namespace_id), **body}, headers=_auth(user)
    )
    return response


@pytest.fixture
def budget(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> tuple[Account, Json, Json]:
    """Alice's folder Projects/Budget holding plan.txt and Q1/, not shared yet."""
    alice = sign_in("alice")
    projects = _mkdir(api, alice, root_of(alice), "Projects")
    folder = _mkdir(api, alice, projects["id"], "Budget")
    plan = _ok(upload_file(alice, b"plan", parent_id=folder["id"], name="plan.txt"))
    _mkdir(api, alice, folder["id"], "Q1")
    return alice, folder, plan


def _shared_with(
    api: TestClient, budget: tuple[Account, Json, Json], other: Account, role: str = "editor"
) -> str:
    """Share Budget with `other`; returns the shared namespace's ID."""
    alice, folder, _ = budget
    _ok(_share(api, alice, folder["id"], role, user_id=_user_id(api, other)), 201)
    shared = [n for n in _namespaces(api, alice) if n["kind"] == "shared"]
    return str(shared[0]["id"])


def test_sharing_a_folder_keeps_its_ids_and_its_place_for_the_owner(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], root_of: RootOf
) -> None:
    alice, folder, plan = budget
    bob = sign_in("bob")
    personal = _namespaces(api, alice)[0]["id"]
    before = _changes(api, alice, personal)[-1]["cursor"]

    namespace = _shared_with(api, budget, bob)

    shared = next(n for n in _namespaces(api, alice) if n["id"] == namespace)
    assert (shared["root_node_id"], shared["role"]) == (folder["id"], "owner")
    projects = _items(_children(api, alice, root_of(alice)))
    [mount] = _items(_children(api, alice, projects[0]["id"]))
    assert (mount["name"], mount["mount_namespace_id"]) == ("Budget", namespace)
    assert mount["id"] != folder["id"]
    names = [c["name"] for c in _items(_children(api, alice, mount["id"]))]
    assert names == ["Q1", "plan.txt"]  # the mount lists the shared folder's contents
    owner_rows = _changes(api, alice, personal, int(str(before)))
    assert [(c["type"], c["node_id"]) for c in owner_rows] == [
        ("removed", folder["id"]),
        ("created", mount["id"]),
    ]
    assert owner_rows[1]["mount_namespace_id"] == namespace
    shared_rows = _changes(api, alice, namespace)
    assert shared_rows[0]["node_id"] == folder["id"] and shared_rows[0]["parent_id"] is None
    assert {c["node_id"] for c in shared_rows} >= {folder["id"], plan["id"]}
    assert {c["type"] for c in shared_rows} == {"created"}


def test_a_recipient_sees_it_in_shared_with_me_and_adds_it_to_their_dropshare(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    root_of: RootOf,
    audit_events: AuditEvents,
) -> None:
    _alice, folder, _ = budget
    bob = sign_in("bob")
    namespace = _shared_with(api, budget, bob, "viewer")

    [item] = api.get("/api/v1/shared-with-me", headers=_auth(bob)).json()["data"]
    assert (item["name"], item["kind"], item["role"]) == ("Budget", "folder", "viewer")
    assert (item["namespace_id"], item["node_id"], item["mount_node_id"]) == (
        namespace,
        folder["id"],
        None,
    )
    assert item["owner"]["display_name"] == "Alice"
    assert namespace not in [n["id"] for n in _namespaces(api, bob)]  # not synced until added

    mount = _ok(_mount(api, bob, namespace), 201)

    assert (mount["parent_id"], mount["name"]) == (root_of(bob), "Budget")
    assert [n["role"] for n in _namespaces(api, bob) if n["id"] == namespace] == ["viewer"]
    again = api.get("/api/v1/shared-with-me", headers=_auth(bob)).json()["data"][0]
    assert again["mount_node_id"] == mount["id"]
    assert _ok(_mount(api, bob, namespace), 201)["id"] == mount["id"]  # adding twice: the same
    assert len(audit_events(AuditAction.SHARE_ACCEPTED)) == 1


def test_a_name_already_used_at_the_top_gets_a_number(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], root_of: RootOf
) -> None:
    bob = sign_in("bob")
    _mkdir(api, bob, root_of(bob), "budget")
    namespace = _shared_with(api, budget, bob)

    assert _ok(_mount(api, bob, namespace), 201)["name"] == "Budget (1)"


def test_editors_change_the_shared_folder_and_the_owner_pays_for_it(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
) -> None:
    _alice, folder, _ = budget
    bob = sign_in("bob")
    mount = _ok(_mount(api, bob, _shared_with(api, budget, bob, "editor")), 201)

    created = _mkdir(api, bob, mount["id"], "From Bob")  # through the mount
    uploaded = _ok(upload_file(bob, b"bob's notes", parent_id=mount["id"], name="notes.txt"))

    assert created["parent_id"] == folder["id"] and uploaded["parent_id"] == folder["id"]
    with session_factory() as session:
        used = {u.username: u.used_bytes for u in session.scalars(select(User))}
    assert used == {"alice": len(b"plan") + len(b"bob's notes"), "bob": 0}  # FR-SHARE-06


def test_viewers_can_read_but_get_403_on_every_write(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    root_of: RootOf,
) -> None:
    _alice, folder, plan = budget
    bob = sign_in("bob")
    mount = _ok(_mount(api, bob, _shared_with(api, budget, bob, "viewer")), 201)
    headers = _auth(bob)

    assert _children(api, bob, mount["id"]).status_code == 200
    assert api.get(f"/api/v1/nodes/{plan['id']}/content", headers=headers).status_code == 200
    writes = [
        api.post("/api/v1/nodes", json={"parent_id": mount["id"], "name": "x"}, headers=headers),
        api.post(
            "/api/v1/uploads",
            json={"parent_id": folder["id"], "name": "x.txt", "size_bytes": 1, "sha256": "0" * 64},
            headers=headers,
        ),
        api.post(
            "/api/v1/uploads",
            json={"node_id": plan["id"], "base_revision": 1, "size_bytes": 1, "sha256": "0" * 64},
            headers=headers,
        ),
        api.patch(
            f"/api/v1/nodes/{plan['id']}", json={"if_revision": 1, "name": "y.txt"}, headers=headers
        ),
        api.delete(f"/api/v1/nodes/{plan['id']}", params={"if_revision": 1}, headers=headers),
    ]
    assert [w.status_code for w in writes] == [403] * len(writes)
    assert {w.json()["error"]["code"] for w in writes} == {"FORBIDDEN"}


def test_people_without_access_get_404_everywhere(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    audit_events: AuditEvents,
) -> None:
    _alice, folder, plan = budget
    bob, carol = sign_in("bob"), sign_in("carol")
    namespace = _shared_with(api, budget, bob)
    headers = _auth(carol)

    responses = [
        _children(api, carol, folder["id"]),
        api.get(f"/api/v1/nodes/{plan['id']}/content", headers=headers),
        api.get(f"/api/v1/nodes/{folder['id']}/members", headers=headers),
        api.get(f"/api/v1/namespaces/{namespace}/changes", headers=headers),
        _mount(api, carol, namespace),
        _share(api, carol, folder["id"], user_id=_user_id(api, carol)),
    ]

    assert [r.status_code for r in responses] == [404] * len(responses)
    assert len(audit_events(AuditAction.ACCESS_DENIED)) == len(responses)
    assert api.get("/api/v1/shared-with-me", headers=headers).json()["data"] == []


def test_only_the_owner_manages_sharing(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json]
) -> None:
    _alice, folder, _ = budget
    bob, carol = sign_in("bob"), sign_in("carol")
    _shared_with(api, budget, bob, "editor")
    response = api.get(f"/api/v1/nodes/{folder['id']}/members", headers=_auth(bob))
    sharing = SharingOut.model_validate(response.json()["data"])
    [member] = sharing.members

    reshare = _share(api, bob, folder["id"], "viewer", user_id=_user_id(api, carol))
    promote = api.patch(f"/api/v1/members/{member.id}", json={"role": "viewer"}, headers=_auth(bob))
    remove = api.delete(f"/api/v1/members/{member.id}", headers=_auth(bob))
    stranger = api.delete(f"/api/v1/members/{member.id}", headers=_auth(carol))

    assert sharing.can_manage is False
    assert sharing.owner.display_name == "Alice"
    assert (member.name, member.role, member.kind) == ("Bob", "editor", "user")
    assert [r.status_code for r in (reshare, promote, remove)] == [403, 403, 403]
    assert stranger.status_code == 404


def test_sharing_mistakes_are_refused_with_the_reason(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], root_of: RootOf
) -> None:
    alice, folder, _ = budget
    bob = sign_in("bob")
    bob_id = _user_id(api, bob)
    _shared_with(api, budget, bob)
    q1 = next(c for c in _items(_children(api, alice, folder["id"])) if c["name"] == "Q1")

    cases = {
        "again": _share(api, alice, folder["id"], user_id=bob_id),
        "self": _share(api, alice, folder["id"], user_id=_user_id(api, alice)),
        "nested": _share(api, alice, q1["id"], user_id=bob_id),
        "root": _share(api, alice, root_of(alice), user_id=bob_id),
        "both": _share(api, alice, folder["id"], user_id=bob_id, group_id=bob_id),
    }

    codes = {name: (r.status_code, r.json()["error"]["code"]) for name, r in cases.items()}
    assert codes == {
        "again": (409, "ALREADY_SHARED"),
        "self": (409, "SHARE_NOT_ALLOWED"),
        "nested": (409, "SHARE_NOT_ALLOWED"),
        "root": (409, "SHARE_NOT_ALLOWED"),
        "both": (400, "VALIDATION_ERROR"),
    }


def test_a_folder_holding_a_shared_folder_cant_be_shared(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], root_of: RootOf
) -> None:
    alice, _, _ = budget
    bob = sign_in("bob")
    _shared_with(api, budget, bob)
    [projects] = _items(_children(api, alice, root_of(alice)))

    response = _share(api, alice, projects["id"], user_id=_user_id(api, bob))

    assert response.json()["error"]["code"] == "SHARE_NOT_ALLOWED"


def test_changing_a_role_takes_effect_at_once(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], audit_events: AuditEvents
) -> None:
    alice, folder, _ = budget
    bob = sign_in("bob")
    mount = _ok(_mount(api, bob, _shared_with(api, budget, bob, "editor")), 201)
    [member] = _members(api, alice, folder["id"])

    changed = _ok(
        api.patch(f"/api/v1/members/{member['id']}", json={"role": "viewer"}, headers=_auth(alice))
    )

    assert changed["role"] == "viewer"
    refused = api.post(
        "/api/v1/nodes", json={"parent_id": mount["id"], "name": "x"}, headers=_auth(bob)
    )
    assert refused.status_code == 403
    [event] = audit_events(AuditAction.SHARE_CHANGED)
    assert event.target_name == "Budget"
    assert '"old_role": "editor"' in (event.details or "")


def _wait(api: TestClient, user: Account, cursors: dict[str, int]) -> Response:
    response: Response = api.post(
        "/api/v1/changes/wait", json={"cursors": cursors}, headers=_auth(user)
    )
    return response


def test_removing_someone_trashes_their_mount_and_wakes_their_devices(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    audit_events: AuditEvents,
) -> None:
    alice, folder, _ = budget
    bob = sign_in("bob")
    namespace = _shared_with(api, budget, bob)
    mount = _ok(_mount(api, bob, namespace), 201)
    personal = _namespaces(api, bob)[0]["id"]
    cursor = _changes(api, bob, personal)[-1]["cursor"]
    [member] = _members(api, alice, folder["id"])

    _ok(api.delete(f"/api/v1/members/{member['id']}", headers=_auth(alice)), 204)

    assert [n["id"] for n in _namespaces(api, bob)] == [personal]
    [row] = _changes(api, bob, personal, int(str(cursor)))
    assert (row["type"], row["node_id"]) == ("trashed", mount["id"])
    assert _wait(api, bob, {namespace: 0}).json()["data"]["changed"] == [namespace]
    assert _children(api, bob, folder["id"]).status_code == 404
    assert api.get("/api/v1/shared-with-me", headers=_auth(bob)).json()["data"] == []
    assert len(audit_events(AuditAction.SHARE_REMOVED)) == 1


def test_access_through_a_group_comes_and_goes_with_the_membership(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    session_factory: sessionmaker[Session],
) -> None:
    alice, folder, _ = budget
    bob = sign_in("bob")
    ctx = cli_context("it-admin")
    with session_factory.begin() as session:
        group = group_service.create_group(session, ctx, "Finance")
        group_service.add_member(session, ctx, "Finance", "bob")
    groups = api.get("/api/v1/groups/search", params={"q": "fin"}, headers=_auth(alice)).json()
    _ok(_share(api, alice, folder["id"], "editor", group_id=groups["data"][0]["id"]), 201)
    namespace = next(n for n in _namespaces(api, alice) if n["kind"] == "shared")["id"]
    mount = _ok(_mount(api, bob, namespace), 201)
    assert group.member_count == 0 and _children(api, bob, mount["id"]).status_code == 200

    with session_factory.begin() as session:
        group_service.remove_member(session, ctx, "Finance", "bob")

    assert _children(api, bob, folder["id"]).status_code == 404
    assert namespace not in [n["id"] for n in _namespaces(api, bob)]


def test_direct_access_survives_losing_the_group(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    session_factory: sessionmaker[Session],
) -> None:
    alice, folder, _ = budget
    bob = sign_in("bob")
    ctx = cli_context("it-admin")
    with session_factory.begin() as session:
        group_service.create_group(session, ctx, "Finance")
        group_service.add_member(session, ctx, "Finance", "bob")
    finance = api.get("/api/v1/groups/search", params={"q": "fin"}, headers=_auth(alice)).json()
    _ok(_share(api, alice, folder["id"], "editor", group_id=finance["data"][0]["id"]), 201)
    namespace = _shared_with(api, budget, bob, "viewer")
    _ok(_mount(api, bob, namespace), 201)
    assert [n["role"] for n in _namespaces(api, bob) if n["id"] == namespace] == ["editor"]

    with session_factory.begin() as session:
        group_service.delete_group(session, ctx, "Finance")

    assert [n["role"] for n in _namespaces(api, bob) if n["id"] == namespace] == ["viewer"]


def test_leaving_affects_only_the_one_who_leaves(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], audit_events: AuditEvents
) -> None:
    alice, folder, _ = budget
    bob, carol = sign_in("bob"), sign_in("carol")
    namespace = _shared_with(api, budget, bob)
    _ok(_share(api, alice, folder["id"], "viewer", user_id=_user_id(api, carol)), 201)
    _ok(_mount(api, bob, namespace), 201)
    _ok(_mount(api, carol, namespace), 201)

    _ok(api.delete(f"/api/v1/mounts/{namespace}", headers=_auth(bob)), 204)

    assert namespace not in [n["id"] for n in _namespaces(api, bob)]
    assert namespace in [n["id"] for n in _namespaces(api, carol)]
    shared = api.get("/api/v1/shared-with-me", headers=_auth(bob)).json()["data"]
    assert [(s["name"], s["mount_node_id"]) for s in shared] == [("Budget", None)]  # can add again
    owner_leaves = api.delete(f"/api/v1/mounts/{namespace}", headers=_auth(alice))
    assert owner_leaves.json()["error"]["code"] == "SHARE_NOT_ALLOWED"
    assert len(audit_events(AuditAction.SHARE_LEFT)) == 1


def test_deleting_a_recipients_mount_is_leaving(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], audit_events: AuditEvents
) -> None:
    alice, _folder, plan = budget
    bob = sign_in("bob")
    mount = _ok(_mount(api, bob, _shared_with(api, budget, bob)), 201)

    _ok(
        api.delete(f"/api/v1/nodes/{mount['id']}", params={"if_revision": 1}, headers=_auth(bob)),
        204,
    )

    assert len(audit_events(AuditAction.SHARE_LEFT)) == 1
    assert _ok(api.get(f"/api/v1/nodes/{plan['id']}", headers=_auth(alice)))["id"] == plan["id"]


def test_the_owner_deletes_a_shared_folder_only_once_nobody_else_has_access(
    api: TestClient,
    sign_in: SignIn,
    budget: tuple[Account, Json, Json],
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    alice, _folder, plan = budget
    bob = sign_in("bob")
    _shared_with(api, budget, bob)
    [projects] = _items(_children(api, alice, root_of(alice)))
    [mount] = _items(_children(api, alice, projects["id"]))
    params = {"if_revision": 1}

    refused = api.delete(f"/api/v1/nodes/{mount['id']}", params=params, headers=_auth(alice))
    [member] = _members(api, alice, mount["id"])
    _ok(api.delete(f"/api/v1/members/{member['id']}", headers=_auth(alice)), 204)
    deleted = api.delete(f"/api/v1/nodes/{projects['id']}", params=params, headers=_auth(alice))

    assert refused.json()["error"]["code"] == "SHARE_NOT_ALLOWED"
    assert deleted.status_code == 204
    with session_factory() as session:
        trashed = session.get(Node, plan["id"])
        assert trashed is not None and str(trashed.trash_root_id) == mount["id"]


def test_every_sharing_action_is_audited(
    api: TestClient, sign_in: SignIn, budget: tuple[Account, Json, Json], audit_events: AuditEvents
) -> None:
    alice, folder, _ = budget
    bob = sign_in("bob")
    namespace = _shared_with(api, budget, bob)
    _ok(_mount(api, bob, namespace), 201)
    [member] = _members(api, alice, folder["id"])
    _ok(api.patch(f"/api/v1/members/{member['id']}", json={"role": "viewer"}, headers=_auth(alice)))
    _ok(api.delete(f"/api/v1/mounts/{namespace}", headers=_auth(bob)), 204)
    _ok(api.delete(f"/api/v1/members/{member['id']}", headers=_auth(alice)), 204)

    sharing = [e for e in audit_events() if e.action.value.startswith("share.")]

    assert [e.action for e in sharing] == [
        AuditAction.SHARE_CREATED,
        AuditAction.SHARE_ACCEPTED,
        AuditAction.SHARE_CHANGED,
        AuditAction.SHARE_LEFT,
        AuditAction.SHARE_REMOVED,
    ]
    created = sharing[0]
    assert (created.target_id, created.target_name) == (folder["id"], "Budget")
    assert '"role": "editor"' in (created.details or "")
