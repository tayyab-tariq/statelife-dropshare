"""Server files for the sync engine: namespaces, the change journal and long-poll, folders,
trash, chunked uploads and ranged, verified downloads (transfers.md, FR-SYNC-03, FR-XFER-01/02).
"""

import hashlib
import logging
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import (
    ApiError,
    ContentMismatchError,
    OfflineError,
    RemoteContentChangedError,
    SourceChangedError,
    TransferCancelledError,
)
from dropshare_client.store.records import StoredUpload
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.envelope import PAGE_LIMIT_MAX, DataResponse, ListResponse
from dropshare_shared.schemas.nodes import FolderCreateIn, NamespaceOut, NodeOut, NodeUpdateIn
from dropshare_shared.schemas.sync import ChangesResponse, WaitIn, WaitOut
from dropshare_shared.schemas.uploads import (
    CHUNK_SHA256_HEADER,
    UploadCreateIn,
    UploadOut,
    UploadStatus,
)

logger = logging.getLogger(__name__)

# The server holds a wait for ~55 s; give it time to answer before calling it lost.
_WAIT_TIMEOUT = httpx.Timeout(connect=10.0, read=75.0, write=30.0, pool=10.0)
_CHUNK_ATTEMPTS = 3
_READ_SIZE = 1024 * 1024
# Never hold more than this of a file in memory, whatever chunk size a server asks for.
_MAX_CHUNK_SIZE = 64 * 1024 * 1024
# Completing assembles and hashes the whole file on the server before it answers.
_COMPLETE_SECONDS_PER_BYTE = 1 / (20 * 1024 * 1024)
_COMPLETE_MIN_SECONDS = 60.0
# The upload can't continue: the target changed, the session is gone, or it was refused for
# good. Its reserved space is given back (it would otherwise count against quota for 24 h).
_UPLOAD_OVER = frozenset(
    {
        ErrorCode.REVISION_CONFLICT,
        ErrorCode.NAME_CONFLICT,
        ErrorCode.FILE_HASH_MISMATCH,
        ErrorCode.NOT_FOUND,
        ErrorCode.QUOTA_EXCEEDED,
        ErrorCode.FILE_TOO_LARGE,
    }
)


@dataclass(frozen=True)
class NewFile:
    parent_id: uuid.UUID
    name: str


@dataclass(frozen=True)
class NewVersion:
    node_id: uuid.UUID
    base_revision: int


type UploadTarget = NewFile | NewVersion
# Bytes of the file transferred so far; called on the transferring thread (FR-CLIENT-08).
type Progress = Callable[[int], None]


def no_progress(_: int) -> None:
    pass


@dataclass(frozen=True)
class SourceFile:
    """A local file as it was hashed; the upload stops if it changes from this."""

    path: Path
    sha256: str
    size: int
    mtime_ns: int


class UploadSessions(Protocol):
    """Where started uploads are remembered by target, so a retry resumes them instead of
    starting over. Called from transfer threads. The sync engine keeps them in its index, so
    they survive the app restarting; elsewhere they last until sign-out."""

    def upload_session(self, key: str) -> StoredUpload | None: ...

    def put_upload_session(self, key: str, stored: StoredUpload) -> None: ...

    def forget_upload_session(self, key: str) -> None: ...


class MemoryUploadSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, StoredUpload] = {}
        self._lock = threading.Lock()

    def upload_session(self, key: str) -> StoredUpload | None:
        with self._lock:
            return self._sessions.get(key)

    def put_upload_session(self, key: str, stored: StoredUpload) -> None:
        with self._lock:
            self._sessions[key] = stored

    def forget_upload_session(self, key: str) -> None:
        with self._lock:
            self._sessions.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


def upload_key(target: UploadTarget) -> str:
    """What an upload goes to: one session per target. The content it is for is stored with
    it, so an edit made since replaces the session instead of leaving it behind (S6)."""
    if isinstance(target, NewFile):
        return f"new:{target.parent_id}/{normalize_name(target.name)}"
    return f"version:{target.node_id}@{target.base_revision}"


class RemoteFiles:
    def __init__(self, api: ApiClient) -> None:
        self._api = api
        self._memory = MemoryUploadSessions()  # when the caller has no store of its own

    def forget_uploads(self) -> None:
        """Signed out: the next user never resumes this one's uploads (their session IDs
        would only be refused, and audited as denied)."""
        self._memory.clear()

    # --- metadata ------------------------------------------------------------------------

    def namespaces(self) -> list[NamespaceOut]:
        response = self._api.envelope("GET", "/api/v1/namespaces", DataResponse[list[NamespaceOut]])
        return response.data

    def changes(self, namespace_id: uuid.UUID, cursor: int) -> ChangesResponse:
        return self._api.envelope(
            "GET",
            f"/api/v1/namespaces/{namespace_id}/changes",
            ChangesResponse,
            params={"cursor": cursor},
        )

    def wait(self, cursors: Mapping[uuid.UUID, int]) -> list[uuid.UUID]:
        """Namespaces with news; empty when the server's wait ran out (FR-SYNC-03)."""
        body = WaitIn(cursors=dict(cursors)).model_dump(mode="json")
        return self._api.data(
            "POST", "/api/v1/changes/wait", WaitOut, json=body, timeout=_WAIT_TIMEOUT
        ).changed

    def children(
        self, folder_id: uuid.UUID, page: int, limit: int = PAGE_LIMIT_MAX
    ) -> ListResponse[NodeOut]:
        """One page of a folder's live items, folders first, then by name (FR-FILE-01)."""
        return self._api.envelope(
            "GET",
            f"/api/v1/nodes/{folder_id}/children",
            ListResponse[NodeOut],
            params={"page": page, "limit": limit},
        )

    def get_node(self, node_id: uuid.UUID) -> NodeOut:
        return self._api.data("GET", f"/api/v1/nodes/{node_id}", NodeOut)

    def create_folder(self, parent_id: uuid.UUID, name: str) -> NodeOut:
        body = FolderCreateIn(parent_id=parent_id, name=name).model_dump(mode="json")
        return self._api.data("POST", "/api/v1/nodes", NodeOut, json=body)

    def trash(
        self,
        node_id: uuid.UUID,
        revision: int,
        cursor: int | None = None,
        mount_cursor: int | None = None,
    ) -> None:
        """To the owner's trash. Repeating a delete that already worked succeeds again. With
        `cursor`, refused if anything inside changed after it (FR-CONF-03); `mount_cursor`
        does the same inside the user's own shared folder when deleting it (OQ-36)."""
        params: dict[str, str | int] = {"if_revision": revision}
        if cursor is not None:
            params["if_cursor"] = cursor
        if mount_cursor is not None:
            params["if_mount_cursor"] = mount_cursor
        self._api.no_content("DELETE", f"/api/v1/nodes/{node_id}", params=params)

    def move(
        self,
        node_id: uuid.UUID,
        revision: int,
        *,
        parent_id: uuid.UUID | None = None,
        name: str | None = None,
    ) -> NodeOut:
        """Rename and/or move within the namespace (FR-SYNC-10). Repeating a move that worked
        returns the node as it is now."""
        body = NodeUpdateIn(if_revision=revision, parent_id=parent_id, name=name)
        return self._api.data(
            "PATCH",
            f"/api/v1/nodes/{node_id}",
            NodeOut,
            json=body.model_dump(mode="json", exclude_none=True),
        )

    # --- uploads -------------------------------------------------------------------------

    def upload(
        self,
        source: SourceFile,
        target: UploadTarget,
        stop: threading.Event,
        progress: Progress = no_progress,
        *,
        sessions: UploadSessions | None = None,
    ) -> NodeOut:
        """Send the file in chunks and complete it. Retrying after any failure, also after the
        app restarted when `sessions` keeps them, picks the same session up and sends only the
        missing chunks; completing twice returns the same node, so a retry after a lost
        response never makes a duplicate (NFR-REL-04)."""
        if stop.is_set():  # paused before its turn came: nothing is sent
            raise TransferCancelledError()
        store = sessions or self._memory
        key = upload_key(target)
        self._check_unchanged(source)
        upload = self._resume(store, key, source)
        if upload is None:
            if stop.is_set():
                raise TransferCancelledError()
            upload = self._start(source, target)
            store.put_upload_session(key, StoredUpload(upload.id, source.sha256, source.size))
        try:
            if upload.status != UploadStatus.COMPLETED:
                self._send_chunks(source, upload, stop, progress)
                self._check_unchanged(source)
            node = self._complete(source, upload, stop, progress)
            if upload.status == UploadStatus.COMPLETED and not self._is_target(node, target):
                # Completed earlier (its answer lost) and the file moved since: that node
                # isn't this target any more. Upload this file as what it is now (B2).
                logger.info("Upload %s completed for a file that has moved; starting over", key)
                store.forget_upload_session(key)
                return self.upload(source, target, stop, progress, sessions=sessions)
        except ApiError as exc:
            if exc.code in _UPLOAD_OVER:
                self._abandon(store, key, upload)
            if exc.code == ErrorCode.FILE_HASH_MISMATCH:
                raise SourceChangedError(str(source.path)) from exc
            raise
        except SourceChangedError:
            self._abandon(store, key, upload)
            raise
        store.forget_upload_session(key)
        return node

    def _is_target(self, node: NodeOut, target: UploadTarget) -> bool:
        """Whether a replayed upload's node is still the target it was started for: the same
        file for a new version; the same name in the same folder for a new file (a folder
        that is a shared folder holds its files under that folder's root)."""
        if isinstance(target, NewVersion):
            return node.id == target.node_id
        if node.name is None or normalize_name(node.name) != normalize_name(target.name):
            return False
        if node.parent_id == target.parent_id:
            return True
        folder = self.get_node(target.parent_id)
        return folder.mount_namespace_id is not None and folder.mount_namespace_id == (
            node.namespace_id
        )

    def _complete(
        self, source: SourceFile, upload: UploadOut, stop: threading.Event, progress: Progress
    ) -> NodeOut:
        """Complete; if the server is missing a chunk after all, send what it lists, once."""
        timeout = httpx.Timeout(
            connect=10.0,
            read=max(_COMPLETE_MIN_SECONDS, source.size * _COMPLETE_SECONDS_PER_BYTE),
            write=60.0,
            pool=10.0,
        )
        path = f"/api/v1/uploads/{upload.id}/complete"
        try:
            return self._api.data("POST", path, NodeOut, timeout=timeout)
        except ApiError as exc:
            if exc.code != ErrorCode.UPLOAD_INCOMPLETE:
                raise
        current = self._api.data("GET", f"/api/v1/uploads/{upload.id}", UploadOut)
        self._send_chunks(source, current, stop, progress)
        self._check_unchanged(source)
        return self._api.data("POST", path, NodeOut, timeout=timeout)

    def _start(self, source: SourceFile, target: UploadTarget) -> UploadOut:
        if isinstance(target, NewFile):
            body = UploadCreateIn(
                parent_id=target.parent_id,
                name=target.name,
                size_bytes=source.size,
                sha256=source.sha256,
            )
        else:
            body = UploadCreateIn(
                node_id=target.node_id,
                base_revision=target.base_revision,
                size_bytes=source.size,
                sha256=source.sha256,
            )
        return self._api.data(
            "POST",
            "/api/v1/uploads",
            UploadOut,
            json=body.model_dump(mode="json", exclude_none=True),
        )

    def _resume(self, store: UploadSessions, key: str, source: SourceFile) -> UploadOut | None:
        """The session started for this target, if it is for this very content and the
        server still has it. One for other content (the file was edited since) is cancelled,
        so its reserved space is given back at once rather than in 24 hours."""
        stored = store.upload_session(key)
        if stored is None:
            return None
        if (stored.sha256, stored.size) != (source.sha256, source.size):
            self._cancel(stored.upload_id)
            store.forget_upload_session(key)
            return None
        try:
            upload = self._api.data("GET", f"/api/v1/uploads/{stored.upload_id}", UploadOut)
        except ApiError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            upload = None
        if upload is None or upload.status in (UploadStatus.CANCELLED, UploadStatus.EXPIRED):
            store.forget_upload_session(key)
            return None
        return upload

    def _send_chunks(
        self, source: SourceFile, upload: UploadOut, stop: threading.Event, progress: Progress
    ) -> None:
        if not 0 < upload.chunk_size <= _MAX_CHUNK_SIZE:
            raise ApiError(
                200, "INVALID_RESPONSE", "The server asked for chunks of an unexpected size."
            )
        missing = sorted(set(range(upload.chunk_count)) - set(upload.received_chunks))
        done = source.size - sum(_chunk_length(source.size, upload.chunk_size, i) for i in missing)
        progress(done)
        with source.path.open("rb") as handle:
            for index in missing:
                if stop.is_set():
                    raise TransferCancelledError()
                handle.seek(index * upload.chunk_size)
                data = handle.read(upload.chunk_size)
                digest = hashlib.sha256(data).hexdigest()
                for attempt in range(1, _CHUNK_ATTEMPTS + 1):
                    try:
                        self._api.no_content(
                            "PUT",
                            f"/api/v1/uploads/{upload.id}/chunks/{index}",
                            content=data,
                            headers={CHUNK_SHA256_HEADER: digest},
                        )
                        break
                    except ApiError as exc:  # corrupted on the way: send the same bytes again
                        if exc.code != ErrorCode.CHUNK_HASH_MISMATCH or attempt == _CHUNK_ATTEMPTS:
                            raise
                done += len(data)
                progress(done)

    def _abandon(self, store: UploadSessions, key: str, upload: UploadOut) -> None:
        """Forget a session that can't complete, and free its space on the server (a
        completed one has none reserved, and can't be cancelled)."""
        store.forget_upload_session(key)
        if upload.status != UploadStatus.COMPLETED:
            self._cancel(upload.id)

    def _cancel(self, upload_id: uuid.UUID) -> None:
        try:
            self._api.no_content("DELETE", f"/api/v1/uploads/{upload_id}")
        except (ApiError, OfflineError):
            logger.info("Couldn't cancel upload %s; it expires on its own", upload_id)

    @staticmethod
    def _check_unchanged(source: SourceFile) -> None:
        info = source.path.stat()
        if (info.st_size, info.st_mtime_ns) != (source.size, source.mtime_ns):
            raise SourceChangedError(str(source.path))

    # --- downloads -----------------------------------------------------------------------

    def download(
        self,
        node_id: uuid.UUID,
        sha256: str,
        size: int,
        part: Path,
        stop: threading.Event,
        progress: Progress = no_progress,
    ) -> None:
        """Fetch the content into `part`, resuming from what is already there, and verify it
        (FR-SYNC-06, FR-XFER-02). A mismatch starts once more from scratch, then gives up."""
        if stop.is_set():  # paused before its turn came: nothing is fetched
            raise TransferCancelledError()
        for attempt in (1, 2):
            digest = self._fetch(node_id, sha256, size, part, stop, progress)
            if digest == sha256 and part.stat().st_size == size:
                return
            logger.warning("Download of %s didn't verify (attempt %d)", node_id, attempt)
            part.unlink(missing_ok=True)
        raise ContentMismatchError(str(node_id))

    def _fetch(
        self,
        node_id: uuid.UUID,
        sha256: str,
        size: int,
        part: Path,
        stop: threading.Event,
        progress: Progress,
    ) -> str:
        """The SHA-256 of `part` once the rest of the file is appended."""
        offset = part.stat().st_size if part.exists() else 0
        if offset > size:
            part.unlink()
            offset = 0
        digest = hashlib.sha256()
        if offset:
            with part.open("rb") as existing:
                while chunk := existing.read(_READ_SIZE):
                    digest.update(chunk)
        if offset >= size:
            part.touch()
            return digest.hexdigest()
        headers = {"Range": f"bytes={offset}-"} if offset else None
        try:
            with self._api.stream("GET", f"/api/v1/nodes/{node_id}/content", headers=headers) as (
                response
            ):
                etag = response.headers.get("ETag", "").strip('"')
                if etag and etag != sha256:
                    raise RemoteContentChangedError(str(node_id))
                if offset and response.status_code != 206:  # whole file sent: start over
                    offset = 0
                    digest = hashlib.sha256()
                with part.open("ab" if offset else "wb") as out:
                    written = offset
                    progress(written)
                    for chunk in response.iter_bytes(_READ_SIZE):
                        if stop.is_set():
                            raise TransferCancelledError()
                        written += len(chunk)
                        if written > size:  # more than the file holds: it can't verify
                            return ""
                        out.write(chunk)
                        digest.update(chunk)
                        progress(written)
                    out.flush()
                    os.fsync(out.fileno())
        except httpx.TransportError as exc:
            raise OfflineError("The download was interrupted") from exc
        except ApiError as exc:
            if exc.code != ErrorCode.RANGE_NOT_SATISFIABLE:
                raise
            part.unlink(missing_ok=True)  # the file got shorter: the next attempt starts over
        return digest.hexdigest()


def file_sha256(path: Path, stop: threading.Event) -> str:
    """Hex SHA-256 of a local file; TransferCancelledError if `stop` is set meanwhile."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(_READ_SIZE):
            if stop.is_set():
                raise TransferCancelledError()
            digest.update(block)
    return digest.hexdigest()


def _chunk_length(size: int, chunk_size: int, index: int) -> int:
    return max(0, min(chunk_size, size - index * chunk_size))
