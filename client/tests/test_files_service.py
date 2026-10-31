"""The file service over HTTP: chunked uploads that resume and complete idempotently
(FR-XFER-01, NFR-REL-04), and ranged downloads verified before use (FR-XFER-02, NFR-REL-02)."""

import hashlib
import json
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import (
    ApiError,
    ContentMismatchError,
    OfflineError,
    RemoteContentChangedError,
    SourceChangedError,
    TransferCancelledError,
)
from dropshare_client.services.files import (
    NewFile,
    NewVersion,
    RemoteFiles,
    SourceFile,
    upload_key,
)
from dropshare_client.services.tokens import TokenStore
from dropshare_client.store.index import Index
from dropshare_shared.schemas.auth import TokenPairOut

CHUNK = 4
NODE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000f1")
PARENT_ID = uuid.UUID("00000000-0000-0000-0000-0000000000d1")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


class _Credentials:
    def __init__(self) -> None:
        self.secret: str | None = None

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = None


def _node(content: bytes, revision: int = 1) -> dict[str, object]:
    return {
        "id": str(NODE_ID),
        "namespace_id": str(uuid.uuid4()),
        "parent_id": str(PARENT_ID),
        "name": "doc.txt",
        "kind": "file",
        "size_bytes": len(content),
        "content_hash": hashlib.sha256(content).hexdigest(),
        "revision": revision,
        "created_at": NOW.isoformat(),
        "modified_at": NOW.isoformat(),
        "modified_by_user_id": str(uuid.uuid4()),
    }


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code}})


class FakeTransfers:
    """Uploads and downloads as the server implements them (services/uploads.py, nodes.py)."""

    def __init__(self, content: bytes = b"") -> None:
        self.content = content  # what GET /content serves
        self.uploads: dict[str, dict[str, object]] = {}
        self.chunks: dict[str, dict[int, bytes]] = {}
        self.requests: list[str] = []
        self.drop_after: Callable[[httpx.Request], bool] = lambda _: False
        self.corrupt = 0  # how many more responses arrive damaged
        self.lose_chunk: int | None = None  # dropped once before completing
        self.complete_error: str | None = None  # answer complete with this code once
        self.chunk_size = CHUNK
        self.extra_bytes = b""  # sent after the content (a misbehaving server)
        self.params: list[str] = []
        self.bodies: list[object] = []
        self.moved: set[str] = set()  # completed uploads whose file was renamed since

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        self.params.append(str(request.url.params))
        response = self._route(request)
        if self.drop_after(request):
            raise httpx.ReadTimeout("answer lost", request=request)
        return response

    def _route(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")[3:]  # after /api/v1
        match request.method, parts:
            case "POST", ["uploads"]:
                body = json.loads(request.content)
                upload_id = str(uuid.uuid4())
                self.uploads[upload_id] = {**body, "status": "pending"}
                self.chunks[upload_id] = {}
                return httpx.Response(201, json={"data": self._upload(upload_id)})
            case "GET", ["uploads", upload_id]:
                if upload_id not in self.uploads:  # cleaned up, or someone else's
                    return _error(404, "NOT_FOUND")
                return httpx.Response(200, json={"data": self._upload(upload_id)})
            case "PUT", ["uploads", upload_id, "chunks", index]:
                data = request.content
                if hashlib.sha256(data).hexdigest() != request.headers["X-Chunk-SHA256"]:
                    return _error(400, "CHUNK_HASH_MISMATCH")
                self.chunks[upload_id][int(index)] = data
                return httpx.Response(204)
            case "POST", ["uploads", upload_id, "complete"]:
                if self.uploads[upload_id]["status"] == "completed" and upload_id in self.moved:
                    moved = {**_node(self.content_of(upload_id), 2), "name": "renamed.txt"}
                    return httpx.Response(200, json={"data": moved})
                if self.complete_error is not None:
                    code, self.complete_error = self.complete_error, None
                    return _error(409, code)
                if self.lose_chunk is not None:
                    self.chunks[upload_id].pop(self.lose_chunk, None)
                    self.lose_chunk = None
                    return _error(409, "UPLOAD_INCOMPLETE")
                upload = self.uploads[upload_id]
                assembled = b"".join(v for _, v in sorted(self.chunks[upload_id].items()))
                if hashlib.sha256(assembled).hexdigest() != upload["sha256"]:
                    upload["status"] = "cancelled"
                    return _error(400, "FILE_HASH_MISMATCH")
                upload["status"] = "completed"
                return httpx.Response(200, json={"data": _node(assembled, 2)})
            case "DELETE", ["uploads", upload_id]:
                self.uploads[upload_id]["status"] = "cancelled"
                return httpx.Response(204)
            case "GET", ["nodes", _, "content"]:
                return self._content(request)
            case "PATCH", ["nodes", _]:
                self.bodies.append(json.loads(request.content))
                return httpx.Response(200, json={"data": _node(b"moved", 2)})
        return _error(404, "NOT_FOUND")

    def content_of(self, upload_id: str) -> bytes:
        return b"".join(v for _, v in sorted(self.chunks[upload_id].items()))

    def _upload(self, upload_id: str) -> dict[str, object]:
        upload = self.uploads[upload_id]
        size = int(str(upload["size_bytes"]))
        return {
            "id": upload_id,
            "status": upload["status"],
            "size_bytes": size,
            "chunk_size": self.chunk_size,
            "chunk_count": -(-size // self.chunk_size),
            "received_chunks": sorted(self.chunks[upload_id]),
            "expires_at": (NOW + timedelta(hours=24)).isoformat(),
            "node_id": None,
        }

    def _content(self, request: httpx.Request) -> httpx.Response:
        content = self.content
        if self.corrupt:
            self.corrupt -= 1
            content = b"X" + content[1:]
        headers = {"ETag": f'"{hashlib.sha256(self.content).hexdigest()}"'}
        header = request.headers.get("Range")
        if header:
            start = int(header.removeprefix("bytes=").rstrip("-"))
            if start >= len(content):
                return _error(416, "RANGE_NOT_SATISFIABLE")
            headers["Content-Range"] = f"bytes {start}-{len(content) - 1}/{len(content)}"
            return httpx.Response(206, content=content[start:], headers=headers)
        return httpx.Response(200, content=content + self.extra_bytes, headers=headers)


@pytest.fixture
def fake() -> FakeTransfers:
    return FakeTransfers()


@pytest.fixture
def files(fake: FakeTransfers) -> RemoteFiles:
    tokens = TokenStore(_Credentials())
    tokens.store(
        TokenPairOut(
            access_token="access",
            refresh_token="refresh",
            expires_in=900,
            must_change_password=False,
        )
    )
    api = ApiClient(
        "https://dropshare.example.com",
        tokens,
        client_version="1.0.0",
        user_agent="test",
        transport=httpx.MockTransport(fake.handle),
    )
    return RemoteFiles(api)


def _source(tmp_path: Path, content: bytes) -> SourceFile:
    path = tmp_path / "doc.txt"
    path.write_bytes(content)
    info = path.stat()
    return SourceFile(path, hashlib.sha256(content).hexdigest(), info.st_size, info.st_mtime_ns)


def test_upload_sends_hashed_chunks_and_completes(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"0123456789")

    node = files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())

    assert node.content_hash == source.sha256
    (upload_id,) = fake.chunks
    assert fake.chunks[upload_id] == {0: b"0123", 1: b"4567", 2: b"89"}
    assert fake.uploads[upload_id]["name"] == "doc.txt"


def test_a_retry_sends_only_the_missing_chunks(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"0123456789")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(source, NewVersion(NODE_ID, 1), threading.Event())

    fake.drop_after = lambda _: False
    fake.requests.clear()
    files.upload(source, NewVersion(NODE_ID, 1), threading.Event())

    puts = [r for r in fake.requests if r.startswith("PUT")]
    assert len(puts) == 1 and puts[0].endswith("/chunks/2")
    assert not any(r == "POST /api/v1/uploads" for r in fake.requests)


def test_an_upload_resumes_after_the_app_restarts(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    """With an index as the store, the session is kept there and nowhere in memory: reopened
    (the app started again), it sends only what the server is missing (FR-XFER-01)."""
    source = _source(tmp_path, b"0123456789")
    index = Index(tmp_path / "index.db")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=index)
    index.close()

    fake.drop_after = lambda _: False
    fake.requests.clear()
    restarted = Index(tmp_path / "index.db")
    files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=restarted)

    puts = [r for r in fake.requests if r.startswith("PUT")]
    assert len(puts) == 1 and puts[0].endswith("/chunks/2")
    assert "POST /api/v1/uploads" not in fake.requests
    assert restarted.upload_session(upload_key(NewVersion(NODE_ID, 1))) is None


def test_an_expired_session_starts_a_new_upload(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"0123456789")
    index = Index(tmp_path / "index.db")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=index)
    fake.drop_after = lambda _: False
    for upload in fake.uploads.values():
        upload["status"] = "expired"  # the app was away for more than a day

    files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=index)

    assert len(fake.uploads) == 2


def test_nothing_is_sent_when_stopped_before_its_turn(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    """Paused while queued (B1): not even the session is started, also for an empty file."""
    stop = threading.Event()
    stop.set()

    with pytest.raises(TransferCancelledError):
        files.upload(_source(tmp_path, b""), NewFile(PARENT_ID, "empty.txt"), stop)
    with pytest.raises(TransferCancelledError):
        files.download(NODE_ID, hashlib.sha256(b"x").hexdigest(), 1, tmp_path / "x.part", stop)

    assert fake.requests == []


def test_a_completed_upload_whose_file_moved_since_is_uploaded_again(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    """B2: the answer to completing was lost, then the file was renamed on the server. The
    same content saved at the old name again is a new file, not the renamed one."""
    source = _source(tmp_path, b"0123456789")
    index = Index(tmp_path / "index.db")
    fake.drop_after = lambda r: r.url.path.endswith("/complete")
    with pytest.raises(OfflineError):
        files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event(), sessions=index)
    fake.drop_after = lambda _: False
    fake.moved.update(fake.uploads)

    node = files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event(), sessions=index)

    assert node.name == "doc.txt"
    assert fake.requests.count("POST /api/v1/uploads") == 2  # a new upload, not the replay
    index.close()


def test_a_session_for_content_edited_since_is_cancelled_not_left_reserving_space(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    """S6: one session per target; the old one is cancelled at once, giving its space back."""
    index = Index(tmp_path / "index.db")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(
            _source(tmp_path, b"0123456789"),
            NewVersion(NODE_ID, 1),
            threading.Event(),
            sessions=index,
        )
    fake.drop_after = lambda _: False
    first = next(iter(fake.uploads))

    files.upload(
        _source(tmp_path, b"edited since"),
        NewVersion(NODE_ID, 1),
        threading.Event(),
        sessions=index,
    )

    assert fake.uploads[first]["status"] == "cancelled"
    assert f"DELETE /api/v1/uploads/{first}" in fake.requests
    index.close()


@pytest.mark.parametrize("status", ["cancelled", "expired", "gone"])
def test_a_session_the_server_ended_starts_a_new_upload(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path, status: str
) -> None:
    source = _source(tmp_path, b"0123456789")
    index = Index(tmp_path / "index.db")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=index)
    fake.drop_after = lambda _: False
    (first,) = fake.uploads
    if status == "gone":  # expired and cleaned up, or someone else's: the server says 404
        del fake.uploads[first]
    else:
        fake.uploads[first]["status"] = status

    files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=index)

    assert len(fake.uploads) == (1 if status == "gone" else 2)
    assert fake.requests.count("POST /api/v1/uploads") == 2
    index.close()


def test_a_completed_upload_resumed_after_a_restart_is_not_sent_again(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"0123456789")
    index = Index(tmp_path / "index.db")
    fake.drop_after = lambda r: r.url.path.endswith("/complete")
    with pytest.raises(OfflineError):
        files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=index)
    index.close()
    fake.drop_after = lambda _: False
    fake.requests.clear()

    restarted = Index(tmp_path / "index.db")
    node = files.upload(source, NewVersion(NODE_ID, 1), threading.Event(), sessions=restarted)

    assert node.content_hash == source.sha256
    assert not any(r.startswith(("PUT", "POST /api/v1/uploads ")) for r in fake.requests)
    assert "POST /api/v1/uploads" not in fake.requests
    restarted.close()


def test_signing_out_forgets_the_uploads_kept_in_memory(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    """S13: the next user never resumes (and is never refused for) this one's session."""
    source = _source(tmp_path, b"0123456789")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(source, NewVersion(NODE_ID, 1), threading.Event())
    fake.drop_after = lambda _: False

    files.forget_uploads()
    fake.requests.clear()
    files.upload(source, NewVersion(NODE_ID, 1), threading.Event())

    assert fake.requests[0] == "POST /api/v1/uploads"  # no attempt to resume the old one


def test_a_lost_completion_is_completed_again_not_uploaded_again(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"abc")
    fake.drop_after = lambda r: r.url.path.endswith("/complete")
    with pytest.raises(OfflineError):
        files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())

    fake.drop_after = lambda _: False
    node = files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())

    assert node.content_hash == source.sha256
    assert len(fake.uploads) == 1


def test_a_file_changed_before_completing_starts_over(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"0123456789")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/0")
    with pytest.raises(OfflineError):
        files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())
    source.path.write_bytes(b"different!")

    with pytest.raises(SourceChangedError):
        files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())


def test_download_resumes_from_the_partial_file(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.content = b"0123456789"
    part = tmp_path / "x.part"
    part.write_bytes(b"0123")

    files.download(NODE_ID, hashlib.sha256(fake.content).hexdigest(), 10, part, threading.Event())

    assert part.read_bytes() == b"0123456789"
    (request,) = [r for r in fake.requests if r.endswith("/content")]
    assert request.startswith("GET")


def test_a_corrupt_download_is_fetched_again(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.content = b"0123456789"
    fake.corrupt = 1
    part = tmp_path / "x.part"

    files.download(NODE_ID, hashlib.sha256(fake.content).hexdigest(), 10, part, threading.Event())

    assert part.read_bytes() == b"0123456789"


def test_a_download_that_never_verifies_fails_and_leaves_nothing(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.content = b"0123456789"
    fake.corrupt = 2
    part = tmp_path / "x.part"

    expected = hashlib.sha256(fake.content).hexdigest()

    with pytest.raises(ContentMismatchError):
        files.download(NODE_ID, expected, 10, part, threading.Event())
    assert not part.exists()


def test_a_newer_server_version_is_reported(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.content = b"new content"
    with pytest.raises(RemoteContentChangedError):
        files.download(
            NODE_ID, hashlib.sha256(b"old").hexdigest(), 3, tmp_path / "x.part", threading.Event()
        )


def test_an_empty_file_downloads_without_a_request(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    part = tmp_path / "x.part"

    files.download(NODE_ID, hashlib.sha256(b"").hexdigest(), 0, part, threading.Event())

    assert part.read_bytes() == b""
    assert fake.requests == []


def test_server_errors_keep_their_code(files: RemoteFiles, tmp_path: Path) -> None:
    with pytest.raises(ApiError) as caught:
        files.get_node(NODE_ID)
    assert caught.value.code == "NOT_FOUND"


def test_chunks_the_server_lost_are_sent_again(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    """UPLOAD_INCOMPLETE is passing trouble: resend what's missing and complete (S3)."""
    source = _source(tmp_path, b"0123456789")
    fake.lose_chunk = 1

    node = files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())

    assert node.content_hash == source.sha256
    puts = [r for r in fake.requests if r.startswith("PUT")]
    assert puts[-1].endswith("/chunks/1")


@pytest.mark.parametrize("code", ["QUOTA_EXCEEDED", "FILE_TOO_LARGE"])
def test_a_refused_upload_gives_its_space_back(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path, code: str
) -> None:
    source = _source(tmp_path, b"abc")
    fake.complete_error = code

    with pytest.raises(ApiError):
        files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())

    (upload,) = fake.uploads.values()
    assert upload["status"] == "cancelled"


def test_an_oversized_chunk_request_is_refused(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.chunk_size = 1024 * 1024 * 1024  # 1 GiB in memory per chunk: no

    with pytest.raises(ApiError):
        files.upload(_source(tmp_path, b"abc"), NewFile(PARENT_ID, "a"), threading.Event())


def test_a_download_longer_than_its_size_is_rejected(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.content = b"0123456789"
    fake.extra_bytes = b"x" * 1000
    part = tmp_path / "x.part"

    with pytest.raises(ContentMismatchError):
        files.download(
            NODE_ID, hashlib.sha256(fake.content).hexdigest(), 10, part, threading.Event()
        )
    assert not part.exists()


def test_trash_sends_the_cursor(files: RemoteFiles, fake: FakeTransfers) -> None:
    with pytest.raises(ApiError):  # the fake has no nodes; only the request matters
        files.trash(NODE_ID, 3, 41)

    assert fake.params == ["if_revision=3&if_cursor=41"]


def test_move_sends_only_what_changes_with_the_base_revision(
    files: RemoteFiles, fake: FakeTransfers
) -> None:
    files.move(NODE_ID, 3, name="new.txt")
    files.move(NODE_ID, 4, parent_id=PARENT_ID)

    assert fake.requests == [f"PATCH /api/v1/nodes/{NODE_ID}"] * 2
    assert fake.bodies == [
        {"if_revision": 3, "name": "new.txt"},
        {"if_revision": 4, "parent_id": str(PARENT_ID)},
    ]


def test_upload_progress_rises_to_the_file_size_and_counts_what_was_already_sent(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    source = _source(tmp_path, b"0123456789")
    fake.drop_after = lambda r: r.url.path.endswith("/chunks/1")
    with pytest.raises(OfflineError):
        files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event())
    fake.drop_after = lambda _: False
    seen: list[int] = []

    files.upload(source, NewFile(PARENT_ID, "doc.txt"), threading.Event(), seen.append)

    assert seen == [8, 10]  # chunks 0 and 1 arrived before the answer was lost


def test_download_progress_starts_from_the_partial_file_and_ends_at_the_size(
    files: RemoteFiles, fake: FakeTransfers, tmp_path: Path
) -> None:
    fake.content = b"0123456789"
    part = tmp_path / "x.part"
    part.write_bytes(b"0123")
    seen: list[int] = []

    files.download(
        NODE_ID,
        hashlib.sha256(fake.content).hexdigest(),
        10,
        part,
        threading.Event(),
        seen.append,
    )

    assert seen[0] == 4 and seen[-1] == 10
    assert seen == sorted(seen)
