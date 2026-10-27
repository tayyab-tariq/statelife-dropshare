"""Resumable chunked uploads (FR-XFER-01, NFR-REL-02/03/04, NFR-SCALE-01/03, FR-AUDIT-01).

Test settings use 4-byte chunks, a 4096-byte file size limit and a 1024-byte quota.
"""

import hashlib
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, Change, FileVersion, UploadSession, User
from dropshare_server.repositories import uploads as upload_repository
from dropshare_server.services import uploads as upload_service
from dropshare_server.storage.local import LocalFilesystemBackend
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type UploadFile = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _start(api: TestClient, user: Account, size: int, sha256: str, **target: object) -> Response:
    body = {key: str(value) if key.endswith("_id") else value for key, value in target.items()}
    response: Response = api.post(
        "/api/v1/uploads",
        json={**body, "size_bytes": size, "sha256": sha256},
        headers=_auth(user),
    )
    return response


def _put(
    api: TestClient, user: Account, upload_id: str, index: int, chunk: bytes, sha: str | None = None
) -> Response:
    response: Response = api.put(
        f"/api/v1/uploads/{upload_id}/chunks/{index}",
        content=chunk,
        headers={**_auth(user), "X-Chunk-SHA256": sha or _sha(chunk)},
    )
    return response


def _complete(api: TestClient, user: Account, upload_id: str) -> Response:
    response: Response = api.post(f"/api/v1/uploads/{upload_id}/complete", headers=_auth(user))
    return response


def _changes(session_factory: sessionmaker[Session]) -> list[Change]:
    with session_factory() as session:
        return list(session.scalars(select(Change).order_by(Change.id)))


def _used_bytes(session_factory: sessionmaker[Session], username: str) -> int:
    with session_factory() as session:
        return session.scalars(select(User.used_bytes).where(User.username == username)).one()


def test_upload_creates_a_verified_file(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
    storage_root: Path,
) -> None:
    alice = sign_in()
    content = b"hello, dropshare"  # 16 bytes: four chunks

    response = upload_file(alice, content, parent_id=root_of(alice), name="hello.txt")

    assert response.status_code == 200, response.text
    node = response.json()["data"]
    assert (node["name"], node["kind"], node["size_bytes"], node["revision"]) == (
        "hello.txt",
        "file",
        16,
        1,
    )
    assert node["content_hash"] == _sha(content)
    with session_factory() as session:
        version = session.scalars(select(FileVersion)).one()
    blob = storage_root.joinpath(*version.blob_key.split("/"))
    assert blob.read_bytes() == content
    assert not (storage_root / "staging").exists() or not any((storage_root / "staging").iterdir())
    [change] = _changes(session_factory)
    assert (str(change.node_id), change.change_type, change.content_hash) == (
        node["id"],
        "created",
        _sha(content),
    )
    [event] = audit_events(AuditAction.FILE_UPLOADED)
    assert (event.target_id, event.outcome) == (node["id"], AuditOutcome.SUCCESS)
    assert _used_bytes(session_factory, "alice") == 16


def test_status_lists_received_chunks_so_a_client_can_resume(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    content = b"0123456789"  # chunks: 0123 4567 89
    upload = _start(api, alice, 10, _sha(content), parent_id=root_of(alice), name="n.bin").json()
    upload_id = upload["data"]["id"]
    _put(api, alice, upload_id, 0, b"0123")
    _put(api, alice, upload_id, 2, b"89")

    status = api.get(f"/api/v1/uploads/{upload_id}", headers=_auth(alice)).json()["data"]
    _put(api, alice, upload_id, 1, b"4567")
    _put(api, alice, upload_id, 1, b"4567")  # re-sending a chunk is harmless

    assert (status["chunk_count"], status["received_chunks"]) == (3, [0, 2])
    assert _complete(api, alice, upload_id).status_code == 200


def test_corrupt_chunk_is_rejected_and_not_stored(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    upload_id = _start(api, alice, 4, _sha(b"abcd"), parent_id=root_of(alice), name="c").json()[
        "data"
    ]["id"]

    response = _put(api, alice, upload_id, 0, b"abcd", sha=_sha(b"abce"))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHUNK_HASH_MISMATCH"
    status = api.get(f"/api/v1/uploads/{upload_id}", headers=_auth(alice)).json()["data"]
    assert status["received_chunks"] == []


def test_chunks_must_have_the_exact_size_and_a_valid_index(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    upload_id = _start(api, alice, 6, _sha(b"abcdef"), parent_id=root_of(alice), name="s").json()[
        "data"
    ]["id"]

    too_long = _put(api, alice, upload_id, 0, b"abcde")
    too_short_last = _put(api, alice, upload_id, 1, b"e")
    out_of_range = _put(api, alice, upload_id, 2, b"ef")
    no_hash = api.put(
        f"/api/v1/uploads/{upload_id}/chunks/0", content=b"abcd", headers=_auth(alice)
    )

    assert [r.status_code for r in (too_long, too_short_last, out_of_range, no_hash)] == [400] * 4


def test_complete_reports_missing_chunks(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()
    upload_id = _start(api, alice, 8, _sha(b"abcdefgh"), parent_id=root_of(alice), name="m").json()[
        "data"
    ]["id"]
    _put(api, alice, upload_id, 1, b"efgh")

    response = _complete(api, alice, upload_id)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UPLOAD_INCOMPLETE"
    assert response.json()["error"]["details"] == {"missing_chunks": [0]}


def test_file_hash_mismatch_cancels_the_upload_and_is_audited(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    root = root_of(alice)
    # Every chunk is intact, but the whole file isn't what the client declared.
    upload_id = _start(api, alice, 4, _sha(b"other"), parent_id=root, name="f.txt").json()["data"][
        "id"
    ]
    _put(api, alice, upload_id, 0, b"abcd")

    response = _complete(api, alice, upload_id)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "FILE_HASH_MISMATCH"
    assert api.get(f"/api/v1/nodes/{root}/children", headers=_auth(alice)).json()["data"] == []
    assert _complete(api, alice, upload_id).status_code == 404  # cancelled for good
    assert _changes(session_factory) == []
    [event] = audit_events(AuditAction.FILE_UPLOADED)
    assert event.outcome == AuditOutcome.FAILURE
    assert _used_bytes(session_factory, "alice") == 0


def test_completing_again_returns_the_same_file(
    api: TestClient, sign_in: SignIn, root_of: RootOf, session_factory: sessionmaker[Session]
) -> None:
    alice = sign_in()
    upload_id = _start(api, alice, 3, _sha(b"abc"), parent_id=root_of(alice), name="r").json()[
        "data"
    ]["id"]
    _put(api, alice, upload_id, 0, b"abc")

    first = _complete(api, alice, upload_id)
    retry = _complete(api, alice, upload_id)  # e.g. the first response was lost (NFR-REL-04)

    assert (first.status_code, retry.status_code) == (200, 200)
    assert retry.json()["data"] == first.json()["data"]
    assert len(_changes(session_factory)) == 1
    assert _used_bytes(session_factory, "alice") == 3


def test_completing_again_after_the_file_was_trashed_is_a_conflict(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    """A lost answer, then another device trashed the file: the retry must not report a
    trashed file as the client's live upload (it uploads again as a new file)."""
    alice = sign_in()
    upload_id = _start(api, alice, 3, _sha(b"abc"), parent_id=root_of(alice), name="r").json()[
        "data"
    ]["id"]
    _put(api, alice, upload_id, 0, b"abc")
    node = _complete(api, alice, upload_id).json()["data"]
    trashed = api.delete(
        f"/api/v1/nodes/{node['id']}?if_revision={node['revision']}",
        headers={"Authorization": alice["authorization"]},
    )
    assert trashed.status_code == 204

    retry = _complete(api, alice, upload_id)

    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "REVISION_CONFLICT"
    assert retry.json()["error"]["details"] == {"current_revision": None}


def test_empty_file_needs_no_chunks(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()

    response = upload_file(alice, b"", parent_id=root_of(alice), name="empty.txt")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["size_bytes"] == 0


def test_new_version_needs_the_current_revision(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    upload_file: UploadFile,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    file = upload_file(alice, b"v1", parent_id=root_of(alice), name="doc.txt").json()["data"]

    updated = upload_file(alice, b"version 2", node_id=file["id"], base_revision=1)
    stale = _start(api, alice, 2, _sha(b"v3"), node_id=file["id"], base_revision=1)

    assert updated.status_code == 200, updated.text
    node = updated.json()["data"]
    assert (node["id"], node["revision"], node["content_hash"]) == (
        file["id"],
        2,
        _sha(b"version 2"),
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "REVISION_CONFLICT",
        "message": "This item changed on the server.",
        "details": {"current_revision": 2},
    }
    assert [c.change_type for c in _changes(session_factory)] == ["created", "modified"]
    assert _used_bytes(session_factory, "alice") == 9


def test_file_changed_during_the_upload_is_a_conflict_at_completion(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    file = upload_file(alice, b"v1", parent_id=root_of(alice), name="doc.txt").json()["data"]
    slow = _start(api, alice, 2, _sha(b"v2"), node_id=file["id"], base_revision=1).json()["data"]
    _put(api, alice, slow["id"], 0, b"v2")
    upload_file(alice, b"other device", node_id=file["id"], base_revision=1)

    response = _complete(api, alice, slow["id"])

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"current_revision": 2}
    current = api.get(f"/api/v1/nodes/{file['id']}", headers=_auth(alice)).json()["data"]
    assert current["content_hash"] == _sha(b"other device")  # nothing was overwritten


def test_file_names_collide_case_insensitively(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    root = root_of(alice)
    upload_file(alice, b"one", parent_id=root, name="Notes.txt")
    late = _start(api, alice, 3, _sha(b"two"), parent_id=root, name="Other.txt").json()["data"]
    _put(api, alice, late["id"], 0, b"two")
    upload_file(alice, b"three", parent_id=root, name="OTHER.TXT")

    at_start = _start(api, alice, 3, _sha(b"two"), parent_id=root, name="NOTES.TXT")
    at_completion = _complete(api, alice, late["id"])

    for response in (at_start, at_completion):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "NAME_CONFLICT"


def test_files_over_the_size_limit_are_refused(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()

    response = _start(api, alice, 4097, _sha(b""), parent_id=root_of(alice), name="big.iso")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_quota_counts_stored_files_and_pending_uploads(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    alice = sign_in()
    root = root_of(alice)
    upload_file(alice, b"x" * 400, parent_id=root, name="stored.bin")
    pending = _start(api, alice, 400, _sha(b"y" * 400), parent_id=root, name="pending.bin")

    over = _start(api, alice, 300, _sha(b"z" * 300), parent_id=root, name="over.bin")
    fits = _start(api, alice, 224, _sha(b"z" * 224), parent_id=root, name="fits.bin")

    assert pending.status_code == 201
    assert over.status_code == 409
    assert over.json()["error"]["code"] == "QUOTA_EXCEEDED"
    assert fits.status_code == 201  # 400 stored + 400 + 224 reserved = 1024


def test_cancelled_upload_cannot_be_continued(
    api: TestClient, sign_in: SignIn, root_of: RootOf, storage_root: Path
) -> None:
    alice = sign_in()
    upload_id = _start(api, alice, 8, _sha(b"abcdefgh"), parent_id=root_of(alice), name="x").json()[
        "data"
    ]["id"]
    _put(api, alice, upload_id, 0, b"abcd")

    cancelled = api.delete(f"/api/v1/uploads/{upload_id}", headers=_auth(alice))

    assert cancelled.status_code == 204
    assert _put(api, alice, upload_id, 1, b"efgh").status_code == 404
    assert _complete(api, alice, upload_id).status_code == 404
    assert not (storage_root / "staging" / uuid.UUID(upload_id).hex).exists()


def test_another_users_upload_and_folders_are_off_limits(
    api: TestClient, sign_in: SignIn, root_of: RootOf, audit_events: AuditEvents
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    upload_id = _start(api, alice, 4, _sha(b"abcd"), parent_id=root_of(alice), name="a").json()[
        "data"
    ]["id"]

    responses = [
        _start(api, bob, 4, _sha(b"abcd"), parent_id=root_of(alice), name="b"),
        api.get(f"/api/v1/uploads/{upload_id}", headers=_auth(bob)),
        _put(api, bob, upload_id, 0, b"abcd"),
        _complete(api, bob, upload_id),
        api.delete(f"/api/v1/uploads/{upload_id}", headers=_auth(bob)),
    ]

    assert [r.status_code for r in responses] == [404] * 5
    assert len(audit_events(AuditAction.ACCESS_DENIED)) == 5


def test_upload_target_must_be_exactly_one_kind(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    root = root_of(alice)

    both = _start(
        api, alice, 1, _sha(b"a"), parent_id=root, name="a", node_id=root, base_revision=1
    )
    half = _start(api, alice, 1, _sha(b"a"), parent_id=root)

    assert (both.status_code, half.status_code) == (400, 400)


def test_new_versions_reserve_their_full_size(
    api: TestClient, sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> None:
    """A same-size edit still stages its whole size on disk, so it counts until done (D1)."""
    alice = sign_in()
    file = upload_file(alice, b"x" * 600, parent_id=root_of(alice), name="big.bin").json()["data"]

    edit = _start(api, alice, 600, _sha(b"y" * 600), node_id=file["id"], base_revision=1)

    assert edit.status_code == 409  # 600 stored + 600 staged > 1024
    assert edit.json()["error"]["code"] == "QUOTA_EXCEEDED"


def test_validation_details_are_always_a_list_of_issues(
    api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    upload_id = _start(api, alice, 6, _sha(b"abcdef"), parent_id=root_of(alice), name="d").json()[
        "data"
    ]["id"]

    responses = {
        "chunk_size": _put(api, alice, upload_id, 0, b"abc"),
        "chunk_index": _put(api, alice, upload_id, 9, b"ab"),
        "chunk_sha256": _put(api, alice, upload_id, 1, b"ef", sha="not-a-hash"),
    }

    for issue_type, response in responses.items():
        assert response.status_code == 400
        [issue] = response.json()["error"]["details"]
        assert issue["type"] == issue_type


def test_chunk_limit_releases_the_database_connection(
    api: TestClient, sign_in: SignIn, root_of: RootOf, session_factory: sessionmaker[Session]
) -> None:
    """No pooled connection may be held while a slow client sends a chunk body (audit S1)."""
    alice = sign_in()
    upload_id = _start(api, alice, 4, _sha(b"abcd"), parent_id=root_of(alice), name="c").json()[
        "data"
    ]["id"]
    user_id = uuid.UUID(api.get("/api/v1/me", headers=_auth(alice)).json()["data"]["id"])

    with session_factory() as session:
        limit = upload_service.chunk_limit(session, user_id, uuid.UUID(upload_id), 0)

        assert limit == 4
        assert not session.in_transaction()


def test_expire_job_ends_old_uploads_and_removes_unowned_chunks(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
) -> None:
    alice = sign_in()
    root = root_of(alice)
    ids = {}
    for name in ("old", "cancelled", "active"):
        ids[name] = _start(api, alice, 8, _sha(b"abcdefgh"), parent_id=root, name=name).json()[
            "data"
        ]["id"]
        _put(api, alice, ids[name], 0, b"abcd")
    api.delete(f"/api/v1/uploads/{ids['cancelled']}", headers=_auth(alice))
    storage = LocalFilesystemBackend(storage_root)
    storage.write_chunk(uuid.UUID(ids["cancelled"]), 1, b"efgh")  # a chunk PUT that raced cancel
    with session_factory.begin() as session:
        session.get_one(UploadSession, uuid.UUID(ids["old"])).expires_at = datetime.now(
            UTC
        ) - timedelta(minutes=1)

    result = upload_service.expire_uploads(session_factory, storage, datetime.now(UTC))
    again = upload_service.expire_uploads(session_factory, storage, datetime.now(UTC))

    assert (result.expired, result.swept) == (1, 2)
    assert (again.expired, again.swept) == (0, 0)
    assert list(storage.staged_upload_ids()) == [uuid.UUID(ids["active"])]
    status = api.get(f"/api/v1/uploads/{ids['old']}", headers=_auth(alice)).json()["data"]
    assert status["status"] == "expired"
    # Only the active upload still reserves quota.
    with session_factory() as session:
        reserved = upload_repository.reserved_bytes(
            session, uuid.UUID(_user_id(api, alice)), datetime.now(UTC)
        )
    assert reserved == 8


def _user_id(api: TestClient, user: Account) -> str:
    user_id: str = api.get("/api/v1/me", headers=_auth(user)).json()["data"]["id"]
    return user_id


def test_denied_upload_access_records_the_target(
    api: TestClient, sign_in: SignIn, root_of: RootOf, audit_events: AuditEvents
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    upload_id = _start(api, alice, 4, _sha(b"abcd"), parent_id=root_of(alice), name="a").json()[
        "data"
    ]["id"]

    api.get(f"/api/v1/uploads/{upload_id}", headers=_auth(bob))

    [event] = audit_events(AuditAction.ACCESS_DENIED)
    assert (event.target_type, event.target_id) == ("upload", upload_id)
