"""One sync cycle, start to finish, on the calling thread (algorithm.md §1).

fetch remote changes → snapshot the affected paths on disk, on the server and in the index →
hash what changed → reconcile → check for a mass delete → execute → report what to retry.
SyncEngine runs this on its own thread; tests call it directly.

The user's own namespace is followed, and each shared folder they added (FR-SHARE-02): a mount
appearing in their tree starts its namespace, a mount going away lets it go (sync/mounts.py).
"""

import concurrent.futures
import dataclasses
import functools
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import FileAttributes, FileIdentity, RecycleBin
from dropshare_client.services.errors import (
    ApiError,
    ContentMismatchError,
    OfflineError,
    RemoteContentChangedError,
    ScanPendingError,
    SessionExpiredError,
    SourceChangedError,
    TransferCancelledError,
    UpgradeRequiredError,
)
from dropshare_client.services.files import NewFile, NewVersion, UploadTarget, upload_key
from dropshare_client.store.index import Index
from dropshare_client.store.records import (
    Entry,
    HeldMove,
    JournalItem,
    NamespaceCursor,
    Problem,
    Refusal,
    RemoteNode,
    StoredUpload,
)
from dropshare_client.sync import paths
from dropshare_client.sync.errors import (
    CantSyncError,
    FileChangingError,
    LocalChangedError,
    RetryLaterError,
    SyncRootMissingError,
    SyncStoppedError,
    UnsafePathError,
)
from dropshare_client.sync.executor import (
    TOO_LONG_HERE,
    ActivityLog,
    Executor,
    Transfer,
    no_crash,
)
from dropshare_client.sync.mounts import MountRelease, ReleaseIncompleteError
from dropshare_client.sync.reconciler import (
    Action,
    Op,
    Plan,
    is_mass_delete,
    needs_hash,
    reconcile,
)
from dropshare_client.sync.remote_tree import RemoteItem, RemoteTree, TreeDelta, from_node
from dropshare_client.sync.scanner import STABLE_AFTER_NS, LocalItem, Scanner
from dropshare_client.sync.server import FileServer
from dropshare_client.sync.status import malware_reason
from dropshare_client.sync.transfers import Direction, TransferEvent, TransferReporter
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.error_details import MalwareDetectedDetails
from dropshare_shared.schemas.nodes import AccessRole, NamespaceKind, NamespaceOut, NodeKind
from dropshare_shared.schemas.sync import ChangeType
from dropshare_shared.schemas.uploads import UploadStatus

logger = logging.getLogger(__name__)

# Seconds before looking again at an item that is still being written (FR-SYNC-06).
STILL_WRITING_DELAY = 2.0
# Locked or failing items: 2 s, 4 s, ... up to this (windows-fs.md "Locked and changing").
MAX_RETRY_DELAY = 60.0
MAX_TRANSFERS = 4  # files moving at once (FR-XFER-03)
# The most the setting allows (OQ-39); the pool has this many threads, started as needed, so the
# setting can change while syncing.
MAX_TRANSFERS_LIMIT = 8
# Smaller files go first; once only larger ones are left running, a cycle ends without waiting
# for them, so a small change made meanwhile is planned and sent beside them (FR-XFER-03).
SMALL_TRANSFER_BYTES = 4 * 1024 * 1024
# How often the engine looks up from waiting on transfers, to pass on a stop or pause.
_TRANSFER_POLL_SECONDS = 0.2
# How soon a cycle looks again at a file still moving after the cycle ended.
_IN_FLIGHT_RECHECK = 1.0
# The server state moved on: refetch and plan again right away.
_REPLAN = frozenset({ErrorCode.REVISION_CONFLICT, ErrorCode.NAME_CONFLICT, ErrorCode.NOT_FOUND})
# Our copy of the node may be out of date: ask the server for it before planning again.
_REFRESH = frozenset({ErrorCode.REVISION_CONFLICT, ErrorCode.NOT_FOUND})
# Passing trouble on the way or on the server: try again a little later.
_TRANSIENT: frozenset[str] = frozenset(
    {
        ErrorCode.UPLOAD_INCOMPLETE,
        ErrorCode.CHUNK_HASH_MISMATCH,
        ErrorCode.RATE_LIMITED,
        ErrorCode.SERVICE_UNAVAILABLE,
        ErrorCode.INTERNAL_ERROR,
        "INVALID_RESPONSE",
    }
)
# Refused for good by the server (FR-SAFE-01/02): listed, and not sent again until the file
# changes, is renamed or moved (FR-SAFE-03). Never retried in a loop.
_REFUSED: frozenset[str] = frozenset({ErrorCode.FILE_TYPE_NOT_ALLOWED, ErrorCode.MALWARE_DETECTED})
_SCANNING = "Waiting for the virus scan. It syncs once the scan is done."
_SCAN_WAIT_MAX = 300.0  # seconds: the longest wait between asking again
# Failures the user can do something about; anything else is retried later.
_PROBLEM_REASONS: dict[str, str] = {
    ErrorCode.QUOTA_EXCEEDED: "There isn't enough space left in your DropShare storage. "
    "Free up space (empty the DropShare trash) or ask your administrator for more.",
    ErrorCode.FILE_TOO_LARGE: "The file is larger than the maximum DropShare accepts.",
    ErrorCode.FORBIDDEN: "You don't have permission to change this item.",
}
# Why a transfer waits (FR-CLIENT-08); it continues on its own once the cause is gone.
_IN_USE = "Open in another program. It syncs once it's closed."
_TRY_AGAIN = "Will try again shortly."
_STOPPED_BECAUSE: dict[type[Exception], str] = {
    OfflineError: "Waiting for the connection.",
    SessionExpiredError: "Waiting for you to sign in again.",
    UpgradeRequiredError: "Waiting for DropShare to be updated.",
    SyncRootMissingError: "Waiting for the sync folder to be back.",
    SyncStoppedError: "Paused.",
}


def _stopped_because(exc: Exception) -> str:
    return next(text for kind, text in _STOPPED_BECAUSE.items() if isinstance(exc, kind))


# Changes a viewer makes that the server refuses; the engine puts them right (FR-SHARE-05).
_VIEWER_UNDOES = frozenset({Op.UPLOAD, Op.MAKE_REMOTE_FOLDER, Op.DELETE_REMOTE})
_TRANSFERS = frozenset({Op.UPLOAD, Op.DOWNLOAD})
# Reads of every namespace per fetch while items keep moving between them.
_FETCH_PASSES = 4
# Passed through to the engine: they stop the whole cycle, not just one item.
_FATAL = (
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
    SyncRootMissingError,
    SyncStoppedError,
)


class NoPersonalFolderError(Exception):
    """The server listed no personal namespace for this user."""


@dataclass
class CycleResult:
    again: bool = False  # run another cycle right away (after fetching changes)
    retry: dict[str, float] = field(default_factory=dict)  # key -> seconds from now
    confirm_deletes: tuple[int, int] | None = None  # (here, on the server): waiting for OK
    changed_anything: bool = False


class SyncCore:
    def __init__(
        self,
        root: Path,
        index: Index,
        server: FileServer,
        recycle_bin: RecycleBin,
        file_identity: FileIdentity,
        file_attributes: FileAttributes,
        log: ActivityLog,
        *,
        display_name: str,
        stop: threading.Event,
        stable_after_ns: int = STABLE_AFTER_NS,
        max_path: int | None = None,
        crash_point: Callable[[str], None] = no_crash,
        progress: Callable[[int], None] = lambda _: None,
        transfer: Callable[[TransferEvent], None] = lambda _: None,
        max_transfers: int = MAX_TRANSFERS,
    ) -> None:
        self._index = index
        self._max_transfers = _transfers_allowed(max_transfers)
        # Transfers moving now, possibly started by an earlier cycle; finished ones are
        # recorded by the next cycle (or when the engine stops). Engine thread only.
        self._pool: concurrent.futures.ThreadPoolExecutor | None = None
        self._flight: dict[concurrent.futures.Future[object], Transfer] = {}
        self._last_progress: int | None = None
        self._server = server
        self._recycle_bin = recycle_bin
        self._file_attributes = file_attributes
        self._log = log
        self._display_name = display_name
        self._stop = stop
        self._crash_point = crash_point
        self._progress = progress
        self._transfers = TransferReporter(transfer)
        self._scanner = Scanner(
            root, file_identity, stable_after_ns=stable_after_ns, max_path=max_path
        )
        self._namespace: NamespaceCursor | None = None  # the user's own
        self._shared: dict[uuid.UUID, NamespaceCursor] = {}  # shared folders they added
        # Keys of items that left one namespace and haven't been seen arriving in another
        # yet: held back for a cycle, so a move in flight is never planned as a delete.
        self._in_transit: set[str] = set()
        # The user's role in each shared folder followed; view-only ones are read-only on disk.
        self._roles: dict[uuid.UUID, AccessRole | str] = {}
        self._remark: set[uuid.UUID] = set()  # role changed: mark the files again
        self._tree: RemoteTree | None = None
        self._release = MountRelease(self._scanner, index, recycle_bin, file_attributes, log, stop)
        self._attempts: dict[str, int] = {}
        self._refused_now: set[str] = set()  # actions the server refused this cycle
        # Files waiting for the server's malware scan, as hashed: (size, mtime, SHA-256) by
        # key, so waiting doesn't read a large file again and again (FR-SAFE-01).
        self._scan_hashes: dict[str, tuple[int, int, str]] = {}
        self._pending_deletes: frozenset[str] = frozenset()
        self._approved_deletes: frozenset[str] = frozenset()

    # --- connecting and remote changes ---------------------------------------------------

    def connect(self) -> None:
        """Find the personal namespace and load what we know of it."""
        personal = next(
            (n for n in self._server.namespaces() if n.kind == NamespaceKind.PERSONAL), None
        )
        if personal is None:
            raise NoPersonalFolderError()
        # What the server refuses (FR-SAFE-03), asked afresh each time. Before anything counts
        # as connected: if it can't be asked, connecting fails and is tried again.
        self._scanner.banned = self._server.banned_extensions(refresh=True)
        known = {c.namespace_id: c for c in self._index.cursors()}
        cursor = known.get(personal.id)
        if cursor is None or cursor.root_node_id != personal.root_node_id:
            if cursor is not None:
                # A different root: the base describes another tree. Without a base nothing
                # is deleted, so start from links and conflicted copies instead.
                logger.warning("The personal folder's root changed; forgetting the base")
                self._index.remove_entries("", subtree=True)
            self._index.start_namespace(personal.id, personal.root_node_id)
            cursor = NamespaceCursor(personal.id, personal.root_node_id, 0)
        self._namespace = cursor
        self._load_tree()

    def _load_tree(self) -> None:
        """The remote tree and cursors as last saved (they are always saved together)."""
        namespace = self._namespace
        if namespace is None:
            raise RuntimeError("connect() first")
        saved = {c.namespace_id: c for c in self._index.cursors()}
        self._namespace = saved.pop(namespace.namespace_id, namespace)
        self._shared = saved
        roots = {c.namespace_id: c.root_node_id for c in saved.values()}
        self._tree = RemoteTree(namespace.root_node_id, self._index.remote_nodes(), roots)

    @property
    def connected(self) -> bool:
        return self._namespace is not None

    def cursors(self) -> dict[uuid.UUID, int]:
        if self._namespace is None:
            return {}
        found = {self._namespace.namespace_id: self._namespace.cursor}
        found.update((c.namespace_id, c.cursor) for c in self._shared.values())
        return found

    def _cursor_of(self, namespace_id: uuid.UUID) -> int:
        namespace, _ = self._require_connected()
        if namespace_id == namespace.namespace_id:
            return namespace.cursor
        shared = self._shared.get(namespace_id)
        return 0 if shared is None else shared.cursor

    def fetch(self) -> set[str]:
        """Apply every new change, then settle anything the journal says was interrupted;
        returns the keys the changes touched. If anything fails part way, the tree in memory
        goes back to what was saved, so it never runs ahead of its cursor."""
        try:
            dirty = self._fetch_pages()
        except BaseException:
            self._load_tree()
            raise
        self.recover()
        return dirty

    def _fetch_pages(self) -> set[str]:
        """Every followed namespace, the user's own first: it decides which shared folders
        are followed. An item moved between two namespaces leaves one journal and arrives in
        the other; while items keep leaving, everything is read again (a few times at most).
        Whatever left and still hasn't arrived is held back from the next cycle, so a move in
        flight is never planned as a delete (requirements §10.1)."""
        _, tree = self._require_connected()
        dirty: set[str] = set()
        left: dict[uuid.UUID, str] = {}
        for _ in range(_FETCH_PASSES):
            moved_out = self._fetch_all(dirty, left)
            if not moved_out:
                break
        # A folder shared meanwhile arrived as its namespace's root, which its mount stands for.
        roots = {shared.root_node_id for shared in self._shared.values()}
        self._in_transit = {
            key
            for node_id, key in left.items()
            if tree.node(node_id) is None and node_id not in roots
        }
        if self._shared:  # roles change on the server; they take effect here at the next sync
            self._learn_roles(self._server.namespaces())
        return dirty

    def _learn_roles(self, listed: list[NamespaceOut]) -> None:
        for namespace in listed:
            if namespace.id in self._shared and self._roles.get(namespace.id) != namespace.role:
                self._roles[namespace.id] = namespace.role
                self._remark.add(namespace.id)

    def view_only(self, namespace_id: uuid.UUID) -> bool:
        return self._roles.get(namespace_id) == AccessRole.VIEWER

    def _fetch_all(self, dirty: set[str], left: dict[uuid.UUID, str]) -> bool:
        """One pass over every namespace; whether anything moved out of one meanwhile."""
        namespace, _ = self._require_connected()
        moved_out = self._fetch_namespace(namespace, dirty, left)
        self._follow_mounts(dirty)
        lost: list[uuid.UUID] = []
        for shared in list(self._shared.values()):
            try:
                moved_out |= self._fetch_namespace(shared, dirty, left)
            except ApiError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                lost.append(shared.namespace_id)
        if lost:
            self._drop_lost(lost, dirty)
        return moved_out

    def _fetch_namespace(
        self, namespace: NamespaceCursor, dirty: set[str], left: dict[uuid.UUID, str]
    ) -> bool:
        """Read one namespace's journal to its end; whether anything moved out of it."""
        _, tree = self._require_connected()
        moved_out = False
        while True:
            page = self._server.changes(namespace.namespace_id, namespace.cursor)
            moved_out |= any(c.type == ChangeType.REMOVED for c in page.data)
            delta = tree.apply(namespace.namespace_id, page.data)
            for node_id in delta.refetch:  # a change type this client doesn't know
                self._refresh_node(node_id, tree, delta)
            left.update(delta.left)
            advanced = page.meta.cursor > namespace.cursor
            namespace = dataclasses.replace(
                namespace, cursor=max(page.meta.cursor, namespace.cursor)
            )
            self._index.save_remote(delta.upserts.values(), delta.removed, namespace)
            if (
                self._namespace is not None
                and namespace.namespace_id == self._namespace.namespace_id
            ):
                self._namespace = namespace
            else:
                self._shared[namespace.namespace_id] = namespace
            dirty |= delta.dirty
            if not page.meta.has_more or not advanced:
                return moved_out

    def _follow_mounts(self, dirty: set[str]) -> None:
        """Start the shared folders whose mounts appeared; let go of those whose mounts went
        (the user left, or lost access). A folder whose mount changed (left and added again
        while this device wasn't looking) is let go and started again from the beginning.

        Which mount a folder is followed through is saved with its cursor, so even after a
        crash the folder its mount left is known and gets released. The release itself waits
        for the cycle, as it touches the disk; it is journaled before the namespace is
        forgotten."""
        _, tree = self._require_connected()
        mounts = tree.mounts()
        for namespace_id, shared in list(self._shared.items()):
            if mounts.get(namespace_id) == shared.mount_node_id:
                continue
            mount_id = shared.mount_node_id
            entry = None if mount_id is None else self._index.entry_of(mount_id)
            if entry is not None:
                self._index.begin("release_mount", entry.key, {"path": entry.path})
                dirty.add(entry.key)
            delta = TreeDelta()
            tree.unfollow(namespace_id, delta)
            self._index.forget_namespace(namespace_id)
            del self._shared[namespace_id]
            logger.info("Stopped following shared folder %s", namespace_id)
        new = mounts.keys() - self._shared.keys()
        if new:
            listing = self._server.namespaces()
            listed = {n.id: n for n in listing}
            for namespace_id in new:
                found = listed.get(namespace_id)
                if found is None:
                    continue  # no access any more: its mount goes to the trash next
                mount_id = mounts[namespace_id]
                self._index.start_namespace(namespace_id, found.root_node_id, mount_id)
                self._shared[namespace_id] = NamespaceCursor(
                    namespace_id, found.root_node_id, 0, mount_id
                )
                tree.follow(namespace_id, found.root_node_id)
                logger.info("Following shared folder %s", namespace_id)
            self._learn_roles(listing)
        for namespace_id, mount_id in mounts.items():
            followed = self._shared.get(namespace_id)
            if followed is not None:
                self._take_over(followed.root_node_id, mount_id)

    def _take_over(self, root_id: uuid.UUID, mount_id: uuid.UUID) -> None:
        """A folder the user shared becomes the root of its namespace, and a mount (a new node)
        takes its place (OQ-30): its synced entry now stands for the mount, so the unchanged
        folder isn't mistaken for one deleted and another created."""
        entry = self._index.entry_of(root_id)
        node = self._require_connected()[1].node(mount_id)
        if entry is not None and node is not None:
            self._index.put_entry(
                dataclasses.replace(entry, node_id=mount_id, revision=node.revision)
            )

    def _drop_lost(self, lost: list[uuid.UUID], dirty: set[str]) -> None:
        """Shared folders the server no longer lets us read. Their mounts go to the trash in
        the same step on the server, so normally the user's own journal says so first; if it
        doesn't list them any more either, let them go now."""
        _, tree = self._require_connected()
        listed = {n.id for n in self._server.namespaces()}
        delta = TreeDelta()
        for namespace_id in lost:
            mount_id = tree.mounts().get(namespace_id)
            if namespace_id not in listed and mount_id is not None:
                tree.remove(mount_id, delta)
        self._index.save_remote(delta.upserts.values(), delta.removed)
        dirty |= delta.dirty
        self._follow_mounts(dirty)

    def _refresh_node(self, node_id: uuid.UUID, tree: RemoteTree, delta: TreeDelta) -> None:
        """Ask the server for one node and take its answer as the truth."""
        try:
            node = from_node(self._server.get_node(node_id))
        except ApiError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
            node = None
        if node is None:
            tree.remove(node_id, delta)
        else:
            tree.upsert(node, delta)

    def recover(self) -> None:
        """Settle operations that were interrupted (algorithm.md §10): by a crash, or by an
        answer that never came. Runs after every fetch, once the tree is current.

        Every executor step is safe to redo through normal reconciliation; the one thing the
        journal adds is an upload that reached the server before the crash. Recording it as
        the base turns a later local edit into an ordinary upload, not a conflicted copy.
        """
        _, tree = self._require_connected()
        for item in self._index.journal():
            if item.kind == "release_mount":
                continue  # carried out by the next cycle (_release_mounts)
            if item.kind == "upload":
                self._recover_upload(item.key, item.data, tree)
            else:
                logger.info(
                    "Journal: %s of %s was interrupted; the next cycle checks it",
                    item.kind,
                    item.key,
                )
            self._index.finish(item.id)

    def _recover_upload(self, key: str, data: dict[str, str], tree: RemoteTree) -> None:
        sha256 = data.get("sha256")
        remote = tree.items_under(key).get(key)
        if remote is None or sha256 is None or remote.node.sha256 != sha256:
            return  # not done: the session stays, so the next attempt resumes it
        session = data.get("session")
        if session is not None:  # done: it must never be replayed for another file (B2)
            self._index.forget_upload_session(session)
        try:
            path = self._scanner.resolve(key)
        except UnsafePathError:
            return
        if path is None:
            return
        logger.info("Journal: the upload of %s had completed", path)
        node = remote.node
        # Size and time no file has, so whatever is on disk now is hashed and compared.
        self._index.put_entry(
            Entry(key, path, node.node_id, node.kind, node.revision, node.sha256, -1, -1, None)
        )

    # --- mass-delete confirmation --------------------------------------------------------

    def approve_deletes(self) -> None:
        """The user confirmed the deletes the last cycle stopped at (FR-SYNC-08)."""
        self._approved_deletes = self._pending_deletes

    # --- the cycle -----------------------------------------------------------------------

    def cycle(self, dirty: set[str]) -> CycleResult:
        _, tree = self._require_connected()
        self._scanner.require_root()
        result = CycleResult()
        self._refused_now = set()
        self._settle_finished(tree, result)
        released, held, stuck = self._release_mounts(result)
        dirty = dirty | released
        self._mark_read_only()
        local: dict[str, LocalItem] = {}
        remote: dict[str, RemoteItem] = {}
        base: dict[str, Entry] = {}
        # Files still moving aren't planned again until they have finished.
        deferred: set[str] = {transfer.action.key for transfer in self._flight.values()}
        for key in deferred:
            result.retry.setdefault(key, _IN_FLIGHT_RECHECK)
        refused: set[str] = set()  # can't be synced until the user changes them
        # Both ends of a held move are always looked at together: seeing only the old one, its
        # item would read as deleted here, and the server copy go to the trash (OQ-59 (1)).
        held_moves = self._index.held_moves()
        dirty = dirty | _partners(dirty, held_moves, tree)
        collected: set[str] = set()
        roots = paths.outermost(dirty)
        while roots:
            for root in roots:
                self._collect(root, tree, local, remote, base, deferred, refused, result)
            collected |= roots
            roots = self._moved_out_of_view(roots, dirty, tree, remote, base)
            dirty = dirty | roots
        self._scanner.require_root()  # a vanished root must never read as "all deleted"
        self._refuse_too_long(remote, base, deferred)
        # Not decided this cycle: folders still being let go, and items in flight between
        # two namespaces. Deferred keys are left alone, with everything below them.
        for key in held | self._in_transit:
            deferred.add(key)
            result.retry.setdefault(key, STILL_WRITING_DELAY)
        self._in_transit = set()
        for key, path in stuck.items():  # after collecting, which clears what it looks at
            self._cant_sync(
                key,
                path,
                "You no longer have this shared folder, but it couldn't be put away yet (a "
                "file in it may be open in another program). DropShare tries again.",
            )

        self._hash_changed(local, base, deferred, result)
        self._hash_held(local, base, refused)
        self._identify_new(local, base)
        self._adopt_own_uploads(tree, local, remote, base, deferred)
        if not self._hold_moves(held_moves, collected, tree, local, deferred, refused, result):
            return result  # a held item moved on: a full scan finds where, first
        plan = reconcile(
            local.keys() | remote.keys() | base.keys(), local, remote, base, deferred, refused
        )
        self._keep_held(plan, held_moves, base)
        if (
            plan.deletes
            and not self._deletes_approved(plan)
            and is_mass_delete(plan.deletes, self._index.file_count())
        ):
            self._pending_deletes = _delete_keys(plan)
            result.confirm_deletes = (plan.local_deletes, plan.remote_deletes)
            return result
        self._approved_deletes = frozenset()
        self._execute(plan, tree, result)
        result.again = result.again or plan.again
        return result

    def _release_mounts(self, result: CycleResult) -> tuple[set[str], set[str], dict[str, str]]:
        """Let shared folders go that the user left or lost access to (OQ-33), before
        anything else looks at them: they'd otherwise read as deleted on the server.
        Returns (keys to look at now, keys of releases still pending, the stuck ones by key
        with their path). A pending release is
        tried again later and its folder left alone meanwhile, so nothing in it is uploaded
        to the user's own folder or deleted; one that keeps failing is listed in Can't sync
        without stopping the rest of the sync."""
        touched: set[str] = set()
        held: set[str] = set()
        stuck: dict[str, str] = {}
        for item in self._index.journal():
            if item.kind != "release_mount":
                continue
            try:
                kept = self._release.release(
                    item.key, item.data.get("kept"), functools.partial(self._kept_in, item)
                )
            except FileChangingError:
                held.add(item.key)
                result.retry[item.key] = STILL_WRITING_DELAY
                continue
            except (ReleaseIncompleteError, UnsafePathError, OSError) as exc:
                logger.info("Letting %s go waits: %s", item.key, exc)
                held.add(item.key)
                result.retry[item.key] = self._backoff(item.key)
                stuck[item.key] = item.data.get("path", item.key)
                continue
            self._index.finish(item.id)
            self._attempts.pop(item.key, None)
            touched.add(item.key)
            if kept is not None:
                touched.add(kept)
            result.changed_anything = True
        return touched, held, stuck

    def _mark_read_only(self) -> None:
        """Files in a view-only shared folder are read-only on disk, and writable again once
        the user may edit it (FR-SHARE-05). New downloads are marked as they arrive."""
        while self._remark:
            namespace_id = self._remark.pop()
            shared = self._shared.get(namespace_id)
            mount_id = None if shared is None else shared.mount_node_id
            entry = None if mount_id is None else self._index.entry_of(mount_id)
            if entry is None:
                continue  # not here yet: its files are marked as they're downloaded
            read_only = self.view_only(namespace_id)
            for item in self._scanner.scan(entry.key).present():
                if item.kind == NodeKind.FILE:
                    self._file_attributes.set_read_only(
                        self._scanner.disk_path(item.path), read_only
                    )

    def _kept_in(self, item: JournalItem, kept: str) -> None:
        self._index.update_journal(item.id, {**item.data, "kept": kept})

    def _collect(
        self,
        root: str,
        tree: RemoteTree,
        local: dict[str, LocalItem],
        remote: dict[str, RemoteItem],
        base: dict[str, Entry],
        deferred: set[str],
        refused: set[str],
        result: CycleResult,
    ) -> None:
        """Snapshot `root` and everything below it: on disk, on the server and in the index."""
        scan = self._scanner.scan(root)
        entries = self._index.entries_under(root)
        scan.accept_synced(entries)
        local.update(scan.items)
        # Refused but there (deferred below): only so a synced item renamed to one of these
        # is seen as moved, not deleted (OQ-59 (1)). Nothing is planned for them.
        local.update(scan.held)
        refused.update(scan.held)
        deferred |= scan.deferred
        for key in scan.still_writing:
            result.retry[key] = STILL_WRITING_DELAY
        for key in scan.unreadable:
            result.retry[key] = self._backoff(key)
        self._index.clear_problems(root)
        problems = dict(scan.problems)
        remote.update(tree.items_under(root, problems))
        for problem in problems.values():
            self._index.set_problem(problem)
        base.update(entries)
        self._still_refused(root, local, deferred, refused)

    def _hold_moves(
        self,
        held_moves: list[HeldMove],
        collected: set[str],
        tree: RemoteTree,
        local: dict[str, LocalItem],
        deferred: set[str],
        refused: set[str],
        result: CycleResult,
    ) -> bool:
        """Apply the held moves this cycle looks at. While the item is still at its refused new
        place, its old key, and wherever the server has the item now, are left alone, however
        the item changed meanwhile (a "safe save" gives it a new file ID): never read as
        deleted, never downloaded beside it (OQ-59 (1)). A hold ends only when the item has
        moved on, its node is gone, or its place isn't refused any more.
        False: a held item has moved on and this cycle doesn't see everything, so nothing may
        be planned now (its old key would read as deleted); the full scan asked for decides."""
        complete = True
        for held in held_moves:
            server_key = _key_on_server(tree, held.node_id)
            ends = (held.old_key, held.target_key, server_key or held.old_key)
            if not any(_covered(end, collected) for end in ends):
                continue
            if server_key is None:  # gone from the server: nothing to keep
                self._index.clear_held_move(held.old_key)
                continue
            item = local.get(held.target_key)
            if item is None:
                # Moved on from the held place. A full scan finds where it went; a partial
                # one might not, so the old key waits for one rather than reading as deleted.
                if "" in collected:
                    self._index.clear_held_move(held.old_key)
                else:
                    result.retry[""] = 0.0
                    complete = False
                continue
            if held.reason is not None:  # the server refused it: held here too
                deferred.add(held.target_key)
                refused.add(held.target_key)
                self._index.set_problem(Problem(held.target_key, item.path, held.reason))
            elif not _is_refused(held.target_key, refused):
                # Its place syncs now (the type was allowed again): an ordinary move from here.
                self._index.clear_held_move(held.old_key)
                continue
            deferred.add(held.old_key)
            deferred.add(server_key)
        return complete

    def _keep_held(
        self,
        plan: Plan,
        held_moves: list[HeldMove],
        base: dict[str, Entry],
    ) -> None:
        """Remember the moves this cycle held. They are forgotten only by `_hold_moves`, when
        it sees they are over."""
        known = {held.old_key: held for held in held_moves}
        for old_key, target in plan.parked.items():
            entry, before = base.get(old_key), known.get(old_key)
            if entry is None or before is not None:
                continue  # not a synced item's old key, or already held
            self._index.set_held_move(HeldMove(old_key, target, entry.node_id))

    def _still_refused(
        self, root: str, local: dict[str, LocalItem], deferred: set[str], refused: set[str]
    ) -> None:
        """What the server refused stays refused while it is unchanged on disk: left alone and
        listed, never sent again (FR-SAFE-03). Changed, renamed or gone: it is forgotten, and
        synced as what it is now."""
        for refusal in self._index.refusals_under(root):
            item = local.get(refusal.key)
            if item is not None and (item.size, item.mtime_ns) == (refusal.size, refusal.mtime_ns):
                deferred.add(refusal.key)
                refused.add(refusal.key)
                self._index.set_problem(Problem(refusal.key, item.path, refusal.reason))
            else:
                self._index.clear_refusal(refusal.key)

    def _refuse_too_long(
        self, remote: dict[str, RemoteItem], base: dict[str, Entry], deferred: set[str]
    ) -> None:
        """Server items whose path here would be too long for Windows (D4) are left alone,
        with everything below them, and listed once, like the scanner's local ones. Planned,
        they'd be refused on every cycle and retried forever. A synced item moved there on
        the server stays where it is here: its old key is held too, so the move neither
        runs nor waits, and the old copy is never read as deleted."""
        refused: list[str] = []
        for key in sorted(remote):  # a folder sorts before what's inside it
            if any(paths.is_within(key, done) for done in refused):
                continue
            item = remote[key]
            if not self._scanner.too_long(item.path, folder=item.node.kind == NodeKind.FOLDER):
                continue
            refused.append(key)
            if key not in deferred:  # else already left alone, and listed if it's a problem
                deferred.add(key)
                self._index.set_problem(Problem(key, item.path, TOO_LONG_HERE))
        if not refused:
            return
        synced_at = {entry.node_id: key for key, entry in base.items()}
        for key, item in remote.items():
            old = synced_at.get(item.node.node_id)
            if old is not None and old != key and any(paths.is_within(key, r) for r in refused):
                deferred.add(old)

    @staticmethod
    def _moved_out_of_view(
        roots: set[str],
        looked_at: set[str],
        tree: RemoteTree,
        remote: dict[str, RemoteItem],
        base: dict[str, Entry],
    ) -> set[str]:
        """Where synced items that left the paths looked at are on the server now. Without
        them this cycle would read those items as deleted there, and a move would become a
        delete plus a new copy (audit B1). Empty once everything is in view."""
        here = {item.node.node_id for item in remote.values()}
        found: set[str] = set()
        for entry in base.values():
            if entry.node_id in here:
                continue
            path = tree.path_of(entry.node_id)
            if path is None:
                continue  # gone from the server
            key = paths.key_of(path)
            if not any(paths.is_within(key, seen) for seen in looked_at | roots):
                found.add(key)
        return paths.outermost(found)

    def _hash_changed(
        self,
        local: dict[str, LocalItem],
        base: dict[str, Entry],
        deferred: set[str],
        result: CycleResult,
    ) -> None:
        for key, item in list(local.items()):
            if key in deferred or not needs_hash(item, base.get(key)):
                continue
            known = self._scan_hashes.get(key)
            if known is not None and known[:2] == (item.size, item.mtime_ns):
                local[key] = dataclasses.replace(item, sha256=known[2])  # unchanged since
                continue
            if self._stop.is_set():
                raise SyncStoppedError()
            try:
                local[key] = self._scanner.hash(item, self._stop)
            except (FileChangingError, FileNotFoundError):
                deferred.add(key)
                result.retry[key] = STILL_WRITING_DELAY
            except OSError as exc:  # locked by another program: "waiting" (FR-SYNC-06)
                logger.info("Can't read %s yet (%s); trying again later", item.path, exc)
                deferred.add(key)
                result.retry[key] = self._backoff(key)
                reason = _IN_USE if isinstance(exc, PermissionError) else _TRY_AGAIN
                self._transfers.waiting_for(key, item.path, Direction.UPLOAD, item.size, reason)

    def _scan_wait(self, key: str, retry_after: float) -> float:
        """How long until asking again about an upload the server is still scanning: what it
        asks, doubled each time it is still scanning, up to five minutes (FR-SAFE-01)."""
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        doubled: float = retry_after * 2.0 ** min(attempt - 1, 10)
        return min(doubled, max(retry_after, _SCAN_WAIT_MAX))

    def _hash_held(
        self, local: dict[str, LocalItem], base: dict[str, Entry], refused: set[str]
    ) -> None:
        """Held files new at their place, when a synced file of the same size is missing:
        their content can tell a move the file ID can't (a tool that saves a new copy), so the
        missing one is held, not read as deleted (algorithm §7, OQ-59 (1))."""
        missing = {
            entry.size
            for key, entry in base.items()
            if entry.kind == NodeKind.FILE and key not in local
        }
        for key in refused:
            item = local.get(key)
            if (
                item is None
                or item.kind != NodeKind.FILE
                or item.sha256 is not None
                or key in base
                or item.size not in missing
            ):
                continue
            try:
                local[key] = self._scanner.hash(item, self._stop)
            except (FileChangingError, OSError):
                logger.info("Couldn't read held %s to match it; not a move", item.path)

    def _adopt_own_uploads(
        self,
        tree: RemoteTree,
        local: dict[str, LocalItem],
        remote: dict[str, RemoteItem],
        base: dict[str, Entry],
        deferred: set[str],
    ) -> None:
        """Files this computer uploaded that the server published after its scan, while
        `complete` here answered "still scanning" (FR-SAFE-01): recorded as synced, as a
        finished upload would have been. A delete, rename or edit made here meanwhile then
        syncs as such: no file comes back, none is sent twice, and the next edit isn't a
        conflict with itself."""
        sessions = self._index.upload_sessions()
        if not sessions:
            return
        for key, item in remote.items():
            node = item.node
            if node.kind != NodeKind.FILE or node.sha256 is None or key in deferred:
                continue
            entry = base.get(key)
            if entry is not None and (
                entry.node_id != node.node_id or entry.revision >= node.revision
            ):
                continue
            stored_key = self._own_upload(sessions, tree, key, node, entry)
            if stored_key is None or not self._published_as(sessions[stored_key], node):
                continue
            here = local.get(key)
            synced = here is not None and here.sha256 == node.sha256
            adopted = Entry(
                key=key,
                path=here.path if here is not None else item.path,
                node_id=node.node_id,
                kind=NodeKind.FILE,
                revision=node.revision,
                sha256=node.sha256,
                # What is here now, if it is that content; else nothing matches, so the
                # change made here is planned from this new base.
                size=here.size if synced and here is not None else -1,
                mtime_ns=here.mtime_ns if synced and here is not None else 0,
                file_id=here.file_id if synced and here is not None else None,
            )
            self._index.adopt_upload(adopted, stored_key)  # both or neither
            self._scan_hashes.pop(key, None)
            self._attempts.pop(key, None)
            base[key] = adopted
            logger.info("Recorded %s as synced: published after its malware scan", key)

    def _published_as(self, stored: StoredUpload, node: RemoteNode) -> bool:
        """The server says this very upload completed as `node`. A session left behind (its
        file moved away mid-upload) never matches someone else's identical file: that one is
        downloaded, not taken for ours and deleted."""
        try:
            upload = self._server.upload_status(stored.upload_id)
        except (ApiError, OfflineError):
            logger.info("Couldn't ask about upload %s; not recorded yet", stored.upload_id)
            return False
        return (
            upload is not None
            and upload.status == UploadStatus.COMPLETED
            and upload.node_id == node.node_id
        )

    def _own_upload(
        self,
        sessions: dict[str, StoredUpload],
        tree: RemoteTree,
        key: str,
        node: RemoteNode,
        entry: Entry | None,
    ) -> str | None:
        """The stored session whose content the server published as `node`, if any."""
        if entry is not None:
            targets: list[UploadTarget] = [NewVersion(node.node_id, entry.revision)]
        else:
            parents = {node.parent_id, tree.node_at(paths.parent_of(key))} - {None}
            targets = [NewFile(parent, node.name or "") for parent in parents if parent]
        for target in targets:
            stored_key = upload_key(target)
            stored = sessions.get(stored_key)
            if stored is not None and (stored.sha256, stored.size) == (node.sha256, node.size):
                return stored_key
        return None

    def _identify_new(self, local: dict[str, LocalItem], base: dict[str, Entry]) -> None:
        """File IDs for move detection (FR-SYNC-10), only where they can matter: items that
        are new here (a moved item looks new at its new path) and entries saved without one.
        A full stat per item would be slow on Windows with a large folder (NFR-PERF-03)."""
        for key, item in local.items():
            entry = base.get(key)
            if item.file_id is None and (entry is None or entry.file_id is None):
                local[key] = dataclasses.replace(item, file_id=self._scanner.file_id(item.path))

    def _deletes_approved(self, plan: Plan) -> bool:
        return bool(self._approved_deletes) and _delete_keys(plan) <= self._approved_deletes

    def _execute(self, plan: Plan, tree: RemoteTree, result: CycleResult) -> None:
        executor = Executor(
            self._scanner,
            self._index,
            self._server,
            tree,
            self._recycle_bin,
            self._file_attributes,
            self._log,
            display_name=self._display_name,
            cursor=self._cursor_of,
            stop=self._stop,
            crash_point=self._crash_point,
            transfers=self._transfers,
            view_only=self.view_only,
        )
        for action in plan.actions:  # FR-CLIENT-08: what this cycle will send and fetch
            if action.op == Op.UPLOAD and action.local is not None:
                self._transfers.queued(
                    action.key, action.local.path, Direction.UPLOAD, action.local.size
                )
            elif action.op == Op.DOWNLOAD and action.remote is not None:
                self._transfers.queued(
                    action.key, action.remote.path, Direction.DOWNLOAD, action.remote.node.size
                )
        actions = list(plan.actions)
        position = 0
        while position < len(actions):
            if self._stop.is_set():
                raise SyncStoppedError()
            action = actions[position]
            if action.op in _TRANSFERS:  # the plan keeps them together, after folders and moves
                end = position
                while end < len(actions) and actions[end].op in _TRANSFERS:
                    end += 1
                after = len(actions) - end
                self._transfer_all(executor, actions[position:end], tree, result, after)
                position = end
                continue
            self._report_progress(len(actions) - position + len(self._flight))
            done_ok, created = self._run(executor, action, tree, result)
            position += 1
            if not done_ok:
                refused = action.key in self._refused_now
                if action.op in (Op.MOVE_LOCAL, Op.MOVE_REMOTE) and not refused:
                    self._hold_move_together(action, result)
                continue
            self._done(action, created, result)
        self._report_progress(len(self._flight))

    def _report_progress(self, pending: int) -> None:
        """Items still to do (FR-CLIENT-01 "Syncing (n items)"); sent only when it changes."""
        if pending != self._last_progress:
            self._last_progress = pending
            self._progress(pending)

    def _done(self, action: Action, created: str | None, result: CycleResult) -> None:
        self._attempts.pop(action.key, None)
        self._scan_hashes.pop(action.key, None)
        if created is not None:
            result.retry[created] = 0.0
        if action.op != Op.RECORD:
            result.changed_anything = True

    def _transfer_all(
        self,
        executor: Executor,
        actions: list[Action],
        tree: RemoteTree,
        result: CycleResult,
        after: int,
    ) -> None:
        """Up to `max_transfers` files at once, smallest first, so many small changes never
        wait behind one large file (FR-XFER-03). Each is prepared only when a slot is free,
        so nothing is started (or shown as moving) before its turn. Bytes move on transfer
        threads; everything recorded happens here, on the engine's thread, as each finishes.

        Once only large files are left moving, the cycle ends without them: the next cycle
        plans whatever changed meanwhile and sends it beside them, and records them when
        they finish. A stop or pause sends nothing more, stops the running ones within a
        moment and records how they ended; a fatal error leaves them to the next cycle."""
        waiting = sorted(actions, key=_transfer_size)
        while True:
            self._settle_finished(tree, result)
            while waiting and len(self._flight) < self._max_transfers and not self._stop.is_set():
                action = waiting.pop(0)
                prepare = functools.partial(executor.prepare, action)
                ok, prepared = self._attempt(action, prepare, tree, result)
                if ok and isinstance(prepared, Transfer):
                    self._flight[self._submit(prepared)] = prepared
            if self._stop.is_set():
                reason = _stopped_because(SyncStoppedError())
                for action in waiting:  # never started: nothing to tidy up
                    self._transfers.waiting(action.key, reason)
                self._drain(reason)
                raise SyncStoppedError()
            if not waiting and not self._flight:
                return
            if not waiting and all(t.size >= SMALL_TRANSFER_BYTES for t in self._flight.values()):
                for transfer in self._flight.values():
                    result.retry[transfer.action.key] = _IN_FLIGHT_RECHECK
                return
            self._report_progress(after + len(waiting) + len(self._flight))
            concurrent.futures.wait(
                self._flight,
                timeout=_TRANSFER_POLL_SECONDS,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )

    def set_max_transfers(self, count: int) -> None:
        """Files moving at once from now on (any thread: read before each start). Fewer than
        are moving lets the running ones finish."""
        self._max_transfers = _transfers_allowed(count)

    def _submit(self, transfer: Transfer) -> concurrent.futures.Future[object]:
        if self._pool is None:
            self._pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_TRANSFERS_LIMIT, thread_name_prefix="dropshare-transfer"
            )
        stop, transfers = self._stop, self._transfers

        def work() -> object:
            try:
                return transfer.work(stop)
            except TransferCancelledError:  # shown at once, recorded by the engine later
                transfers.waiting(transfer.action.key, _stopped_because(SyncStoppedError()))
                raise

        return self._pool.submit(work)

    def _settle_finished(self, tree: RemoteTree, result: CycleResult) -> None:
        """Record the transfers that have finished, on the engine's thread. One stopped by an
        earlier pause is only tidied up: it goes again in this cycle. While stopping, the
        stop itself records them all (`_drain`)."""
        if self._stop.is_set():
            return
        for future in [f for f in self._flight if f.done()]:
            transfer = self._flight.pop(future)
            key = transfer.action.key
            if isinstance(future.exception(), TransferCancelledError) and not self._stop.is_set():
                transfer.failed(TransferCancelledError())
                result.retry[key] = 0.0
                continue
            settle = functools.partial(_settle, transfer, future)
            ok, _ = self._attempt(transfer.action, settle, tree, result)
            if ok:
                self._done(transfer.action, None, result)

    def _drain(self, reason: str) -> None:
        """Wait for the running transfers (they are stopping) and record how each ended: one
        that got through is recorded as done, one that stopped is tidied up."""
        if not self._flight:
            return
        concurrent.futures.wait(self._flight)
        for future, transfer in list(self._flight.items()):
            del self._flight[future]
            key = transfer.action.key
            try:
                _settle(transfer, future)
            except TransferCancelledError:
                self._transfers.waiting(key, reason)
            except ScanPendingError:  # the server is still scanning it: asked again next run
                self._transfers.waiting(key, _SCANNING)
            except Exception:
                logger.warning("Couldn't record the transfer of %s", key, exc_info=True)
                self._transfers.waiting(key, _TRY_AGAIN)
            else:
                self._attempts.pop(key, None)

    def close(self) -> None:
        """The engine is stopping: let the running transfers end and record them."""
        self._drain(_stopped_because(SyncStoppedError()))
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def _run(
        self, executor: Executor, action: Action, tree: RemoteTree, result: CycleResult
    ) -> tuple[bool, str | None]:
        """(done, key of an item the step created)."""
        ok, created = self._attempt(action, functools.partial(executor.run, action), tree, result)
        return ok, created if isinstance(created, str) else None

    def _attempt(
        self,
        action: Action,
        step: Callable[[], object],
        tree: RemoteTree,
        result: CycleResult,
    ) -> tuple[bool, object]:
        """(done, what `step` returned). A failure that isn't fatal is recorded in `result`
        (what to retry and when) and reported as not done."""
        try:
            return True, step()
        except _FATAL as exc:
            self._transfers.waiting(action.key, _stopped_because(exc))
            raise
        except TransferCancelledError as exc:
            self._transfers.waiting(action.key, _stopped_because(SyncStoppedError()))
            raise SyncStoppedError() from exc
        except ScanPendingError as exc:
            # The server has every byte and scans it (FR-SAFE-01): ask again later, sending
            # nothing, each time a little later; the upload session survives a restart.
            result.retry[action.key] = self._scan_wait(action.key, exc.retry_after)
            item = action.local
            if item is not None and item.sha256 is not None:
                self._scan_hashes[action.key] = (item.size, item.mtime_ns, item.sha256)
            self._transfers.waiting(action.key, _SCANNING)
            return False, None
        except RetryLaterError as exc:
            result.again = True
            result.retry[action.key] = 0.0
            if exc.key:
                result.retry[exc.key] = 0.0
        except CantSyncError as exc:
            self._cant_sync(action.key, action.path, exc.reason)
        except UnsafePathError as exc:
            self._cant_sync(paths.key_of(exc.path), exc.path, exc.reason)
            self._transfers.failed(action.key, exc.reason)  # not retried until it changes
        except (
            LocalChangedError,
            FileChangingError,
            SourceChangedError,
            RemoteContentChangedError,
        ) as exc:
            logger.info(
                "%s changed while syncing (%s); planning it again",
                action.path,
                type(exc).__name__,
            )
            result.again = True
            result.retry[action.key] = STILL_WRITING_DELAY
        except ApiError as exc:
            self._api_failed(action, exc, tree, result)
        except PermissionError as exc:  # open in another program (FR-SYNC-06)
            logger.info("%s of %s waits: %s", action.op, action.path, exc)
            result.retry[action.key] = self._backoff(action.key)
            self._transfers.waiting(action.key, _IN_USE)
        except (ContentMismatchError, OSError) as exc:
            logger.warning("%s of %s failed: %s", action.op, action.path, exc)
            result.retry[action.key] = self._backoff(action.key)
        # Unless it was listed as can't sync (failed), it is tried again on its own.
        self._transfers.waiting(action.key, _TRY_AGAIN)
        return False, None

    def _hold_move_together(self, action: Action, result: CycleResult) -> None:
        """A move that didn't happen is retried as one: its old key, its target and where it
        is on disk get the same delay. Looked at apart, the old key would read as deleted on
        the side the item left, and the move would become a delete plus a new copy (audit
        B1). Without a delay of its own (can't sync), it backs off."""
        keys = {action.key, paths.key_of(action.path)}
        if action.base is not None:
            keys.add(action.base.key)
        if action.local is not None:
            keys.add(paths.key_of(action.local.path))
        delay = result.retry.get(action.key)
        if delay is None:
            delay = self._backoff(action.key)
        for key in keys:
            result.retry[key] = delay

    def _api_failed(
        self, action: Action, exc: ApiError, tree: RemoteTree, result: CycleResult
    ) -> None:
        key, path = action.key, action.path
        logger.info("%s of %s: %s (request %s)", action.op, path, exc.code, exc.request_id)
        if exc.code == ErrorCode.FORBIDDEN and action.op == Op.MOVE_REMOTE:
            self._undo_refused_move(action, result)
        elif (
            exc.code == ErrorCode.FORBIDDEN
            and action.op in _VIEWER_UNDOES
            and self._in_view_only(action, tree)
        ):
            self._undo_viewer_change(action, result)
        elif exc.code in _REFUSED and action.op in (Op.UPLOAD, Op.MOVE_REMOTE):
            self._refused(action, exc, result)
        elif exc.code == ErrorCode.SHARE_NOT_ALLOWED and action.op == Op.DELETE_REMOTE:
            # A folder the user shared, deleted here while others still have it: the server
            # keeps it. Forgetting the base makes it new here, so it comes back (edit wins).
            self._index.remove_entries(key, subtree=True)
            result.again = True
            result.retry[key] = 0.0
            self._log.activity("kept_shared_folder", path, exc.message)
        elif exc.code in _REPLAN:
            result.again = True
            result.retry[key] = 0.0
            if exc.code in _REFRESH and action.remote is not None:
                # Our copy may be stale (e.g. a replayed upload of a file since trashed).
                delta = TreeDelta()
                self._refresh_node(action.remote.node.node_id, tree, delta)
                self._index.save_remote(delta.upserts.values(), delta.removed)
        elif exc.code in _TRANSIENT or exc.status >= 500 or exc.status == 429:
            result.retry[key] = self._backoff(key)
        elif exc.code == ErrorCode.QUOTA_EXCEEDED and action.op == Op.UPLOAD and self._flight:
            # Each running upload reserves its whole size (D1): one at a time it may fit, so
            # it waits for them rather than telling the user they're out of space.
            result.retry[key] = self._backoff(key)
            self._transfers.waiting(key, _TRY_AGAIN)
        elif exc.code in _PROBLEM_REASONS or 400 <= exc.status < 500:
            reason = _PROBLEM_REASONS.get(exc.code, exc.message)
            if exc.request_id:
                reason = f"{reason} (reference {exc.request_id})"
            self._cant_sync(key, path, reason)
        else:
            result.retry[key] = self._backoff(key)

    def _refused(self, action: Action, exc: ApiError, result: CycleResult) -> None:
        """The server refused the file, or a move or rename of it, for good: remember it as it
        is now, so it waits until it changes (a refused move is held, the server keeping the
        item where it was; OQ-59 (1)). Its local copy is never touched (OQ-57 (1))."""
        item = action.local
        reason = exc.message
        if exc.code == ErrorCode.MALWARE_DETECTED:
            reason = malware_reason(_detection(exc))
        if exc.request_id:
            reason = f"{reason} (reference {exc.request_id})"
        if item is None:
            self._cant_sync(action.key, action.path, reason)
            return
        key = paths.key_of(item.path)
        if action.op == Op.MOVE_REMOTE and action.base is not None:
            # A rename or move the server refused: held, the server keeping the item where it
            # was, until the user renames it back (OQ-59 (1)). Never retried as is.
            old = action.base
            reason = (
                f"{reason} The server keeps it as “{old.path}”: rename it back to sync it again."
            )
            self._index.set_held_move(HeldMove(old.key, key, old.node_id, reason))
            self._refused_now.add(action.key)
            self._cant_sync(key, item.path, reason)
            result.again = True
            return
        self._index.set_refusal(Refusal(key, item.path, item.size, item.mtime_ns, reason))
        self._refused_now.add(action.key)
        self._cant_sync(key, item.path, reason)
        result.again = True  # the next cycle holds it, now that it is refused

    def _in_view_only(self, action: Action, tree: RemoteTree) -> bool:
        """Whether the action's item is (or would be) in a view-only shared folder. Any other
        403 is listed in Can't sync instead, so it can never loop."""
        if action.remote is not None:
            return self.view_only(action.remote.node.namespace_id)
        parent = tree.node(tree.node_at(paths.parent_of(action.key)) or tree.root_id)
        if parent is None:
            return False
        namespace = parent.mount_namespace_id or parent.namespace_id
        return self.view_only(namespace)

    def _undo_refused_move(self, action: Action, result: CycleResult) -> None:
        """The server refused a move: in a view-only folder (FR-SHARE-05), or out of someone
        else's shared folder (OQ-35). The original stays where it is on the server and comes
        back here; what the user moved stays where they put it and is synced from there as a
        new item: a copy in their own folder, or, in a view-only one, their conflicted copy."""
        result.again = True
        result.retry[action.key] = 0.0
        if action.base is not None:
            self._index.remove_entries(action.base.key, subtree=True)
            result.retry[action.base.key] = 0.0
        self._index.remove_entries(action.key, subtree=True)
        self._log.activity("move_refused", action.path)

    def _undo_viewer_change(self, action: Action, result: CycleResult) -> None:
        """A change in a view-only shared folder isn't uploaded (FR-SHARE-05): an edited or
        new item moves to the top of the user's own folder as a conflicted copy, and the
        shared folder goes back to how it is on the server. Forgetting the base makes its
        items new here, so they download again (nothing on the server changed)."""
        result.again = True
        result.retry[action.key] = 0.0
        if action.base is not None:
            self._index.remove_entries(action.base.key, subtree=True)
        self._index.remove_entries(action.key, subtree=True)
        if action.op == Op.DELETE_REMOTE or action.local is None:
            self._log.activity("restored_view_only", action.path)
            return
        source = action.local.path
        name = paths.conflict_name(
            paths.name_of(source),
            self._display_name,
            datetime.now(UTC),
            lambda candidate: self._scanner.disk_path(candidate).exists(),
        )
        target = self._scanner.disk_path(name)
        try:
            rename_no_replace(self._scanner.disk_path(source), target)
        except FileNotFoundError:
            return  # gone meanwhile: nothing to keep
        except OSError as exc:  # open in another program: try again shortly
            logger.info("Can't move %s out of a view-only folder yet: %s", source, exc)
            result.retry[action.key] = self._backoff(action.key)
            return
        self._writable(paths.key_of(name))
        result.retry[paths.key_of(name)] = 0.0
        self._log.conflict(source, name)

    def _writable(self, key: str) -> None:
        """Clear read-only on the files at and below `key`. Through the scanner, which never
        follows links or junctions out of the sync folder."""
        if self._scanner.resolve(key) is None:
            return
        for item in self._scanner.scan(key).present():
            if item.kind == NodeKind.FILE:
                self._file_attributes.set_read_only(self._scanner.disk_path(item.path), False)

    def _cant_sync(self, key: str, path: str, reason: str) -> None:
        """Listed for the user (FR-SYNC-07), not retried until the item changes."""
        logger.info("Can't sync %s: %s", path, reason)
        self._index.set_problem(Problem(key, path, reason))
        self._transfers.failed(key, reason)
        self._log.activity("cant_sync", path, reason)

    def _backoff(self, key: str) -> float:
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt
        return min(2.0 * float(2 ** min(attempt - 1, 6)), MAX_RETRY_DELAY)

    def _require_connected(self) -> tuple[NamespaceCursor, RemoteTree]:
        if self._namespace is None or self._tree is None:
            raise RuntimeError("connect() first")
        return self._namespace, self._tree


def _detection(exc: ApiError) -> str:
    """The scanner's name for what it found (MalwareDetectedDetails); "" if not given."""
    try:
        return MalwareDetectedDetails.model_validate(exc.details).detection
    except ValidationError:
        return ""


def _touches(keys: set[str], key: str) -> bool:
    """Some key in `keys` is `key`, above it, or below it."""
    return any(paths.is_within(key, k) or paths.is_within(k, key) for k in keys)


def _partners(dirty: set[str], held_moves: list[HeldMove], tree: RemoteTree) -> set[str]:
    """Every end of a held move a cycle over `dirty` would look at: its old key, its held
    place here, and where the server has the item now (renamed there meanwhile)."""
    found: set[str] = set()
    for held in held_moves:
        ends = {held.old_key, held.target_key}
        server_key = _key_on_server(tree, held.node_id)
        if server_key is not None:
            ends.add(server_key)
        if any(_touches(dirty, end) for end in ends):
            found |= ends
    return found


def _key_on_server(tree: RemoteTree, node_id: uuid.UUID) -> str | None:
    """Where the server has the node now, as a key; None if it is gone."""
    if tree.node(node_id) is None:
        return None
    path = tree.path_of(node_id)
    return None if path is None else paths.key_of(path)


def _is_refused(key: str, refused: set[str]) -> bool:
    return any(paths.is_within(key, r) for r in refused)


def _covered(key: str, collected: set[str]) -> bool:
    return any(paths.is_within(key, root) for root in collected)


def _delete_keys(plan: Plan) -> frozenset[str]:
    return frozenset(a.key for a in plan.actions if a.op in (Op.DELETE_LOCAL, Op.DELETE_REMOTE))


def _transfer_size(action: Action) -> int:
    """Bytes to move: small files go first (FR-XFER-03)."""
    if action.op == Op.UPLOAD and action.local is not None:
        return action.local.size
    if action.remote is not None:
        return action.remote.node.size
    return 0


def _settle(transfer: Transfer, future: concurrent.futures.Future[object]) -> None:
    """On the engine's thread: record a finished transfer, or tidy up after a failed one and
    raise its error for the usual handling."""
    try:
        value = future.result()
    except BaseException as exc:
        transfer.failed(exc)
        raise
    transfer.finish(value)


def _transfers_allowed(count: int) -> int:
    return min(max(count, 1), MAX_TRANSFERS_LIMIT)
