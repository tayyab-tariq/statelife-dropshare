"""The trash: list, restore, permanent delete, empty, and the cleanup jobs (FR-TRASH-01 … 04,
NFR-REL-05, FR-AUDIT-03, OQ-37)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, Change, FileVersion, Node, UploadSession, User
from dropshare_server.repositories import audit_retention
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services import blobs as blob_service
from dropshare_server.services import trash as trash_service
from dropshare_server.services.context import cli_context
from dropshare_server.storage.local import LocalFilesystemBackend
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.nodes import NodeKind

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type UploadFile = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]
type Json = dict[str, object]

_CLI = cli_context("scheduler")


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _ok(response: Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    if status == 204:
        return {}
    data: Json = response.json()["data"]
    return data


def _mkdir(api: TestClient, user: Account, parent_id: object, name: str) -> Json:
    body = {"parent_id": str(parent_id), "name": name}
    return _ok(api.post("/api/v1/nodes", json=body, headers=_auth(user)), 201)


def _delete(api: TestClient, user: Account, node: Json) -> None:
    current = _ok(api.get(f"/api/v1/nodes/{node['id']}", headers=_auth(user)))
    params = {"if_revision": current["revision"]}
    _ok(api.delete(f"/api/v1/nodes/{node['id']}", params=params, headers=_auth(user)), 204)


def _trash(api: TestClient, user: Account) -> list[Json]:
    response = api.get("/api/v1/trash", headers=_auth(user))
    assert response.status_code == 200, response.text
    items: list[Json] = response.json()["data"]
    return items


def _restore(api: TestClient, user: Account, node_id: object) -> Response:
    response: Response = api.post(f"/api/v1/trash/{node_id}/restore", headers=_auth(user))
    return response


def _purge(api: TestClient, user: Account, node_id: object) -> Response:
    response: Response = api.delete(f"/api/v1/trash/{node_id}", headers=_auth(user))
    return response


def _names(api: TestClient, user: Account, folder_id: object) -> list[str]:
    response = api.get(f"/api/v1/nodes/{folder_id}/children", headers=_auth(user))
    assert response.status_code == 200, response.text
    return [str(child["name"]) for child in response.json()["data"]]


def _child(api: TestClient, user: Account, folder_id: object, name: str) -> Json:
    response = api.get(f"/api/v1/nodes/{folder_id}/children", headers=_auth(user))
    return next(c for c in response.json()["data"] if c["name"] == name)


def _user_id(api: TestClient, user: Account) -> str:
    return str(_ok(api.get("/api/v1/me", headers=_auth(user)))["id"])


def _used_bytes(session_factory: sessionmaker[Session], username: str) -> int:
    with session_factory() as session:
        return session.scalars(select(User.used_bytes).where(User.username == username)).one()


def _personal(api: TestClient, user: Account) -> str:
    namespaces = api.get("/api/v1/namespaces", headers=_auth(user)).json()["data"]
    return str(namespaces[0]["id"])


def _changes(api: TestClient, user: Account, namespace_id: str, cursor: int) -> list[Json]:
    response = api.get(
        f"/api/v1/namespaces/{namespace_id}/changes",
        params={"cursor": cursor},
        headers=_auth(user),
    )
    assert response.status_code == 200, response.text
    rows: list[Json] = response.json()["data"]
    return rows


def _cursor(api: TestClient, user: Account, namespace_id: str) -> int:
    rows = _changes(api, user, namespace_id, 0)
    return int(str(rows[-1]["cursor"])) if rows else 0


def _age(session_factory: sessionmaker[Session], node_id: object, days: int) -> None:
    """Pretend an item went to the trash `days` ago."""
    with session_factory.begin() as session:
        session.execute(
            update(Node)
            .where(Node.trash_root_id == uuid.UUID(str(node_id)))
            .values(trashed_at=datetime.now(UTC) - timedelta(days=days))
        )


@pytest.fixture
def docs(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> tuple[Account, Json, Json, Json]:
    """Alice's Docs/ holding Notes/ with a.txt (5 bytes) and b.txt (3 bytes)."""
    alice = sign_in("alice")
    folder = _mkdir(api, alice, root_of(alice), "Docs")
    notes = _mkdir(api, alice, folder["id"], "Notes")
    a = _ok(upload_file(alice, b"apple", parent_id=notes["id"], name="a.txt"))
    _ok(upload_file(alice, b"bee", parent_id=notes["id"], name="b.txt"))
    return alice, folder, notes, a


# --- listing --------------------------------------------------------------------------------


def test_the_trash_lists_what_was_deleted_not_what_was_inside_it(
    api: TestClient, docs: tuple[Account, Json, Json, Json]
) -> None:
    alice, _folder, notes, _a = docs
    _delete(api, alice, notes)

    [item] = _trash(api, alice)

    assert (item["id"], item["name"], item["kind"]) == (notes["id"], "Notes", "folder")
    assert (item["item_count"], item["size_bytes"]) == (2, 8)
    assert (item["location"], item["shared_folder"]) == ("Docs", None)
    assert (item["trashed_by"], item["can_delete"]) == ("Alice", True)


def test_the_trash_is_newest_first_and_paged(
    api: TestClient, docs: tuple[Account, Json, Json, Json], root_of: RootOf
) -> None:
    alice, folder, _notes, a = docs
    _delete(api, alice, a)
    _delete(api, alice, folder)

    first = api.get("/api/v1/trash", params={"limit": 1}, headers=_auth(alice)).json()

    assert [i["name"] for i in first["data"]] == ["Docs"]
    assert first["meta"]["total"] == 2
    assert [i["location"] for i in _trash(api, alice)] == ["", "Docs/Notes"]


def test_signing_in_is_required(api: TestClient) -> None:
    assert api.get("/api/v1/trash").status_code == 401


# --- restore --------------------------------------------------------------------------------


def test_restoring_puts_it_back_with_its_ids_and_tells_clients(
    api: TestClient, docs: tuple[Account, Json, Json, Json], audit_events: AuditEvents
) -> None:
    alice, folder, notes, a = docs
    _delete(api, alice, notes)
    namespace = _personal(api, alice)
    before = _cursor(api, alice, namespace)

    restored = _ok(_restore(api, alice, notes["id"]))

    assert (restored["id"], restored["parent_id"], restored["name"]) == (
        notes["id"],
        folder["id"],
        "Notes",
    )
    assert _names(api, alice, notes["id"]) == ["a.txt", "b.txt"]
    assert _trash(api, alice) == []
    rows = _changes(api, alice, namespace, before)
    assert [r["type"] for r in rows] == ["created"] * 3
    assert rows[0]["node_id"] == notes["id"]  # parents first
    # Each with a new revision: clients ignore an item at the revision it was trashed at.
    assert (
        next(r for r in rows if r["node_id"] == a["id"])["revision"] == int(str(a["revision"])) + 1
    )
    assert a["id"] in {r["node_id"] for r in rows}
    [event] = audit_events(AuditAction.NODE_RESTORED)
    assert event.target_id == notes["id"] and '"items": 2' in str(event.details)


def test_restoring_again_returns_the_item_as_it_is(
    api: TestClient, docs: tuple[Account, Json, Json, Json]
) -> None:
    alice, _folder, _notes, a = docs
    _delete(api, alice, a)
    first = _ok(_restore(api, alice, a["id"]))

    again = _ok(_restore(api, alice, a["id"]))

    assert again == first


def test_a_name_taken_meanwhile_gets_restored_added(
    api: TestClient, docs: tuple[Account, Json, Json, Json], upload_file: UploadFile
) -> None:
    alice, _folder, notes, a = docs
    _delete(api, alice, a)
    _ok(upload_file(alice, b"new", parent_id=notes["id"], name="A.TXT"))
    _ok(_restore(api, alice, a["id"]))
    b = _child(api, alice, notes["id"], "b.txt")
    _delete(api, alice, b)
    _ok(upload_file(alice, b"x", parent_id=notes["id"], name="b.txt"))
    _ok(upload_file(alice, b"y", parent_id=notes["id"], name="b (restored).txt"))

    again = _ok(_restore(api, alice, b["id"]))

    assert "a (restored).txt" in _names(api, alice, notes["id"])
    assert again["name"] == "b (restored 2).txt"


def test_a_deleted_parent_is_recreated_and_the_deleted_folder_then_gets_a_new_name(
    api: TestClient, docs: tuple[Account, Json, Json, Json], root_of: RootOf
) -> None:
    alice, folder, notes, a = docs
    _delete(api, alice, a)  # first the file, then the folders it was in
    _delete(api, alice, folder)

    restored = _ok(_restore(api, alice, a["id"]))

    [docs_again] = [_child(api, alice, root_of(alice), "Docs")]
    notes_again = _child(api, alice, docs_again["id"], "Notes")
    assert docs_again["id"] != folder["id"] and notes_again["id"] != notes["id"]
    assert restored["parent_id"] == notes_again["id"]
    # The deleted Docs still holds b.txt; it comes back beside the recreated one.
    _ok(_restore(api, alice, folder["id"]))
    assert _names(api, alice, root_of(alice)) == ["Docs", "Docs (restored)"]


def test_a_file_where_a_deleted_folder_was_means_a_new_folder_beside_it(
    api: TestClient, docs: tuple[Account, Json, Json, Json], upload_file: UploadFile
) -> None:
    alice, folder, notes, a = docs
    _delete(api, alice, a)
    _delete(api, alice, notes)
    _ok(upload_file(alice, b"!", parent_id=folder["id"], name="Notes"))

    restored = _ok(_restore(api, alice, a["id"]))

    assert sorted(_names(api, alice, folder["id"])) == ["Notes", "Notes (restored)"]
    parent = _ok(api.get(f"/api/v1/nodes/{restored['parent_id']}", headers=_auth(alice)))
    assert (parent["name"], parent["kind"]) == ("Notes (restored)", "folder")


def test_only_items_in_the_trash_can_be_restored(
    api: TestClient, docs: tuple[Account, Json, Json, Json], sign_in: SignIn
) -> None:
    alice, _folder, notes, a = docs
    _delete(api, alice, notes)
    bob = sign_in("bob")

    inside = _restore(api, alice, a["id"])  # it went with Notes, so it comes back with Notes
    strangers = _restore(api, bob, notes["id"])
    missing = _restore(api, alice, uuid.uuid4())

    assert [r.status_code for r in (inside, strangers, missing)] == [404, 404, 404]


# --- permanent delete -------------------------------------------------------------------------


def test_deleting_permanently_frees_quota_and_removes_the_content(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    session_factory: sessionmaker[Session],
    storage_root: Path,
    audit_events: AuditEvents,
) -> None:
    alice, _folder, notes, _a = docs
    _delete(api, alice, notes)
    assert _used_bytes(session_factory, "alice") == 8  # still counted while in the trash

    _ok(_purge(api, alice, notes["id"]), 204)

    assert _used_bytes(session_factory, "alice") == 0
    assert _trash(api, alice) == []
    assert _purge(api, alice, notes["id"]).status_code == 404
    assert list((storage_root / "blobs").glob("*/*/*")) == []
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(FileVersion)) == 0
        # The journal keeps its rows: a client offline for longer still learns of the delete.
        assert session.scalar(
            select(func.count()).where(Change.node_id == uuid.UUID(str(notes["id"])))
        )
    [event] = audit_events(AuditAction.NODE_PURGED)
    assert event.target_id == notes["id"] and '"freed_bytes": 8' in str(event.details)


def test_a_device_that_was_away_still_reads_the_delete_of_a_purged_item(
    api: TestClient, docs: tuple[Account, Json, Json, Json]
) -> None:
    alice, _folder, notes, _a = docs
    namespace = _personal(api, alice)
    away = _cursor(api, alice, namespace)  # a device that stops syncing here
    _delete(api, alice, notes)
    _ok(_purge(api, alice, notes["id"]), 204)

    rows = _changes(api, alice, namespace, away)

    assert [(r["type"], r["node_id"]) for r in rows] == [("trashed", notes["id"])]
    assert _changes(api, alice, namespace, int(str(rows[-1]["cursor"]))) == []


def test_items_deleted_earlier_from_inside_stay_in_the_trash(
    api: TestClient, docs: tuple[Account, Json, Json, Json]
) -> None:
    alice, folder, notes, a = docs
    _delete(api, alice, a)
    _delete(api, alice, notes)

    _ok(_purge(api, alice, notes["id"]), 204)

    [left] = _trash(api, alice)
    assert (left["id"], left["location"]) == (a["id"], "Docs")  # moved up past what is gone
    assert _ok(_restore(api, alice, a["id"]))["parent_id"] == folder["id"]


def test_emptying_the_trash_removes_only_your_own(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    sign_in: SignIn,
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice, folder, _notes, a = docs
    _delete(api, alice, a)
    _delete(api, alice, folder)
    bob = sign_in("bob")
    kept = _ok(upload_file(bob, b"bob's", parent_id=root_of(bob), name="kept.txt"))
    _delete(api, bob, kept)

    emptied = _ok(api.delete("/api/v1/trash", headers=_auth(alice)))

    assert emptied == {"removed": 2, "freed_bytes": 8, "more": False}
    assert _trash(api, alice) == []
    assert [i["id"] for i in _trash(api, bob)] == [kept["id"]]
    assert _used_bytes(session_factory, "alice") == 0
    assert len(audit_events(AuditAction.NODE_PURGED)) == 2
    assert len(audit_events(AuditAction.TRASH_EMPTIED)) == 1


def test_deleting_for_good_what_was_just_restored_says_so(
    api: TestClient, docs: tuple[Account, Json, Json, Json]
) -> None:
    alice, _folder, _notes, a = docs
    _delete(api, alice, a)
    _ok(_restore(api, alice, a["id"]))  # another device, a moment earlier

    late = _purge(api, alice, a["id"])

    assert (late.status_code, late.json()["error"]["code"]) == (409, "REVISION_CONFLICT")
    assert _ok(api.get(f"/api/v1/nodes/{a['id']}", headers=_auth(alice)))["name"] == "a.txt"


def test_emptying_a_large_trash_takes_several_calls(
    api: TestClient, docs: tuple[Account, Json, Json, Json], monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, _folder, notes, a = docs
    _delete(api, alice, a)
    _delete(api, alice, notes)
    monkeypatch.setattr(trash_service, "EMPTY_LIMIT", 1)

    first = _ok(api.delete("/api/v1/trash", headers=_auth(alice)))
    second = _ok(api.delete("/api/v1/trash", headers=_auth(alice)))

    assert (first["removed"], first["more"]) == (1, True)
    assert (second["removed"], second["more"]) == (1, False)
    assert _trash(api, alice) == []


def test_purging_waits_for_an_upload_that_is_finishing(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    session_factory: sessionmaker[Session],
) -> None:
    alice, _folder, notes, _a = docs
    started = api.post(
        "/api/v1/uploads",
        json={"parent_id": notes["id"], "name": "late.txt", "size_bytes": 1, "sha256": "0" * 64},
        headers=_auth(alice),
    )
    upload_id = uuid.UUID(str(_ok(started, 201)["id"]))
    _delete(api, alice, notes)

    with session_factory.begin() as completing:  # holds the upload row, as `complete` does
        completing.scalars(
            select(UploadSession).where(UploadSession.id == upload_id).with_for_update()
        ).one()
        busy = _purge(api, alice, notes["id"])
    after = _purge(api, alice, notes["id"])

    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "REVISION_CONFLICT")
    assert after.status_code == 204


def test_restoring_into_a_folder_deleted_meanwhile_asks_to_try_again(
    api: TestClient, docs: tuple[Account, Json, Json, Json], monkeypatch: pytest.MonkeyPatch
) -> None:
    alice, folder, notes, a = docs
    _delete(api, alice, a)
    real = node_repository.get_for_update

    def deleted_while_locking(session: Session, node_id: uuid.UUID) -> Node | None:
        locked = real(session, node_id)
        if locked is not None and str(node_id) == notes["id"]:
            locked.trashed_at = datetime.now(UTC)  # as if a delete committed just before
        return locked

    monkeypatch.setattr(node_repository, "get_for_update", deleted_while_locking)

    raced = _restore(api, alice, a["id"])

    assert (raced.status_code, raced.json()["error"]["code"]) == (409, "REVISION_CONFLICT")
    monkeypatch.undo()
    assert [i["id"] for i in _trash(api, alice)] == [a["id"]]  # nothing changed
    assert _names(api, alice, folder["id"]) == ["Notes"]


# --- shared folders ---------------------------------------------------------------------------


@pytest.fixture
def shared(
    api: TestClient, docs: tuple[Account, Json, Json, Json], sign_in: SignIn
) -> tuple[Account, Account, Json]:
    """Docs shared with Bob as editor, added to his DropShare; returns (alice, bob, his mount)."""
    alice, folder, _notes, _a = docs
    bob = sign_in("bob")
    body = {"user_id": _user_id(api, bob), "role": "editor"}
    _ok(api.post(f"/api/v1/nodes/{folder['id']}/members", json=body, headers=_auth(alice)), 201)
    namespaces = api.get("/api/v1/namespaces", headers=_auth(alice)).json()["data"]
    namespace = next(n["id"] for n in namespaces if n["kind"] == "shared")
    mount = _ok(
        api.post("/api/v1/mounts", json={"namespace_id": namespace}, headers=_auth(bob)), 201
    )
    return alice, bob, mount


def test_an_editors_delete_goes_to_the_owners_trash_and_shows_in_theirs(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    shared: tuple[Account, Account, Json],
    sign_in: SignIn,
    audit_events: AuditEvents,
) -> None:
    _alice, _folder, notes, a = docs
    alice, bob, _mount = shared
    _delete(api, bob, a)
    carol = sign_in("carol")

    [owners] = _trash(api, alice)
    [editors] = _trash(api, bob)

    assert owners["id"] == editors["id"] == a["id"]
    assert (owners["trashed_by"], owners["shared_folder"]) == ("Bob", "Docs")
    assert (owners["location"], owners["can_delete"]) == ("Notes", True)
    assert editors["can_delete"] is False
    assert _trash(api, carol) == []
    refused = _purge(api, bob, a["id"])
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "FORBIDDEN")
    [denial] = audit_events(AuditAction.ACCESS_DENIED)
    assert denial.target_id == a["id"]  # which item was refused (FR-AUDIT-02)
    assert _ok(_restore(api, bob, a["id"]))["parent_id"] == notes["id"]


def test_an_editor_doesnt_see_what_others_deleted_or_after_losing_edit_rights(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    shared: tuple[Account, Account, Json],
    audit_events: AuditEvents,
) -> None:
    _alice, _folder, notes, a = docs
    alice, bob, _mount = shared
    _delete(api, alice, a)
    b = _child(api, bob, notes["id"], "b.txt")
    _delete(api, bob, b)
    members = api.get(f"/api/v1/nodes/{notes['id']}/members", headers=_auth(alice)).json()
    member_id = members["data"]["members"][0]["id"]

    assert [i["name"] for i in _trash(api, bob)] == ["b.txt"]
    assert _restore(api, bob, a["id"]).status_code == 404  # Alice's delete, not his
    _ok(api.patch(f"/api/v1/members/{member_id}", json={"role": "viewer"}, headers=_auth(alice)))
    assert _trash(api, bob) == []
    assert _restore(api, bob, b["id"]).status_code == 404
    # Refused while holding a role on the folder: audited as denials (FR-AUDIT-01).
    denied = audit_events(AuditAction.ACCESS_DENIED)
    assert [e.target_id for e in denied] == [a["id"], b["id"]]


def test_a_shared_folder_left_by_a_recipient_isnt_in_their_trash(
    api: TestClient, shared: tuple[Account, Account, Json]
) -> None:
    _alice, bob, mount = shared
    _delete(api, bob, mount)

    assert _trash(api, bob) == []
    assert _restore(api, bob, mount["id"]).status_code == 404
    # Emptying removes it too, but doesn't count what the trash never showed.
    assert _ok(api.delete("/api/v1/trash", headers=_auth(bob)))["removed"] == 0
    assert _restore(api, bob, mount["id"]).status_code == 404


def test_the_owners_deleted_shared_folder_comes_back_with_its_contents(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    shared: tuple[Account, Account, Json],
    root_of: RootOf,
) -> None:
    _alice, folder, notes, a = docs
    alice, _bob, _bob_mount = shared
    _delete(api, alice, a)  # deleted on its own first
    member = api.get(f"/api/v1/nodes/{folder['id']}/members", headers=_auth(alice)).json()
    member_id = member["data"]["members"][0]["id"]
    _ok(api.delete(f"/api/v1/members/{member_id}", headers=_auth(alice)), 204)
    owners_mount = _child(api, alice, root_of(alice), "Docs")
    _delete(api, alice, owners_mount)

    blocked = _restore(api, alice, a["id"])
    listed = {i["name"] for i in _trash(api, alice)}
    _ok(_restore(api, alice, owners_mount["id"]))

    assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "SHARE_NOT_ALLOWED")
    assert listed == {"Docs", "a.txt"}
    assert _names(api, alice, owners_mount["id"]) == ["Notes"]
    assert _names(api, alice, notes["id"]) == ["b.txt"]
    assert _ok(_restore(api, alice, a["id"]))["parent_id"] == notes["id"]


def test_a_name_taken_inside_the_deleted_shared_folder_doesnt_block_its_restore(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    shared: tuple[Account, Account, Json],
    root_of: RootOf,
    upload_file: UploadFile,
) -> None:
    _alice, folder, _notes, _a = docs
    alice, _bob, _bob_mount = shared
    member = api.get(f"/api/v1/nodes/{folder['id']}/members", headers=_auth(alice)).json()
    member_id = member["data"]["members"][0]["id"]
    _ok(api.delete(f"/api/v1/members/{member_id}", headers=_auth(alice)), 204)
    owners_mount = _child(api, alice, root_of(alice), "Docs")
    _delete(api, alice, owners_mount)
    # A device that hadn't seen the delete yet adds "Notes" at the shared folder's top.
    _ok(upload_file(alice, b"late", parent_id=folder["id"], name="Notes"))

    _ok(_restore(api, alice, owners_mount["id"]))

    assert sorted(_names(api, alice, owners_mount["id"])) == ["Notes", "Notes (restored)"]


# --- jobs -------------------------------------------------------------------------------------


def test_purge_trash_removes_only_what_is_past_retention_and_can_run_again(
    api: TestClient,
    docs: tuple[Account, Json, Json, Json],
    session_factory: sessionmaker[Session],
    storage_root: Path,
    audit_events: AuditEvents,
) -> None:
    alice, folder, notes, a = docs
    _delete(api, alice, a)
    _delete(api, alice, notes)
    _age(session_factory, a["id"], days=31)
    storage = LocalFilesystemBackend(storage_root)

    first = trash_service.purge_expired(session_factory, storage, _CLI, datetime.now(UTC), 30)
    second = trash_service.purge_expired(session_factory, storage, _CLI, datetime.now(UTC), 30)

    assert (first.items, first.freed_bytes) == (1, 5)
    assert (second.items, second.freed_bytes) == (0, 0)
    assert [i["id"] for i in _trash(api, alice)] == [notes["id"]]
    assert _used_bytes(session_factory, "alice") == 3
    [event] = audit_events(AuditAction.NODE_PURGED)
    assert event.actor_label == "scheduler" and '"automatic": true' in str(event.details)
    assert _ok(api.get(f"/api/v1/nodes/{folder['id']}", headers=_auth(alice)))["name"] == "Docs"


def test_gc_blobs_removes_replaced_versions_and_unreferenced_blobs_after_the_grace_period(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
    storage_root: Path,
) -> None:
    alice = sign_in("alice")
    first = _ok(upload_file(alice, b"v1", parent_id=root_of(alice), name="f.txt"))
    _ok(upload_file(alice, b"v2", node_id=first["id"], base_revision=first["revision"]))
    storage = LocalFilesystemBackend(storage_root)
    stray = "blobs/00/00/" + "0" * 32
    (storage_root / "blobs" / "00" / "00").mkdir(parents=True)
    (storage_root / stray).write_bytes(b"left behind")
    now = datetime.now(UTC)

    early = blob_service.collect(session_factory, storage, now, grace_hours=24)
    later = blob_service.collect(session_factory, storage, now + timedelta(hours=25), 24)
    again = blob_service.collect(session_factory, storage, now + timedelta(hours=25), 24)

    assert (early.versions, early.orphans) == (0, 0)
    assert (later.versions, later.orphans) == (1, 1)  # the replaced v1, and the stray blob
    assert (again.versions, again.orphans) == (0, 0)
    with session_factory() as session:
        [kept] = session.scalars(select(FileVersion)).all()
    assert [key for key, _ in storage.blobs()] == [kept.blob_key]
    content = api.get(f"/api/v1/nodes/{first['id']}/content", headers=_auth(alice))
    assert content.content == b"v2"


def test_gc_blobs_refuses_when_most_blobs_look_unreferenced(
    session_factory: sessionmaker[Session], storage_root: Path
) -> None:
    storage = LocalFilesystemBackend(storage_root)
    (storage_root / "blobs" / "00" / "00").mkdir(parents=True)
    for n in range(blob_service.ORPHAN_REFUSE_COUNT + 1):
        (storage_root / "blobs" / "00" / "00" / f"{n:032x}").write_bytes(b"x")
    later = datetime.now(UTC) + timedelta(hours=25)

    with pytest.raises(blob_service.TooManyOrphansError):
        blob_service.collect(session_factory, storage, later, 24)
    kept = len(list(storage.blobs()))
    forced = blob_service.collect(session_factory, storage, later, 24, force=True)

    assert kept == blob_service.ORPHAN_REFUSE_COUNT + 1
    assert forced.orphans == kept
    assert list(storage.blobs()) == []


def test_purge_audit_logs_a_purge_that_failed_partway(
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory.begin() as session:
        for _ in range(3):
            audit_service.record(session, _CLI, AuditAction.USER_LISTED)
        session.flush()
        session.execute(
            update(AuditEvent).values(occurred_at=datetime.now(UTC) - timedelta(days=400))
        )
    monkeypatch.setattr(audit_service, "_PURGE_BATCH", 1)
    real = audit_retention.delete_before
    calls: list[int] = []

    def fails_second_time(session: Session, before: datetime, limit: int) -> int:
        calls.append(limit)
        if len(calls) == 2:
            raise OperationalError("DELETE", {}, Exception("connection lost"))
        return real(session, before, limit)

    monkeypatch.setattr(audit_retention, "delete_before", fails_second_time)

    with pytest.raises(OperationalError):
        audit_service.purge_expired(session_factory, session_factory, _CLI, datetime.now(UTC), 365)

    [logged] = audit_events(AuditAction.AUDIT_PURGED)
    assert logged.outcome == AuditOutcome.FAILURE and '"removed": 1' in str(logged.details)


def test_purge_audit_removes_old_events_and_records_that_it_ran(
    session_factory: sessionmaker[Session], audit_events: AuditEvents
) -> None:
    with session_factory.begin() as session:
        audit_service.record(session, _CLI, AuditAction.USER_LISTED)
        audit_service.record(session, _CLI, AuditAction.GROUP_LISTED)
        session.flush()
        session.execute(
            update(AuditEvent)
            .where(AuditEvent.action == AuditAction.USER_LISTED)
            .values(occurred_at=datetime.now(UTC) - timedelta(days=400))
        )

    removed = audit_service.purge_expired(
        session_factory, session_factory, _CLI, datetime.now(UTC), 365
    )

    assert removed == 1
    assert [e.action for e in audit_events()] == [
        AuditAction.GROUP_LISTED,
        AuditAction.AUDIT_PURGED,
    ]
    [purged] = audit_events(AuditAction.AUDIT_PURGED)
    assert purged.outcome == AuditOutcome.SUCCESS and '"removed": 1' in str(purged.details)


def test_restored_name_keeps_the_extension_and_the_length_limit() -> None:
    assert trash_service.restored_name("a.txt", NodeKind.FILE, 1) == "a (restored).txt"
    assert trash_service.restored_name(".env", NodeKind.FILE, 2) == ".env (restored 2)"
    assert trash_service.restored_name("v1.2", NodeKind.FOLDER, 1) == "v1.2 (restored)"
    long = trash_service.restored_name("x" * 250 + ".pdf", NodeKind.FILE, 1)
    assert len(long) == 255 and long.endswith(" (restored).pdf")
