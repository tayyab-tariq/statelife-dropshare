"""Interleavings that clients rely on (audit B1, B2, S2, S9; NFR-REL-04, FR-SYNC-03)."""

import hashlib
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

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


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _mkdir(api: TestClient, user: Account, parent_id: str, name: str) -> dict[str, object]:
    response = api.post(
        "/api/v1/nodes", json={"parent_id": parent_id, "name": name}, headers=_auth(user)
    )
    assert response.status_code == 201, response.text
    created: dict[str, object] = response.json()["data"]
    return created


def _blobs(storage_root: Path) -> list[Path]:
    blobs = storage_root / "blobs"
    return [p for p in blobs.rglob("*") if p.is_file()] if blobs.exists() else []


def test_change_cursors_follow_commit_order(
    api: TestClient, sign_in: SignIn, root_of: RootOf, session_factory: sessionmaker[Session]
) -> None:
    """A write that numbers its change first but commits later must not end up below a cursor a
    client has already seen: writers to one namespace are serialised on the cursor."""
    alice = sign_in()
    root = root_of(alice)
    folder_a, folder_b = _mkdir(api, alice, root, "A"), _mkdir(api, alice, root, "B")
    user_id = uuid.UUID(api.get("/api/v1/me", headers=_auth(alice)).json()["data"]["id"])
    ctx = CallContext(actor=Actor(AuditActorType.USER, user_id=user_id))

    def create(session: Session, parent: object, name: str) -> None:
        node_service.create_folder(
            session, ctx, user_id, FolderCreateIn(parent_id=uuid.UUID(str(parent)), name=name)
        )

    def second_writer() -> None:
        with session_factory.begin() as session:
            create(session, folder_b["id"], "second")

    with session_factory() as first:
        create(first, folder_a["id"], "first")  # numbered, not committed yet
        writer = threading.Thread(target=second_writer)
        writer.start()
        time.sleep(0.5)
        blocked = writer.is_alive()
        first.commit()
    writer.join(timeout=10)

    namespace = api.get("/api/v1/namespaces", headers=_auth(alice)).json()["data"][0]["id"]
    changes = api.get(f"/api/v1/namespaces/{namespace}/changes", headers=_auth(alice)).json()
    names = [change["name"] for change in changes["data"]]
    cursors = [change["cursor"] for change in changes["data"]]
    assert blocked, "the second writer must wait for the first to commit"
    assert names == ["A", "B", "first", "second"]
    assert cursors == [1, 2, 3, 4]


def test_repeated_complete_returns_this_uploads_result_not_later_edits(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    """Device A's response is lost; device B edits; A retries. A must get its own revision, or it
    would later overwrite B's edit without a conflict."""
    alice = sign_in()
    file = upload_file(alice, b"v1", parent_id=root_of(alice), name="doc.txt").json()["data"]
    started = api.post(
        "/api/v1/uploads",
        json={"node_id": file["id"], "base_revision": 1, "size_bytes": 4, "sha256": _sha(b"by A")},
        headers=_auth(alice),
    ).json()["data"]
    api.put(
        f"/api/v1/uploads/{started['id']}/chunks/0",
        content=b"by A",
        headers={**_auth(alice), "X-Chunk-SHA256": _sha(b"by A")},
    )
    first = api.post(f"/api/v1/uploads/{started['id']}/complete", headers=_auth(alice))
    upload_file(alice, b"by B", node_id=file["id"], base_revision=2)
    # (Trashed meanwhile is a conflict instead: test_uploads_api, audit S1.)

    retry = api.post(f"/api/v1/uploads/{started['id']}/complete", headers=_auth(alice))

    assert retry.status_code == 200, retry.text
    replayed = retry.json()["data"]
    assert (replayed["revision"], replayed["content_hash"]) == (2, _sha(b"by A"))
    assert replayed == first.json()["data"]


def test_conflict_at_completion_keeps_the_upload_retryable(
    api: TestClient, sign_in: SignIn, root_of: RootOf, storage_root: Path
) -> None:
    """A failure after assembling must not consume the chunks or leave a blob behind."""
    alice = sign_in()
    root = root_of(alice)
    late = api.post(
        "/api/v1/uploads",
        json={"parent_id": root, "name": "same.txt", "size_bytes": 4, "sha256": _sha(b"late")},
        headers=_auth(alice),
    ).json()["data"]
    api.put(
        f"/api/v1/uploads/{late['id']}/chunks/0",
        content=b"late",
        headers={**_auth(alice), "X-Chunk-SHA256": _sha(b"late")},
    )
    winner = _mkdir(api, alice, root, "SAME.TXT")

    conflict = api.post(f"/api/v1/uploads/{late['id']}/complete", headers=_auth(alice))
    status = api.get(f"/api/v1/uploads/{late['id']}", headers=_auth(alice)).json()["data"]

    assert conflict.status_code == 409
    assert conflict.json()["error"]["details"] == {"node_id": winner["id"], "kind": "folder"}
    assert (status["status"], status["received_chunks"]) == ("pending", [0])
    assert _blobs(storage_root) == []


def test_repeated_delete_confirms_success(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    folder = _mkdir(api, alice, root_of(alice), "Old")
    url = f"/api/v1/nodes/{folder['id']}?if_revision=1"

    first = api.delete(url, headers=_auth(alice))
    retry = api.delete(url, headers=_auth(alice))  # the first response was lost
    stale = api.delete(f"/api/v1/nodes/{folder['id']}?if_revision=5", headers=_auth(alice))

    assert (first.status_code, retry.status_code, stale.status_code) == (204, 204, 404)
