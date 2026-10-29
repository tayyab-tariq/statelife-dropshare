"""Rename and move: `PATCH /nodes/{id}` (FR-FILE-04, FR-SYNC-10, FR-AUDIT-01)."""

import json
import threading
import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.errors import AppError
from dropshare_server.models import AuditEvent, Change
from dropshare_server.services import nodes as node_service
from dropshare_server.services.context import Actor, CallContext
from dropshare_shared.schemas.audit import AuditAction, AuditActorType
from dropshare_shared.schemas.nodes import NodeUpdateIn

type User = dict[str, str]
type SignIn = Callable[..., User]
type RootOf = Callable[[User], str]
type UploadFile = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _mkdir(api: TestClient, user: User, parent_id: str, name: str) -> dict[str, object]:
    response = api.post(
        "/api/v1/nodes", json={"parent_id": parent_id, "name": name}, headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()["data"]
    return data


def _patch(api: TestClient, user: User, node_id: object, **body: object) -> Response:
    response: Response = api.patch(f"/api/v1/nodes/{node_id}", json=body, headers=_auth(user))
    return response


def _changes(session_factory: sessionmaker[Session]) -> list[Change]:
    with session_factory() as session:
        return list(session.scalars(select(Change).order_by(Change.id)))


def test_rename_keeps_the_id_bumps_the_revision_and_writes_a_change_and_audit_row(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Draft")

    response = _patch(api, alice, folder["id"], if_revision=1, name="Final")

    assert response.status_code == 200, response.text
    renamed = response.json()["data"]
    assert (renamed["id"], renamed["name"], renamed["revision"]) == (folder["id"], "Final", 2)
    last = _changes(session_factory)[-1]
    assert (str(last.node_id), last.change_type, last.name, last.revision) == (
        folder["id"],
        "moved",
        "Final",
        2,
    )
    [event] = audit_events(AuditAction.NODE_RENAMED)
    assert (event.target_id, event.target_name) == (folder["id"], "Final")
    assert json.loads(event.details or "{}") == {"old_name": "Draft"}


def test_moving_a_folder_takes_everything_inside_along_with_one_change_row(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    root = root_of(alice)
    projects = _mkdir(api, alice, root, "Projects")
    archive = _mkdir(api, alice, root, "Archive")
    file = upload_file(alice, b"plan", parent_id=projects["id"], name="plan.txt").json()["data"]
    before = len(_changes(session_factory))

    response = _patch(api, alice, projects["id"], if_revision=1, parent_id=archive["id"])

    assert response.status_code == 200, response.text
    assert response.json()["data"]["parent_id"] == archive["id"]
    inner = api.get(f"/api/v1/nodes/{file['id']}", headers=_auth(alice)).json()["data"]
    assert (inner["parent_id"], inner["revision"]) == (projects["id"], file["revision"])
    assert len(_changes(session_factory)) == before + 1
    [event] = audit_events(AuditAction.NODE_MOVED)
    assert json.loads(event.details or "{}") == {
        "old_name": "Projects",
        "from_parent_id": root,
        "to_parent_id": archive["id"],
    }


def test_rename_and_move_in_one_request(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    root = root_of(alice)
    target = _mkdir(api, alice, root, "Target")
    file = upload_file(alice, b"x", parent_id=root, name="a.txt").json()["data"]

    response = _patch(
        api, alice, file["id"], if_revision=file["revision"], name="b.txt", parent_id=target["id"]
    )

    assert response.status_code == 200, response.text
    moved = response.json()["data"]
    assert (moved["name"], moved["parent_id"], moved["content_hash"]) == (
        "b.txt",
        target["id"],
        file["content_hash"],
    )


def test_the_old_name_is_free_and_the_new_one_taken_afterwards(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    root = root_of(alice)
    folder = _mkdir(api, alice, root, "Old")

    _patch(api, alice, folder["id"], if_revision=1, name="New")

    assert (
        api.post(
            "/api/v1/nodes", json={"parent_id": root, "name": "old"}, headers=_auth(alice)
        ).status_code
        == 201
    )
    taken = api.post("/api/v1/nodes", json={"parent_id": root, "name": "NEW"}, headers=_auth(alice))
    assert taken.json()["error"]["code"] == "NAME_CONFLICT"


def test_a_case_only_rename_is_allowed(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "report")

    response = _patch(api, alice, folder["id"], if_revision=1, name="Report")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["name"] == "Report"


def test_name_collisions_are_case_insensitive_and_name_the_existing_item(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    root = root_of(alice)
    existing = _mkdir(api, alice, root, "Reports")
    other = _mkdir(api, alice, root, "Other")
    before = len(_changes(session_factory))

    response = _patch(api, alice, other["id"], if_revision=1, name="REPORTS")

    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["details"]) == (
        "NAME_CONFLICT",
        {"node_id": existing["id"], "kind": "folder"},
    )
    assert len(_changes(session_factory)) == before


def test_a_stale_revision_is_a_conflict(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "A")
    _patch(api, alice, folder["id"], if_revision=1, name="B")

    response = _patch(api, alice, folder["id"], if_revision=1, name="C")

    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["details"]) == ("REVISION_CONFLICT", {"current_revision": 2})


def test_repeating_a_move_that_succeeded_succeeds_without_another_change(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    """A retry after a lost response (NFR-REL-04)."""
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "A")
    first = _patch(api, alice, folder["id"], if_revision=1, name="B")
    count = len(_changes(session_factory))

    again = _patch(api, alice, folder["id"], if_revision=1, name="B")

    assert again.status_code == 200, again.text
    assert again.json()["data"] == first.json()["data"]
    assert len(_changes(session_factory)) == count


def test_a_folder_cant_be_moved_into_itself_or_below_itself(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    outer = _mkdir(api, alice, root_of(alice), "Outer")
    inner = _mkdir(api, alice, str(outer["id"]), "Inner")
    deepest = _mkdir(api, alice, str(inner["id"]), "Deepest")

    for target in (outer, inner, deepest):
        response = _patch(api, alice, outer["id"], if_revision=1, parent_id=target["id"])
        assert response.status_code == 400, target["name"]
        [issue] = response.json()["error"]["details"]
        assert issue["type"] == "into_itself"


def test_the_target_must_be_a_live_folder(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    root = root_of(alice)
    folder = _mkdir(api, alice, root, "A")
    file = upload_file(alice, b"x", parent_id=root, name="f.txt").json()["data"]
    gone = _mkdir(api, alice, root, "Gone")
    api.delete(f"/api/v1/nodes/{gone['id']}?if_revision=1", headers=_auth(alice))

    into_file = _patch(api, alice, folder["id"], if_revision=1, parent_id=file["id"])
    into_trash = _patch(api, alice, folder["id"], if_revision=1, parent_id=gone["id"])

    assert into_file.json()["error"]["details"][0]["type"] == "not_a_folder"
    assert (into_trash.status_code, into_trash.json()["error"]["code"]) == (404, "NOT_FOUND")


def test_the_root_cant_be_moved_and_names_are_validated(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    root = root_of(alice)
    folder = _mkdir(api, alice, root, "A")

    root_move = _patch(api, alice, root, if_revision=1, name="Root")
    bad_name = _patch(api, alice, folder["id"], if_revision=1, name="CON.txt")
    nothing = _patch(api, alice, folder["id"], if_revision=1)

    assert root_move.json()["error"]["details"][0]["type"] == "root_not_movable"
    assert bad_name.json()["error"]["details"][0]["type"] == "RESERVED"
    assert nothing.json()["error"]["code"] == "VALIDATION_ERROR"


def test_another_users_items_are_not_found_and_the_denial_is_audited(
    api: TestClient, sign_in: SignIn, root_of: RootOf, audit_events: AuditEvents
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    alices = _mkdir(api, alice, root_of(alice), "Private")
    bobs = _mkdir(api, bob, root_of(bob), "Mine")

    renaming_hers = _patch(api, bob, alices["id"], if_revision=1, name="Taken")
    into_hers = _patch(api, bob, bobs["id"], if_revision=1, parent_id=alices["id"])
    missing = _patch(api, bob, uuid.uuid4(), if_revision=1, name="X")

    assert [r.status_code for r in (renaming_hers, into_hers, missing)] == [404, 404, 404]
    assert len(audit_events(AuditAction.ACCESS_DENIED)) == 2
    assert (
        api.get(f"/api/v1/nodes/{alices['id']}", headers=_auth(alice)).json()["data"]["name"]
        == "Private"
    )


def test_opposite_moves_at_the_same_time_cant_make_a_folder_cycle(
    api: TestClient, sign_in: SignIn, root_of: RootOf, session_factory: sessionmaker[Session]
) -> None:
    """A into B/b1 while B goes into A/a1: the second waits for the first, then sees the
    cycle it would make (audit B2)."""
    alice = sign_in()
    root = root_of(alice)
    user_id = uuid.UUID(api.get("/api/v1/me", headers=_auth(alice)).json()["data"]["id"])
    a = _mkdir(api, alice, root, "A")
    a1 = _mkdir(api, alice, str(a["id"]), "a1")
    b = _mkdir(api, alice, root, "B")
    b1 = _mkdir(api, alice, str(b["id"]), "b1")
    ctx = CallContext(Actor(AuditActorType.USER, user_id=user_id))
    second: list[object] = []

    def move_b_into_a1() -> None:
        with session_factory() as session:
            try:
                node_service.move_node(
                    session,
                    ctx,
                    user_id,
                    uuid.UUID(str(b["id"])),
                    NodeUpdateIn(if_revision=1, parent_id=uuid.UUID(str(a1["id"]))),
                )
                session.commit()
                second.append("moved")
            except AppError as exc:
                session.rollback()
                second.append(exc)

    with session_factory() as first:
        node_service.move_node(
            first,
            ctx,
            user_id,
            uuid.UUID(str(a["id"])),
            NodeUpdateIn(if_revision=1, parent_id=uuid.UUID(str(b1["id"]))),
        )
        racer = threading.Thread(target=move_b_into_a1)
        racer.start()
        racer.join(1.0)
        assert racer.is_alive()  # waiting for the first move's lock
        first.commit()
    racer.join(30.0)

    [outcome] = second
    assert isinstance(outcome, AppError) and outcome.status_code == 400
    assert (
        api.get(f"/api/v1/nodes/{b['id']}", headers=_auth(alice)).json()["data"]["parent_id"]
        == root
    )
