"""Single shared files (FR-SHARE-03, OQ-32) and moves into and out of shared folders
(FR-FILE-04, FR-SHARE-06)."""

import hashlib
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, User
from dropshare_shared.schemas.audit import AuditAction

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
    data: Json = response.json()["data"] if status != 204 else {}
    return data


def _items(response: Response) -> list[Json]:
    assert response.status_code == 200, response.text
    items: list[Json] = response.json()["data"]
    return items


def _user_id(api: TestClient, user: Account) -> str:
    return str(_ok(api.get("/api/v1/me", headers=_auth(user)))["id"])


def _mkdir(api: TestClient, user: Account, parent_id: object, name: str) -> Json:
    body = {"parent_id": str(parent_id), "name": name}
    return _ok(api.post("/api/v1/nodes", json=body, headers=_auth(user)), 201)


def _share(api: TestClient, owner: Account, node_id: object, role: str, other: Account) -> Json:
    body = {"user_id": _user_id(api, other), "role": role}
    return _ok(api.post(f"/api/v1/nodes/{node_id}/members", json=body, headers=_auth(owner)), 201)


def _used(sessions: sessionmaker[Session]) -> dict[str, int]:
    with sessions() as session:
        return {u.username: u.used_bytes for u in session.scalars(select(User))}


def _start_new_version(api: TestClient, user: Account, node: Json, content: bytes) -> Response:
    response: Response = api.post(
        "/api/v1/uploads",
        json={
            "node_id": node["id"],
            "base_revision": node["revision"],
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
        headers=_auth(user),
    )
    return response


@pytest.fixture
def plan(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> tuple[Account, Json]:
    alice = sign_in("alice")
    return alice, _ok(upload_file(alice, b"plan", parent_id=root_of(alice), name="plan.txt"))


def test_a_shared_file_appears_in_shared_with_me_and_can_be_downloaded(
    api: TestClient, sign_in: SignIn, plan: tuple[Account, Json]
) -> None:
    alice, file = plan
    bob = sign_in("bob")

    _share(api, alice, file["id"], "viewer", bob)

    [item] = _items(api.get("/api/v1/shared-with-me", headers=_auth(bob)))
    assert (item["kind"], item["name"], item["role"], item["namespace_id"]) == (
        "file",
        "plan.txt",
        "viewer",
        None,
    )
    assert item["size_bytes"] == 4
    content = api.get(f"/api/v1/nodes/{file['id']}/content", headers=_auth(bob))
    assert content.content == b"plan"
    assert _ok(api.get(f"/api/v1/nodes/{file['id']}", headers=_auth(bob)))["name"] == "plan.txt"


def test_an_editor_uploads_a_new_version_but_cant_rename_or_delete(
    api: TestClient,
    sign_in: SignIn,
    plan: tuple[Account, Json],
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
) -> None:
    alice, file = plan
    bob = sign_in("bob")
    _share(api, alice, file["id"], "editor", bob)

    updated = _ok(upload_file(bob, b"plan v2!", node_id=file["id"], base_revision=1))

    assert (updated["revision"], updated["size_bytes"]) == (2, 8)
    assert _used(session_factory) == {"alice": 8, "bob": 0}  # charged to the owner
    rename = api.patch(
        f"/api/v1/nodes/{file['id']}", json={"if_revision": 2, "name": "x.txt"}, headers=_auth(bob)
    )
    delete = api.delete(
        f"/api/v1/nodes/{file['id']}", params={"if_revision": 2}, headers=_auth(bob)
    )
    assert (rename.status_code, delete.status_code) == (404, 404)


def test_a_viewer_of_a_shared_file_cant_upload_a_new_version(
    api: TestClient, sign_in: SignIn, plan: tuple[Account, Json]
) -> None:
    alice, file = plan
    bob = sign_in("bob")
    _share(api, alice, file["id"], "viewer", bob)

    assert _start_new_version(api, bob, file, b"mine").status_code == 403


def test_sharing_a_file_reaches_only_that_file(
    api: TestClient,
    sign_in: SignIn,
    plan: tuple[Account, Json],
    root_of: RootOf,
    upload_file: UploadFile,
) -> None:
    alice, file = plan
    bob = sign_in("bob")
    other = _ok(upload_file(alice, b"other", parent_id=root_of(alice), name="other.txt"))
    _share(api, alice, file["id"], "editor", bob)

    assert api.get(f"/api/v1/nodes/{other['id']}", headers=_auth(bob)).status_code == 404
    assert (
        api.get(f"/api/v1/nodes/{root_of(alice)}/children", headers=_auth(bob)).status_code == 404
    )


def test_file_shares_are_listed_and_removed_like_folder_members(
    api: TestClient, sign_in: SignIn, plan: tuple[Account, Json], audit_events: AuditEvents
) -> None:
    alice, file = plan
    bob = sign_in("bob")
    member = _share(api, alice, file["id"], "viewer", bob)

    sharing = _ok(api.get(f"/api/v1/nodes/{file['id']}/members", headers=_auth(bob)))
    _ok(api.delete(f"/api/v1/members/{member['id']}", headers=_auth(alice)), 204)

    assert sharing["can_manage"] is False
    assert api.get(f"/api/v1/nodes/{file['id']}", headers=_auth(bob)).status_code == 404
    assert _items(api.get("/api/v1/shared-with-me", headers=_auth(bob))) == []
    assert [e.action for e in audit_events() if e.action.value.startswith("share.")] == [
        AuditAction.SHARE_CREATED,
        AuditAction.SHARE_REMOVED,
    ]


def test_a_trashed_shared_file_leaves_shared_with_me(
    api: TestClient, sign_in: SignIn, plan: tuple[Account, Json]
) -> None:
    alice, file = plan
    bob = sign_in("bob")
    _share(api, alice, file["id"], "viewer", bob)

    _ok(
        api.delete(f"/api/v1/nodes/{file['id']}", params={"if_revision": 1}, headers=_auth(alice)),
        204,
    )

    assert _items(api.get("/api/v1/shared-with-me", headers=_auth(bob))) == []


@pytest.fixture
def shared_with_bob(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> tuple[Account, Account, Json, Json]:
    """Alice's folder Team, shared with Bob as editor and added to his DropShare."""
    alice, bob = sign_in("alice"), sign_in("bob")
    team = _mkdir(api, alice, root_of(alice), "Team")
    _share(api, alice, team["id"], "editor", bob)
    namespace = next(
        n["id"]
        for n in _items(api.get("/api/v1/namespaces", headers=_auth(alice)))
        if n["kind"] == "shared"
    )
    mount = _ok(
        api.post("/api/v1/mounts", json={"namespace_id": namespace}, headers=_auth(bob)), 201
    )
    return alice, bob, team, mount


def _changes(api: TestClient, user: Account, namespace_id: object, cursor: int) -> list[Json]:
    return _items(
        api.get(
            f"/api/v1/namespaces/{namespace_id}/changes",
            params={"cursor": cursor},
            headers=_auth(user),
        )
    )


def _latest(api: TestClient, user: Account, namespace_id: object) -> int:
    rows = _changes(api, user, namespace_id, 0)
    return int(str(rows[-1]["cursor"])) if rows else 0


def test_moving_into_a_shared_folder_keeps_ids_and_moves_the_bytes_to_the_owner(
    api: TestClient,
    shared_with_bob: tuple[Account, Account, Json, Json],
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice, bob, _team, mount = shared_with_bob
    notes = _mkdir(api, bob, root_of(bob), "Notes")
    draft = _ok(upload_file(bob, b"draft", parent_id=notes["id"], name="draft.txt"))
    bob_personal = _items(api.get("/api/v1/namespaces", headers=_auth(bob)))[0]["id"]
    since_personal = _latest(api, bob, bob_personal)
    since_shared = _latest(api, alice, mount["mount_namespace_id"])

    moved = _ok(
        api.patch(
            f"/api/v1/nodes/{notes['id']}",
            json={"if_revision": 1, "parent_id": mount["id"]},  # dropped onto the mount
            headers=_auth(bob),
        )
    )

    assert moved["id"] == notes["id"] and moved["namespace_id"] == mount["mount_namespace_id"]
    assert _used(session_factory) == {"alice": 5, "bob": 0}
    left = _changes(api, bob, bob_personal, since_personal)
    assert [(c["type"], c["node_id"]) for c in left] == [("removed", notes["id"])]
    arrived = _changes(api, alice, mount["mount_namespace_id"], since_shared)
    assert [(c["type"], c["node_id"]) for c in arrived] == [
        ("created", notes["id"]),
        ("created", draft["id"]),
    ]
    content = api.get(f"/api/v1/nodes/{draft['id']}/content", headers=_auth(alice))
    assert content.content == b"draft"
    [event] = [e for e in audit_events(AuditAction.NODE_MOVED)]
    assert f'"to_namespace_id": "{mount["mount_namespace_id"]}"' in (event.details or "")


def test_moving_out_of_a_shared_folder_charges_the_one_who_takes_it(
    api: TestClient,
    shared_with_bob: tuple[Account, Account, Json, Json],
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
) -> None:
    alice, bob, team, _mount = shared_with_bob
    file = _ok(upload_file(alice, b"budget", parent_id=team["id"], name="budget.txt"))

    _ok(
        api.patch(
            f"/api/v1/nodes/{file['id']}",
            json={"if_revision": 1, "parent_id": root_of(bob)},
            headers=_auth(bob),
        )
    )

    assert _used(session_factory) == {"alice": 0, "bob": 6}
    assert api.get(f"/api/v1/nodes/{file['id']}", headers=_auth(alice)).status_code == 404


def test_a_move_the_new_owner_has_no_room_for_is_refused(
    api: TestClient,
    shared_with_bob: tuple[Account, Account, Json, Json],
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
) -> None:
    alice, bob, _, mount = shared_with_bob
    _ok(upload_file(alice, b"x" * 1000, parent_id=root_of(alice), name="big.bin"))  # 1000/1024
    file = _ok(upload_file(bob, b"y" * 100, parent_id=root_of(bob), name="mine.bin"))

    response = api.patch(
        f"/api/v1/nodes/{file['id']}",
        json={"if_revision": 1, "parent_id": mount["id"]},
        headers=_auth(bob),
    )

    assert response.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert _used(session_factory) == {"alice": 1000, "bob": 100}


def test_a_viewer_cant_move_things_in_or_out(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    team = _mkdir(api, alice, root_of(alice), "Team")
    inside = _ok(upload_file(alice, b"in", parent_id=team["id"], name="in.txt"))
    _share(api, alice, team["id"], "viewer", bob)
    mine = _ok(upload_file(bob, b"mine", parent_id=root_of(bob), name="mine.txt"))

    into = api.patch(
        f"/api/v1/nodes/{mine['id']}",
        json={"if_revision": 1, "parent_id": team["id"]},
        headers=_auth(bob),
    )
    out = api.patch(
        f"/api/v1/nodes/{inside['id']}",
        json={"if_revision": 1, "parent_id": root_of(bob)},
        headers=_auth(bob),
    )

    assert (into.status_code, out.status_code) == (403, 403)


def test_a_shared_folder_never_goes_inside_another(
    api: TestClient, shared_with_bob: tuple[Account, Account, Json, Json], root_of: RootOf
) -> None:
    alice, bob, _, mount = shared_with_bob
    other = _mkdir(api, bob, root_of(bob), "Other")
    _share(api, bob, other["id"], "editor", alice)
    [bobs_mount] = [
        c
        for c in _items(api.get(f"/api/v1/nodes/{root_of(bob)}/children", headers=_auth(bob)))
        if c["name"] == "Other"
    ]

    response = api.patch(
        f"/api/v1/nodes/{bobs_mount['id']}",
        json={"if_revision": 1, "parent_id": mount["id"]},
        headers=_auth(bob),
    )

    assert response.json()["error"]["code"] == "SHARE_NOT_ALLOWED"


def test_a_recipient_renames_and_moves_their_mount_for_themselves_only(
    api: TestClient, shared_with_bob: tuple[Account, Account, Json, Json], root_of: RootOf
) -> None:
    alice, bob, _team, mount = shared_with_bob
    work = _mkdir(api, bob, root_of(bob), "Work")

    moved = _ok(
        api.patch(
            f"/api/v1/nodes/{mount['id']}",
            json={"if_revision": 1, "parent_id": work["id"], "name": "Alice's team"},
            headers=_auth(bob),
        )
    )

    assert (moved["name"], moved["parent_id"]) == ("Alice's team", work["id"])
    shared = _items(api.get("/api/v1/shared-with-me", headers=_auth(bob)))
    assert shared[0]["name"] == "Team"  # the owner's name is unchanged
    alices = _items(api.get(f"/api/v1/nodes/{root_of(alice)}/children", headers=_auth(alice)))
    assert [c["name"] for c in alices] == ["Team"]
