"""Malware scanning of uploads (FR-SAFE-01, OQ-55, OQ-57 (5), OQ-58) with the fake scanner:
within `complete` for small files, by the scan worker for large ones or while the scanner is
down. A version is visible only once it has scanned clean."""

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from scanner_fake import SIGNATURE, FakeScanner
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app
from dropshare_server.models import AuditEvent, Device, FileVersion, Node, UploadSession, User
from dropshare_server.repositories import uploads as upload_repository
from dropshare_server.services import blobs as blob_service
from dropshare_server.services import scans as scan_service
from dropshare_server.services import uploads as upload_service
from dropshare_server.services.scans import ScanOutcome
from dropshare_server.storage.local import LocalFilesystemBackend
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome
from dropshare_shared.schemas.uploads import UploadStatus
from dropshare_shared.schemas.users import UserStatus

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type AuditEvents = Callable[..., list[AuditEvent]]
type Json = dict[str, object]

_VIRUS = b"VIRUS"  # content holding this is "infected" for the fake scanner
_DETECTION = "Win.Test.Fake-1"


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _ok(response: Response, status: int = 200) -> Json:
    assert response.status_code == status, response.text
    data: Json = response.json()["data"]
    return data


def _start(client: TestClient, user: Account, content: bytes, **target: object) -> Json:
    body = {key: str(value) if key.endswith("_id") else value for key, value in target.items()}
    response = client.post(
        "/api/v1/uploads",
        json={**body, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()},
        headers=_auth(user),
    )
    return _ok(response, 201)


def _send(client: TestClient, user: Account, upload: Json, content: bytes) -> None:
    size = int(str(upload["chunk_size"]))
    for index in range(int(str(upload["chunk_count"]))):
        chunk = content[index * size : (index + 1) * size]
        sent = client.put(
            f"/api/v1/uploads/{upload['id']}/chunks/{index}",
            content=chunk,
            headers={**_auth(user), "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
        )
        assert sent.status_code == 204, sent.text


def _complete(client: TestClient, user: Account, upload: Json) -> Response:
    response: Response = client.post(
        f"/api/v1/uploads/{upload['id']}/complete", headers=_auth(user)
    )
    return response


def _upload(
    client: TestClient, user: Account, content: bytes, **target: object
) -> tuple[Json, Response]:
    upload = _start(client, user, content, **target)
    _send(client, user, upload, content)
    return upload, _complete(client, user, upload)


def _malware(response: Response) -> str:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "MALWARE_DETECTED"
    detection: str = error["details"]["detection"]
    assert detection in error["message"]
    return detection


def _upload_row(sessions: sessionmaker[Session], upload: Json) -> UploadSession:
    with sessions() as session:
        row = session.get(UploadSession, upload["id"])
        assert row is not None
        return row


def _blob_exists(storage_root: Path, blob_key: str | None) -> bool:
    assert blob_key is not None
    return storage_root.joinpath(*blob_key.split("/")).is_file()


def _staged(storage_root: Path) -> list[Path]:
    staging = storage_root / "staging"
    return list(staging.iterdir()) if staging.is_dir() else []


def _details(event: AuditEvent) -> Json:
    details: Json = json.loads(event.details or "{}")
    return details


@pytest.fixture
def worker(
    session_factory: sessionmaker[Session],
    storage_root: Path,
    scanner: FakeScanner,
    db_settings: Settings,
) -> Callable[[], ScanOutcome]:
    """Run the scan worker once, now."""
    storage = LocalFilesystemBackend(storage_root)

    def run() -> ScanOutcome:
        return scan_service.scan_next(
            session_factory, storage, scanner, db_settings, datetime.now(UTC)
        )

    return run


@pytest.fixture
def scanning_api(db_settings: Settings) -> Iterator[TestClient]:
    """A server that leaves every non-empty file to the scan worker (as for files over 64 MiB)."""
    settings = db_settings.model_copy(update={"scan_inline_max_bytes": 0})
    with TestClient(
        create_app(settings), raise_server_exceptions=False, headers={"X-Client-Version": "0.2.0"}
    ) as client:
        yield client


# --- Scanned within `complete` -------------------------------------------------------------


def test_a_clean_file_is_scanned_and_visible_at_once(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    scanner: FakeScanner,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()

    _, response = _upload(api, alice, b"hello there", parent_id=root_of(alice), name="a.txt")

    node = _ok(response)
    assert node["name"] == "a.txt"
    assert scanner.scanned == [b"hello there"]
    with session_factory() as session:
        version = session.scalars(select(FileVersion)).one()
    assert version.scan_signature == SIGNATURE
    assert version.scanned_at is not None


def test_an_infected_file_is_refused_and_never_visible(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    scanner: FakeScanner,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    audit_events: AuditEvents,
) -> None:
    scanner.detect(_VIRUS, _DETECTION)
    alice = sign_in()
    root = root_of(alice)

    upload, response = _upload(api, alice, b"a VIRUS here", parent_id=root, name="a.docx")

    assert _malware(response) == _DETECTION
    with session_factory() as session:
        assert session.scalars(select(Node).where(Node.name == "a.docx")).all() == []
        assert session.scalars(select(FileVersion)).all() == []
    row = _upload_row(session_factory, upload)
    assert (row.status, row.detection) == (UploadStatus.REJECTED, _DETECTION)
    assert _staged(storage_root) == []
    assert [
        path for path in storage_root.rglob("*") if path.is_file()
    ] == []  # blob and chunks gone
    [event] = audit_events(AuditAction.FILE_MALWARE_DETECTED)
    assert (event.outcome, event.target_name, event.target_id) == (
        AuditOutcome.DENIED,
        "a.docx",
        root,
    )
    assert _details(event)["detection"] == _DETECTION
    assert _details(event)["folder_id"] == root
    assert event.device_id is not None and event.source_ip is not None
    assert "VIRUS" not in (event.details or "")  # never content (FR-AUDIT-02)
    assert audit_events(AuditAction.FILE_UPLOADED) == []


def test_completing_a_refused_upload_again_answers_the_same_without_rescanning(
    api: TestClient, sign_in: SignIn, root_of: RootOf, scanner: FakeScanner
) -> None:
    scanner.detect(_VIRUS, _DETECTION)
    alice = sign_in()
    upload, first = _upload(api, alice, b"VIRUS", parent_id=root_of(alice), name="a.txt")

    again = _complete(api, alice, upload)

    assert _malware(first) == _malware(again) == _DETECTION
    assert len(scanner.scanned) == 1
    # Every chunk counts as received, so no client sends the file again for this upload.
    status = _ok(api.get(f"/api/v1/uploads/{upload['id']}", headers=_auth(alice)))
    assert status["status"] == "rejected"
    assert status["received_chunks"] == list(range(int(str(status["chunk_count"]))))


def test_an_infected_new_version_leaves_the_old_one_current(
    api: TestClient, sign_in: SignIn, root_of: RootOf, scanner: FakeScanner
) -> None:
    scanner.detect(_VIRUS, _DETECTION)
    alice = sign_in()
    _, created = _upload(api, alice, b"good", parent_id=root_of(alice), name="a.txt")
    old = _ok(created)

    _, response = _upload(api, alice, b"VIRUS!", node_id=old["id"], base_revision=old["revision"])

    _malware(response)
    current = _ok(api.get(f"/api/v1/nodes/{old['id']}", headers=_auth(alice)))
    assert (current["revision"], current["content_hash"]) == (old["revision"], old["content_hash"])
    content = api.get(f"/api/v1/nodes/{old['id']}/content", headers=_auth(alice))
    assert content.content == b"good"


def test_an_infected_file_in_a_shared_folder_is_refused_for_an_editor(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    scanner: FakeScanner,
    audit_events: AuditEvents,
) -> None:
    scanner.detect(_VIRUS, _DETECTION)
    alice, bob = sign_in("alice"), sign_in("bob")
    team = _ok(
        api.post(
            "/api/v1/nodes",
            json={"parent_id": root_of(alice), "name": "Team"},
            headers=_auth(alice),
        ),
        201,
    )
    bob_id = _ok(api.get("/api/v1/me", headers=_auth(bob)))["id"]
    _ok(
        api.post(
            f"/api/v1/nodes/{team['id']}/members",
            json={"user_id": bob_id, "role": "editor"},
            headers=_auth(alice),
        ),
        201,
    )

    _, response = _upload(api, bob, b"VIRUS", parent_id=team["id"], name="b.txt")

    _malware(response)
    [event] = audit_events(AuditAction.FILE_MALWARE_DETECTED)
    assert str(event.actor_user_id) == bob_id


# --- Left to the scan worker ----------------------------------------------------------------


def test_while_the_scanner_is_down_the_upload_waits_and_nothing_is_visible(
    api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    scanner: FakeScanner,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    worker: Callable[[], ScanOutcome],
) -> None:
    scanner.down = True
    alice = sign_in()
    root = root_of(alice)

    upload, response = _upload(api, alice, b"report", parent_id=root, name="r.txt")

    assert response.status_code == 202, response.text
    waiting = response.json()["data"]
    assert (waiting["id"], waiting["status"]) == (upload["id"], "scanning")
    assert waiting["received_chunks"] == list(range(int(str(upload["chunk_count"]))))
    assert _items(api, alice, root) == []
    assert _complete(api, alice, upload).status_code == 202  # asking again doesn't wait
    row = _upload_row(session_factory, upload)
    assert _blob_exists(storage_root, row.blob_key)
    assert _staged(storage_root) == []  # the blob holds every byte now

    # A scanner that doesn't answer at all counts against no file: the upload waits untouched.
    assert worker() == ScanOutcome.SCANNER_DOWN
    assert worker() == ScanOutcome.SCANNER_DOWN
    assert _upload_row(session_factory, upload).scan_attempts == 0

    scanner.down = False
    assert worker() == ScanOutcome.CLEAN
    node = _ok(_complete(api, alice, upload))
    assert node["name"] == "r.txt"
    assert [item["id"] for item in _items(api, alice, root)] == [node["id"]]


def test_a_large_file_is_scanned_by_the_worker_and_completed_once(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    scanner: FakeScanner,
    session_factory: sessionmaker[Session],
    worker: Callable[[], ScanOutcome],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    upload, response = _upload(
        scanning_api, alice, b"big file", parent_id=root_of(alice), name="b.iso.txt"
    )
    assert response.status_code == 202
    assert scanner.scanned == []

    assert worker() == ScanOutcome.CLEAN
    assert worker() == ScanOutcome.NOTHING

    first = _ok(_complete(scanning_api, alice, upload))
    second = _ok(_complete(scanning_api, alice, upload))
    assert first == second  # NFR-REL-04: no second version
    assert scanner.scanned == [b"big file"]
    with session_factory() as session:
        assert len(session.scalars(select(FileVersion)).all()) == 1
    [event] = audit_events(AuditAction.FILE_UPLOADED)
    assert event.device_id is not None and event.source_ip is not None  # as the uploader


def test_the_worker_refuses_an_infected_file_and_audits_it_as_the_uploader(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    scanner: FakeScanner,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    worker: Callable[[], ScanOutcome],
    audit_events: AuditEvents,
) -> None:
    scanner.detect(_VIRUS, _DETECTION)
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"VIRUS", parent_id=root_of(alice), name="v.txt")
    blob_key = _upload_row(session_factory, upload).blob_key

    assert worker() == ScanOutcome.INFECTED

    assert _malware(_complete(scanning_api, alice, upload)) == _DETECTION
    assert not _blob_exists(storage_root, blob_key)
    [event] = audit_events(AuditAction.FILE_MALWARE_DETECTED)
    alice_id = _ok(scanning_api.get("/api/v1/me", headers=_auth(alice)))["id"]
    assert str(event.actor_user_id) == alice_id
    assert event.device_id is not None and event.source_ip is not None


def test_a_file_edited_elsewhere_while_it_waited_is_refused_as_a_conflict(
    api: TestClient,
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    worker: Callable[[], ScanOutcome],
) -> None:
    # NFR-REL-04: two uploads of one file racing through scanning keep revisions right.
    alice = sign_in()
    _, created = _upload(api, alice, b"v1", parent_id=root_of(alice), name="a.txt")
    v1 = _ok(created)
    slow, response = _upload(
        scanning_api, alice, b"v2 slow", node_id=v1["id"], base_revision=v1["revision"]
    )
    assert response.status_code == 202
    _, fast = _upload(api, alice, b"v2 fast", node_id=v1["id"], base_revision=v1["revision"])
    v2 = _ok(fast)
    blob_key = _upload_row(session_factory, slow).blob_key

    assert worker() == ScanOutcome.REFUSED

    refused = _complete(scanning_api, alice, slow)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "REVISION_CONFLICT"
    assert refused.json()["error"]["details"]["current_revision"] == v2["revision"]
    current = _ok(api.get(f"/api/v1/nodes/{v1['id']}", headers=_auth(alice)))
    assert current["revision"] == v2["revision"]
    assert not _blob_exists(storage_root, blob_key)


def test_completing_while_the_worker_scans_answers_at_once(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"data", parent_id=root_of(alice), name="d.txt")
    holder = session_factory()
    holder.execute(
        select(UploadSession).where(UploadSession.id == upload["id"]).with_for_update()
    )  # as the worker holds it while scanning
    try:
        answered: list[int] = []
        thread = threading.Thread(
            target=lambda: answered.append(_complete(scanning_api, alice, upload).status_code)
        )
        thread.start()
        thread.join(5)
        assert answered == [202]
    finally:
        holder.rollback()
        holder.close()


# --- Cleanup and quota keep scanning uploads (OQ-55) ----------------------------------------


def test_a_waiting_upload_keeps_its_blob_its_quota_and_doesnt_expire(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
) -> None:
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"x" * 600, parent_id=root_of(alice), name="w.txt")
    blob_key = _upload_row(session_factory, upload).blob_key
    storage = LocalFilesystemBackend(storage_root)
    much_later = datetime.now(UTC) + timedelta(days=30)

    upload_service.expire_uploads(session_factory, storage, much_later)
    collected = blob_service.collect(session_factory, storage, much_later, 24, force=True)

    assert collected.orphans == 0
    assert _blob_exists(storage_root, blob_key)
    assert _upload_row(session_factory, upload).status == UploadStatus.SCANNING
    # Its 600 bytes stay reserved: another 600 don't fit the 1024-byte test quota.
    refused = scanning_api.post(
        "/api/v1/uploads",
        json={
            "parent_id": root_of(alice),
            "name": "more.txt",
            "size_bytes": 600,
            "sha256": hashlib.sha256(b"y" * 600).hexdigest(),
        },
        headers=_auth(alice),
    )
    assert refused.json()["error"]["code"] == "QUOTA_EXCEEDED"


def test_a_cancelled_waiting_upload_is_not_scanned_and_its_blob_is_collected(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    worker: Callable[[], ScanOutcome],
) -> None:
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"draft", parent_id=root_of(alice), name="c.txt")
    blob_key = _upload_row(session_factory, upload).blob_key

    cancelled = scanning_api.delete(f"/api/v1/uploads/{upload['id']}", headers=_auth(alice))

    assert cancelled.status_code == 204
    assert worker() == ScanOutcome.NOTHING
    storage = LocalFilesystemBackend(storage_root)
    later = datetime.now(UTC) + timedelta(days=2)
    assert blob_service.collect(session_factory, storage, later, 24, force=True).orphans == 1
    assert not _blob_exists(storage_root, blob_key)


def _items(client: TestClient, user: Account, folder_id: str) -> list[Json]:
    response = client.get(f"/api/v1/nodes/{folder_id}/children", headers=_auth(user))
    assert response.status_code == 200, response.text
    items: list[Json] = response.json()["data"]
    return items


def _due_now(sessions: sessionmaker[Session], upload: Json) -> None:
    with sessions.begin() as session:
        session.execute(
            update(UploadSession)
            .where(UploadSession.id == upload["id"])
            .values(next_scan_at=datetime.now(UTC) - timedelta(seconds=1))
        )


# --- Fixes from audit 2026-11-07-0309 -------------------------------------------------------


def test_completing_while_another_complete_holds_the_upload_answers_at_once(
    api: TestClient, sign_in: SignIn, root_of: RootOf, session_factory: sessionmaker[Session]
) -> None:
    # NFR-REL-04: a retry after a timeout never waits for the first `complete`, and never gets
    # 404 when that one hands the upload to the scan worker meanwhile.
    alice = sign_in()
    upload = _start(api, alice, b"data", parent_id=root_of(alice), name="d.txt")
    _send(api, alice, upload, b"data")
    holder = session_factory()
    holder.execute(
        select(UploadSession).where(UploadSession.id == upload["id"]).with_for_update()
    )  # as a first `complete` holds it while it assembles
    try:
        response = _complete(api, alice, upload)
    finally:
        holder.rollback()
        holder.close()

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "pending"
    assert response.headers["Retry-After"] == "5"
    assert _ok(_complete(api, alice, upload))["name"] == "d.txt"  # then it completes


def test_a_retry_after_the_upload_was_handed_to_the_worker_gets_202_not_404(
    scanning_api: TestClient, sign_in: SignIn, root_of: RootOf
) -> None:
    alice = sign_in()
    upload, first = _upload(scanning_api, alice, b"data", parent_id=root_of(alice), name="d.txt")

    again = _complete(scanning_api, alice, upload)

    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json()["data"]["status"] == "scanning"


def test_cancelling_an_upload_being_scanned_answers_at_once(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"data", parent_id=root_of(alice), name="d.txt")
    holder = session_factory()
    holder.execute(
        select(UploadSession).where(UploadSession.id == upload["id"]).with_for_update()
    )  # the worker, scanning it
    try:
        response = scanning_api.delete(f"/api/v1/uploads/{upload['id']}", headers=_auth(alice))
    finally:
        holder.rollback()
        holder.close()

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "UPLOAD_SCANNING"
    assert _upload_row(session_factory, upload).status == UploadStatus.SCANNING


def test_the_worker_holds_only_the_upload_it_scans(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
) -> None:
    alice = sign_in()
    first, _ = _upload(scanning_api, alice, b"one", parent_id=root_of(alice), name="1.txt")
    second, _ = _upload(scanning_api, alice, b"two", parent_id=root_of(alice), name="2.txt")
    worker_session = session_factory()
    try:
        locked = upload_repository.lock_next_to_scan(worker_session, datetime.now(UTC))
        assert locked is not None
        other = uuid.UUID(str(second["id"] if str(locked.id) == first["id"] else first["id"]))
        with session_factory() as session:
            free = upload_repository.get_for_update(session, other, skip_locked=True)
            assert free is not None  # not held by the worker
    finally:
        worker_session.rollback()
        worker_session.close()


def test_gc_never_deletes_a_blob_that_becomes_a_version_while_it_looks(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    worker: Callable[[], ScanOutcome],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Blocker of audit 2026-11-07-0309: the worker publishes the upload between gc-blobs'
    # lookups. Whichever lookup runs first, the blob must count as in use.
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"precious", parent_id=root_of(alice), name="p.txt")
    blob_key = _upload_row(session_factory, upload).blob_key
    published: list[ScanOutcome] = []

    def after_first_lookup(original: Callable[..., set[str]]) -> Callable[..., set[str]]:
        def lookup(*args: object, **kwargs: object) -> set[str]:
            found = original(*args, **kwargs)
            if not published:
                published.append(worker())  # commits the version right now
            return found

        return lookup

    for module, name in (
        ("dropshare_server.repositories.uploads", "scanning_blob_keys"),
        ("dropshare_server.repositories.nodes", "known_blob_keys"),
    ):
        monkeypatch.setattr(
            f"{module}.{name}",
            after_first_lookup(getattr(__import__(module, fromlist=[name]), name)),
        )
    storage = LocalFilesystemBackend(storage_root)
    much_later = datetime.now(UTC) + timedelta(days=3)  # the blob is past the grace period

    collected = blob_service.collect(session_factory, storage, much_later, 24, force=True)

    assert published == [ScanOutcome.CLEAN]
    assert collected.orphans == 0
    assert _blob_exists(storage_root, blob_key)
    node = _ok(_complete(scanning_api, alice, upload))
    content = scanning_api.get(f"/api/v1/nodes/{node['id']}/content", headers=_auth(alice))
    assert content.content == b"precious"


def test_one_bad_upload_doesnt_stop_the_others(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    scanner: FakeScanner,
    db_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alice = sign_in()
    bad, _ = _upload(scanning_api, alice, b"bad one", parent_id=root_of(alice), name="b.txt")
    good, _ = _upload(scanning_api, alice, b"good one", parent_id=root_of(alice), name="g.txt")
    original = upload_service.record_version

    def flaky(*args: object, **kwargs: object) -> object:
        upload = args[3]
        assert isinstance(upload, UploadSession)
        if str(upload.id) == bad["id"]:
            raise RuntimeError("something unexpected")
        return original(*args, **kwargs)  # type: ignore[arg-type]  # passes the same arguments on

    monkeypatch.setattr(upload_service, "record_version", flaky)
    storage = LocalFilesystemBackend(storage_root)

    counts = scan_service.scan_due(session_factory, storage, scanner, db_settings)

    assert (counts.clean, counts.retrying) == (1, 1)
    assert _ok(_complete(scanning_api, alice, good))["name"] == "g.txt"
    row = _upload_row(session_factory, bad)
    assert (row.status, row.scan_attempts) == (UploadStatus.SCANNING, 1)
    assert row.next_scan_at is not None and row.next_scan_at > datetime.now(UTC)


def test_a_file_the_scanner_keeps_failing_on_is_refused_as_unscannable(
    db_settings: Settings,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    scanner: FakeScanner,
) -> None:
    settings = db_settings.model_copy(update={"scan_inline_max_bytes": 0, "scan_max_attempts": 2})
    scanner.fail_on(b"tricky")
    storage = LocalFilesystemBackend(storage_root)
    with TestClient(
        create_app(settings), raise_server_exceptions=False, headers={"X-Client-Version": "0.2.0"}
    ) as client:
        alice = sign_in()
        upload, _ = _upload(client, alice, b"a tricky file", parent_id=root_of(alice), name="t.zip")

        def run() -> ScanOutcome:
            return scan_service.scan_next(
                session_factory, storage, scanner, settings, datetime.now(UTC)
            )

        assert run() == ScanOutcome.RETRY
        _due_now(session_factory, upload)
        assert run() == ScanOutcome.INFECTED  # fail closed: never published unscanned
        assert _malware(_complete(client, alice, upload)) == scan_service.UNSCANNABLE


def test_a_missing_blob_ends_the_upload_so_the_client_sends_it_again(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    storage_root: Path,
    worker: Callable[[], ScanOutcome],
    audit_events: AuditEvents,
) -> None:
    alice = sign_in()
    upload, _ = _upload(scanning_api, alice, b"data", parent_id=root_of(alice), name="m.txt")
    blob_key = _upload_row(session_factory, upload).blob_key
    assert blob_key is not None
    storage_root.joinpath(*blob_key.split("/")).unlink()

    assert worker() == ScanOutcome.REFUSED

    response = _complete(scanning_api, alice, upload)
    assert (response.status_code, response.json()["error"]["code"]) == (404, "NOT_FOUND")
    [event] = audit_events(AuditAction.FILE_UPLOADED)
    assert (event.outcome, _details(event)["reason"]) == (AuditOutcome.FAILURE, "blob_missing")


@pytest.mark.parametrize("what", ["account_disabled", "device_revoked"])
def test_nothing_is_published_for_an_uploader_disabled_or_revoked_while_it_waited(
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    session_factory: sessionmaker[Session],
    worker: Callable[[], ScanOutcome],
    audit_events: AuditEvents,
    what: str,
) -> None:
    alice = sign_in()
    root = root_of(alice)
    upload, _ = _upload(scanning_api, alice, b"data", parent_id=root, name="x.txt")
    row = _upload_row(session_factory, upload)
    with session_factory.begin() as session:
        if what == "account_disabled":
            session.execute(
                update(User).where(User.id == row.user_id).values(status=UserStatus.DISABLED)
            )
        else:
            session.execute(
                update(Device)
                .where(Device.id == row.device_id)
                .values(revoked_at=datetime.now(UTC))
            )

    assert worker() == ScanOutcome.REFUSED

    with session_factory() as session:
        assert session.scalars(select(Node).where(Node.name == "x.txt")).all() == []
    assert _upload_row(session_factory, upload).status == UploadStatus.REJECTED
    [event] = audit_events(AuditAction.FILE_UPLOADED)
    assert (event.outcome, _details(event)["reason"]) == (AuditOutcome.DENIED, what)
    assert event.request_id is not None  # traceable to the `complete` call (FR-AUDIT-02)


def test_an_editor_made_viewer_while_it_waited_is_told_so(
    api: TestClient,
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    worker: Callable[[], ScanOutcome],
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    team = _ok(
        api.post(
            "/api/v1/nodes",
            json={"parent_id": root_of(alice), "name": "Team"},
            headers=_auth(alice),
        ),
        201,
    )
    bob_id = _ok(api.get("/api/v1/me", headers=_auth(bob)))["id"]
    member = _ok(
        api.post(
            f"/api/v1/nodes/{team['id']}/members",
            json={"user_id": bob_id, "role": "editor"},
            headers=_auth(alice),
        ),
        201,
    )
    upload, _ = _upload(scanning_api, bob, b"data", parent_id=team["id"], name="b.txt")
    _ok(api.patch(f"/api/v1/members/{member['id']}", json={"role": "viewer"}, headers=_auth(alice)))

    assert worker() == ScanOutcome.REFUSED

    response = _complete(scanning_api, bob, upload)
    assert (response.status_code, response.json()["error"]["code"]) == (403, "FORBIDDEN")


def test_a_name_taken_while_it_waited_is_answered_with_the_taken_item(
    api: TestClient,
    scanning_api: TestClient,
    sign_in: SignIn,
    root_of: RootOf,
    worker: Callable[[], ScanOutcome],
) -> None:
    alice = sign_in()
    root = root_of(alice)
    slow, _ = _upload(scanning_api, alice, b"slow", parent_id=root, name="n.txt")
    _, fast = _upload(api, alice, b"fast", parent_id=root, name="n.txt")
    taken = _ok(fast)

    assert worker() == ScanOutcome.REFUSED

    response = _complete(scanning_api, alice, slow)
    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["details"]["node_id"]) == ("NAME_CONFLICT", taken["id"])


def test_loop_is_only_for_the_scan_worker(cli: Callable[..., tuple[int, str, str]]) -> None:
    code, _, err = cli("jobs", "run", "gc-blobs", "--loop")

    assert code == 2
    assert "--loop" in err
