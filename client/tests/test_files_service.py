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
)
from dropshare_client.services.files import NewFile, NewVersion, RemoteFiles, SourceFile
from dropshare_client.services.tokens import TokenStore
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
                return httpx.Response(200, json={"data": self._upload(upload_id)})
            case "PUT", ["uploads", upload_id, "chunks", index]:
                data = request.content
                if hashlib.sha256(data).hexdigest() != request.headers["X-Chunk-SHA256"]:
                    return _error(400, "CHUNK_HASH_MISMATCH")
                self.chunks[upload_id][int(index)] = data
                return httpx.Response(204)
            case "POST", ["uploads", upload_id, "complete"]:
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
        return _error(404, "NOT_FOUND")

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
