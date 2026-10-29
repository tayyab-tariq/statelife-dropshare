"""Fakes for the sync engine tests: a file server that behaves like the real one where the
engine depends on it (revisions, name conflicts, trash, the journal and its cursor), a
Recycle Bin, and a Device that runs SyncCore cycles on a temporary sync folder."""

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Concatenate

from dropshare_client.platform.base import FileId, FileIdentity, RecycleBin
from dropshare_client.services.errors import (
    ApiError,
    OfflineError,
    RemoteContentChangedError,
    SourceChangedError,
)
from dropshare_client.services.files import (
    NewFile,
    Progress,
    SourceFile,
    UploadTarget,
    no_progress,
)
from dropshare_client.store.index import Index
from dropshare_client.sync.core import CycleResult, SyncCore
from dropshare_client.sync.executor import no_crash
from dropshare_client.sync.paths import key_of, outermost
from dropshare_client.sync.server import FileServer
from dropshare_client.sync.transfers import TransferEvent
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import InvalidNameError, normalize_name, validate_name
from dropshare_shared.schemas.nodes import (
    AccessRole,
    NamespaceKind,
    NamespaceOut,
    NodeKind,
    NodeOut,
)
from dropshare_shared.schemas.sync import ChangeOut, ChangesMeta, ChangesResponse, ChangeType

USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000a11c")
OTHER_DEVICE = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
DISPLAY_NAME = "Alice Example"
type Tree = dict[str, bytes | None]  # path -> content, None for a folder
type Listing = list[tuple[str, bytes | None]]

_DASH = "\N{EN DASH}"
_CONFLICT = re.compile(rf" \(conflicted copy {_DASH} [^)]*{_DASH} \d{{4}}-\d\d-\d\d \d\d-\d\d\)")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plain(tree: Mapping[str, bytes | None]) -> Tree:
    """Conflict-copy names without their time stamp, so trees can be compared."""
    return {_CONFLICT.sub(" (conflict)", path): content for path, content in tree.items()}


class Crash(BaseException):
    """Raised at a crash point; nothing in the engine catches BaseException."""


@dataclass
class FakeNode:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str | None
    kind: NodeKind
    content: bytes = b""
    revision: int = 1
    trashed: bool = False
    trash_root: uuid.UUID | None = None
    modified_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def out(self, namespace_id: uuid.UUID) -> NodeOut:
        return NodeOut(
            id=self.id,
            namespace_id=namespace_id,
            parent_id=self.parent_id,
            name=self.name,
            kind=self.kind,
            size_bytes=len(self.content) if self.kind == NodeKind.FILE else 0,
            content_hash=sha(self.content) if self.kind == NodeKind.FILE else None,
            revision=self.revision,
            created_at=self.modified_at,
            modified_at=self.modified_at,
            modified_by_user_id=USER_ID,
        )


@dataclass
class Fault:
    method: str
    error: BaseException
    after: bool  # True: the operation happens, then the answer is lost
    times: int = 1
    skip: int = 0  # let this many matching calls through first


def _locked[**P, R](
    method: Callable[Concatenate["FakeFileServer", P], R],
) -> Callable[Concatenate["FakeFileServer", P], R]:
    """One caller at a time: the engine's threads and the test thread share the fake."""

    def wrapper(self: "FakeFileServer", /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper


class FakeFileServer:
    def __init__(self, page_size: int = 500, wait_seconds: float = 0.0) -> None:
        self.namespace_id = uuid.uuid4()
        self.root = FakeNode(uuid.uuid4(), None, None, NodeKind.FOLDER)
        self.nodes: dict[uuid.UUID, FakeNode] = {self.root.id: self.root}
        self.journal: list[ChangeOut] = []
        self.page_size = page_size
        self.faults: list[Fault] = []
        self.calls: list[str] = []
        self.offline = False
        self.wait_seconds = wait_seconds  # how long wait() holds when there is no news
        self._lock = threading.RLock()

    # --- test helpers (another device's actions) ------------------------------------------

    def fail(
        self,
        method: str,
        error: BaseException,
        *,
        after: bool = False,
        times: int = 1,
        skip: int = 0,
    ) -> None:
        self.faults.append(Fault(method, error, after, times, skip))

    @_locked
    def put(self, path: str, content: bytes | None = None) -> FakeNode:
        """Create or overwrite a file (content) or create a folder (None) at `path`."""
        parent = self._lookup(path.rpartition("/")[0])
        name = path.rpartition("/")[2]
        existing = self._child(parent.id, name)
        if existing is not None:
            assert content is not None and existing.kind == NodeKind.FILE
            existing.content = content
            existing.revision += 1
            existing.modified_at = datetime.now(UTC)
            self._record(ChangeType.MODIFIED, existing)
            return existing
        kind = NodeKind.FOLDER if content is None else NodeKind.FILE
        node = FakeNode(uuid.uuid4(), parent.id, name, kind, content or b"")
        self.nodes[node.id] = node
        self._record(ChangeType.CREATED, node)
        return node

    @_locked
    def remove(self, path: str) -> None:
        node = self._lookup(path)
        self._trash(node)

    @_locked
    def relocate(self, path: str, new_path: str) -> FakeNode:
        """Another device renames and/or moves `path` to `new_path`."""
        node = self._lookup(path)
        parent = self._lookup(new_path.rpartition("/")[0])
        name = new_path.rpartition("/")[2]
        existing = self._child(parent.id, name)
        assert existing is None or existing.id == node.id, f"{new_path} is taken"
        node.parent_id, node.name = parent.id, name
        node.revision += 1
        self._record(ChangeType.MOVED, node)
        return node

    @_locked
    def tree(self) -> Tree:
        result: Tree = {}
        for node in self.nodes.values():
            if node.trashed or node.parent_id is None:
                continue
            path = self.path_of(node)
            if path is not None:
                result[path] = node.content if node.kind == NodeKind.FILE else None
        return result

    @_locked
    def trashed(self) -> Listing:
        """Trash roots by name, with their content (what a user could restore)."""
        return sorted(
            (node.name or "", node.content if node.kind == NodeKind.FILE else None)
            for node in self.nodes.values()
            if node.trashed and node.trash_root == node.id
        )

    def path_of(self, node: FakeNode) -> str | None:
        names = []
        current: FakeNode | None = node
        while current is not None and current.parent_id is not None:
            names.append(current.name or "")
            current = self.nodes.get(current.parent_id)
        return "/".join(reversed(names))

    # --- FileServer ------------------------------------------------------------------------

    @_locked
    def namespaces(self) -> list[NamespaceOut]:
        self._call("namespaces")
        return [
            NamespaceOut(
                id=self.namespace_id,
                kind=NamespaceKind.PERSONAL,
                root_node_id=self.root.id,
                role=AccessRole.OWNER,
            )
        ]

    @_locked
    def changes(self, namespace_id: uuid.UUID, cursor: int) -> ChangesResponse:
        self._call("changes")
        later = [c for c in self.journal if c.cursor > cursor]
        page = later[: self.page_size]
        last = page[-1].cursor if page else cursor
        result = ChangesResponse(
            data=page, meta=ChangesMeta(cursor=last, has_more=len(later) > len(page))
        )
        self._after("changes")
        return result

    def wait(self, cursors: Mapping[uuid.UUID, int]) -> list[uuid.UUID]:
        deadline = time.monotonic() + self.wait_seconds
        while True:
            with self._lock:
                self._call("wait")
                latest = self.journal[-1].cursor if self.journal else 0
                if latest > cursors.get(self.namespace_id, 0):
                    return [self.namespace_id]
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.01)

    @_locked
    def get_node(self, node_id: uuid.UUID) -> NodeOut:
        self._call("get_node")
        node = self.nodes.get(node_id)
        if node is None or node.trashed:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Not found")
        return node.out(self.namespace_id)

    @_locked
    def create_folder(self, parent_id: uuid.UUID, name: str) -> NodeOut:
        self._call("create_folder")
        _check_name(name)
        parent = self._live_folder(parent_id)
        self._check_free(parent, name)
        node = FakeNode(uuid.uuid4(), parent.id, name, NodeKind.FOLDER)
        self.nodes[node.id] = node
        self._record(ChangeType.CREATED, node)
        self._after("create_folder")
        return node.out(self.namespace_id)

    @_locked
    def trash(
        self,
        node_id: uuid.UUID,
        revision: int,
        cursor: int | None = None,
        mount_cursor: int | None = None,
    ) -> None:
        self._call("trash")
        node = self.nodes.get(node_id)
        if node is None:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Not found")
        if node.trashed:
            if node.trash_root == node.id and node.revision == revision + 1:
                self._after("trash")
                return
            raise ApiError(404, ErrorCode.NOT_FOUND, "Not found")
        if node.revision != revision:
            raise _revision_conflict(node.revision)
        if cursor is not None:  # as the server: refused if anything inside changed since
            inside = {n.id for n in self._live_below(node)}
            if any(c.cursor > cursor and c.node_id in inside for c in self.journal):
                raise _revision_conflict(node.revision)
        self._trash(node)
        self._after("trash")

    @_locked
    def move(
        self,
        node_id: uuid.UUID,
        revision: int,
        *,
        parent_id: uuid.UUID | None = None,
        name: str | None = None,
    ) -> NodeOut:
        """As the server's PATCH /nodes/{id}."""
        self._call("move")
        node = self.nodes.get(node_id)
        if node is None or node.trashed:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Not found")
        if node.parent_id is None:
            raise _invalid("The root folder can't be moved.", "root_not_movable")
        if name is not None:
            _check_name(name)
        parent = self._live_node(parent_id or node.parent_id)
        if parent.kind != NodeKind.FOLDER:
            raise _invalid("The parent must be a folder.", "not_a_folder")
        new_name = name or node.name or ""
        if (parent.id, new_name) == (node.parent_id, node.name) and node.revision in (
            revision,
            revision + 1,
        ):
            self._after("move")
            return node.out(self.namespace_id)
        if node.revision != revision:
            raise _revision_conflict(node.revision)
        if parent.id == node.id or parent in self._live_below(node):
            raise _invalid("A folder can't be moved into itself.", "into_itself")
        existing = self._child(parent.id, new_name)
        if existing is not None and existing.id != node.id:
            self._check_free(parent, new_name)
        node.parent_id, node.name = parent.id, new_name
        node.revision += 1
        self._record(ChangeType.MOVED, node)
        self._after("move")
        return node.out(self.namespace_id)

    def _live_below(self, node: FakeNode) -> list[FakeNode]:
        found: list[FakeNode] = []
        level = [node.id]
        while level:
            children = [n for n in self.nodes.values() if n.parent_id in level and not n.trashed]
            found.extend(children)
            level = [n.id for n in children]
        return found

    @_locked
    def upload(
        self,
        source: SourceFile,
        target: UploadTarget,
        stop: threading.Event,
        progress: Progress = no_progress,
    ) -> NodeOut:
        self._call("upload")
        info = source.path.stat()
        content = source.path.read_bytes()
        if (info.st_size, info.st_mtime_ns) != (source.size, source.mtime_ns) or sha(
            content
        ) != source.sha256:
            raise SourceChangedError(str(source.path))
        if isinstance(target, NewFile):
            parent = self._live_folder(target.parent_id)
            self._check_free(parent, target.name)
            node = FakeNode(uuid.uuid4(), parent.id, target.name, NodeKind.FILE, content)
            self.nodes[node.id] = node
            self._record(ChangeType.CREATED, node)
        else:
            existing = self.nodes.get(target.node_id)
            if existing is None or existing.trashed:
                raise _revision_conflict(None)
            if existing.revision != target.base_revision:
                raise _revision_conflict(existing.revision)
            existing.content = content
            existing.revision += 1
            existing.modified_at = datetime.now(UTC)
            node = existing
            self._record(ChangeType.MODIFIED, node)
        progress(len(content))
        self._after("upload")
        return node.out(self.namespace_id)

    @_locked
    def download(
        self,
        node_id: uuid.UUID,
        sha256: str,
        size: int,
        part: Path,
        stop: threading.Event,
        progress: Progress = no_progress,
    ) -> None:
        self._call("download")
        node = self.nodes.get(node_id)
        if node is None or node.trashed:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Not found")
        if sha(node.content) != sha256:
            raise RemoteContentChangedError(str(node_id))
        part.write_bytes(node.content)
        progress(len(node.content))
        self._after("download")

    # --- internals -------------------------------------------------------------------------

    def _call(self, method: str) -> None:
        self.calls.append(method)
        if self.offline:
            raise OfflineError("offline")
        self._trip(method, after=False)

    def _after(self, method: str) -> None:
        self._trip(method, after=True)

    def _trip(self, method: str, *, after: bool) -> None:
        for fault in self.faults:
            if fault.method != method or fault.after != after or fault.times <= 0:
                continue
            if fault.skip > 0:
                fault.skip -= 1
                continue
            fault.times -= 1
            raise fault.error

    def _record(self, kind: ChangeType, node: FakeNode) -> None:
        self.journal.append(
            ChangeOut(
                cursor=len(self.journal) + 1,
                type=kind,
                occurred_at=datetime.now(UTC),
                node_id=node.id,
                parent_id=node.parent_id,
                name=node.name,
                kind=node.kind,
                size_bytes=len(node.content) if node.kind == NodeKind.FILE else 0,
                content_hash=sha(node.content) if node.kind == NodeKind.FILE else None,
                revision=node.revision,
                modified_at=node.modified_at,
                modified_by_user_id=USER_ID,
            )
        )

    def _trash(self, node: FakeNode) -> None:
        node.revision += 1
        pending = [node]
        while pending:
            current = pending.pop()
            current.trashed = True
            current.trash_root = node.id
            pending.extend(
                n for n in self.nodes.values() if n.parent_id == current.id and not n.trashed
            )
        self._record(ChangeType.TRASHED, node)

    def _lookup(self, path: str) -> FakeNode:
        node = self.root
        for part in path.split("/") if path else []:
            child = self._child(node.id, part)
            assert child is not None, f"no {path} on the server"
            node = child
        return node

    def _child(self, parent_id: uuid.UUID, name: str) -> FakeNode | None:
        key = normalize_name(name)
        return next(
            (
                n
                for n in self.nodes.values()
                if n.parent_id == parent_id
                and not n.trashed
                and normalize_name(n.name or "") == key
            ),
            None,
        )

    def _live_node(self, node_id: uuid.UUID) -> FakeNode:
        node = self.nodes.get(node_id)
        if node is None or node.trashed:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Folder not found")
        return node

    def _live_folder(self, node_id: uuid.UUID) -> FakeNode:
        node = self.nodes.get(node_id)
        if node is None or node.trashed or node.kind != NodeKind.FOLDER:
            raise ApiError(404, ErrorCode.NOT_FOUND, "Folder not found")
        return node

    def _check_free(self, parent: FakeNode, name: str) -> None:
        existing = self._child(parent.id, name)
        if existing is not None:
            raise ApiError(
                409,
                ErrorCode.NAME_CONFLICT,
                "Name taken",
                {"node_id": str(existing.id), "kind": existing.kind.value},
            )


def _invalid(message: str, problem: str) -> ApiError:
    """400 VALIDATION_ERROR shaped as the server's (core.errors.invalid)."""
    return ApiError(
        400,
        ErrorCode.VALIDATION_ERROR,
        message,
        [{"loc": ["body"], "msg": message, "type": problem}],
    )


def _check_name(name: str) -> None:
    """As the server: NFR-COMPAT-02 names, with the NameProblem code as the issue type."""
    try:
        validate_name(name)
    except InvalidNameError as exc:
        raise _invalid(str(exc), exc.problem.value) from None


def _revision_conflict(current: int | None) -> ApiError:
    return ApiError(409, ErrorCode.REVISION_CONFLICT, "Changed", {"current_revision": current})


class FakeRecycleBin:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.broken = False

    def move_to_recycle_bin(self, path: Path) -> None:
        if self.broken:
            raise OSError("The Recycle Bin is turned off for this drive")
        target = self.folder / f"{len(list(self.folder.iterdir()))}-{path.name}"
        shutil.move(path, target)

    def items(self) -> Listing:
        """What was recycled, by original name, with its content (folders with theirs)."""
        result: Listing = []
        for item in self.folder.iterdir():
            name = item.name.split("-", 1)[1]
            for path, content in read_tree(item).items():
                result.append((f"{name}/{path}" if path else name, content))
            if item.is_dir():
                result.append((name, None))
        return sorted(result)


class NoAttributes:
    """Records read-only marks instead of setting them, so tests can still write the files
    (as a user who clears the attribute would)."""

    def __init__(self) -> None:
        self.read_only: dict[Path, bool] = {}

    def hide(self, path: Path) -> None:
        return None

    def set_read_only(self, path: Path, read_only: bool) -> None:
        self.read_only[path] = read_only

    def marked(self, root: Path) -> set[str]:
        """Paths below `root` currently marked read-only."""
        return {
            path.relative_to(root).as_posix()
            for path, read_only in self.read_only.items()
            if read_only and path.exists()
        }


class InodeIdentity:
    def file_id(self, path: Path) -> FileId:
        info = path.stat()
        return FileId(info.st_dev, info.st_ino)


class Recorder:
    def __init__(self) -> None:
        self.activities: list[tuple[str, str, str]] = []
        self.conflicts: list[tuple[str, str]] = []

    def activity(self, kind: str, path: str, detail: str = "") -> None:
        self.activities.append((kind, path, detail))

    def conflict(self, path: str, copy_path: str) -> None:
        self.conflicts.append((path, copy_path))


def read_tree(root: Path) -> Tree:
    """Everything under `root` (a file reads as {"": content})."""
    if root.is_file():
        return {"": root.read_bytes()}
    result: Tree = {}
    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        if relative.split("/", 1)[0] == ".dropshare-tmp":
            continue
        result[relative] = item.read_bytes() if item.is_file() else None
    return result


class Device:
    """One computer with a sync folder and an index, syncing with `server`."""

    _clock = time.time_ns() - 3_600_000_000_000  # an hour ago: every file is "stable"

    def __init__(self, base: Path, server: FileServer, *, max_path: int | None = None) -> None:
        self.max_path = max_path
        self.root = base / "DropShare"
        self.root.mkdir(parents=True)
        self.index_path = base / "index.db"
        self.index = Index(self.index_path)
        self.server = server
        self.bin = FakeRecycleBin(base / "bin")
        self.log = Recorder()
        self.attributes = NoAttributes()
        self.stop = threading.Event()
        self.core = self.restart()

    def restart(
        self,
        crash_point: Callable[[str], None] = no_crash,
        recycle_bin: RecycleBin | None = None,
        file_identity: FileIdentity | None = None,
        transfer: Callable[[TransferEvent], None] = lambda _: None,
    ) -> SyncCore:
        """A fresh engine over the same folder and index, as after the app restarts."""
        self.core = SyncCore(
            self.root,
            self.index,
            self.server,
            recycle_bin or self.bin,
            file_identity or InodeIdentity(),
            self.attributes,
            self.log,
            display_name=DISPLAY_NAME,
            stop=self.stop,
            stable_after_ns=0,
            max_path=self.max_path,
            crash_point=crash_point,
            transfer=transfer,
        )
        return self.core

    def sync(
        self,
        max_cycles: int = 12,
        *,
        confirm_deletes: bool = True,
        local: Iterable[str] | None = None,
    ) -> CycleResult:
        """Run cycles until nothing more is planned. A mass delete (FR-SYNC-08) is
        confirmed, or returned unconfirmed with `confirm_deletes=False`.

        By default every cycle looks at the whole folder (a full scan). With `local`, cycles
        look only at what the running engine would: those paths (watcher hints), the keys
        the fetched changes touched, and what the last cycle asked to retry."""
        if not self.core.connected:
            self.core.connect()
        pending = {key_of(path) for path in local} if local is not None else {""}
        for _ in range(max_cycles):
            dirty = self.core.fetch() | pending
            if local is None:
                dirty = {""}
            result = self.core.cycle(dirty) if dirty else CycleResult()
            if result.confirm_deletes is not None:
                if not confirm_deletes:
                    return result
                self.core.approve_deletes()
                pending = dirty
                continue
            pending = {key for key, delay in result.retry.items() if delay == 0}
            if result.again:
                pending |= outermost(dirty)
            if not pending and not result.again:
                return result
        raise AssertionError("sync didn't settle")

    # --- the user's actions on disk ----------------------------------------------------------

    def write(self, path: str, content: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        Device._clock += 1_000_000_000  # every edit gets a new, distinct modified time
        os.utime(target, ns=(Device._clock, Device._clock))

    def mkdir(self, path: str) -> None:
        (self.root / path).mkdir(parents=True)

    def delete(self, path: str) -> None:
        target = self.root / path
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    def tree(self) -> Tree:
        return read_tree(self.root)
