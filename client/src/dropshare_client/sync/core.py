"""One sync cycle, start to finish, on the calling thread (algorithm.md §1).

fetch remote changes → snapshot the affected paths on disk, on the server and in the index →
hash what changed → reconcile → check for a mass delete → execute → report what to retry.
SyncEngine runs this on its own thread; tests call it directly.

The user's own namespace is followed, and each shared folder they added (FR-SHARE-02): a mount
appearing in their tree starts its namespace, a mount going away lets it go (sync/mounts.py).
"""

import dataclasses
import functools
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import FileAttributes, FileIdentity, RecycleBin
from dropshare_client.services.errors import (
    ApiError,
    ContentMismatchError,
    OfflineError,
    RemoteContentChangedError,
    SessionExpiredError,
    SourceChangedError,
    TransferCancelledError,
    UpgradeRequiredError,
)
from dropshare_client.store.index import Index
from dropshare_client.store.records import Entry, JournalItem, NamespaceCursor, Problem
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
from dropshare_client.sync.executor import ActivityLog, Executor, no_crash
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
from dropshare_client.sync.transfers import Direction, TransferEvent, TransferReporter
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.nodes import AccessRole, NamespaceKind, NamespaceOut, NodeKind
from dropshare_shared.schemas.sync import ChangeType

logger = logging.getLogger(__name__)

# Seconds before looking again at an item that is still being written (FR-SYNC-06).
STILL_WRITING_DELAY = 2.0
# Locked or failing items: 2 s, 4 s, ... up to this (windows-fs.md "Locked and changing").
MAX_RETRY_DELAY = 60.0
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
    ) -> None:
        self._index = index
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
                self._recover_upload(item.key, item.data.get("sha256"), tree)
            else:
                logger.info(
                    "Journal: %s of %s was interrupted; the next cycle checks it",
                    item.kind,
                    item.key,
                )
            self._index.finish(item.id)

    def _recover_upload(self, key: str, sha256: str | None, tree: RemoteTree) -> None:
        remote = tree.items_under(key).get(key)
        if remote is None or sha256 is None or remote.node.sha256 != sha256:
            return
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
        released, held, stuck = self._release_mounts(result)
        dirty = dirty | released
        self._mark_read_only()
        local: dict[str, LocalItem] = {}
        remote: dict[str, RemoteItem] = {}
        base: dict[str, Entry] = {}
        deferred: set[str] = set()
        roots = paths.outermost(dirty)
        while roots:
            for root in roots:
                self._collect(root, tree, local, remote, base, deferred, result)
            roots = self._moved_out_of_view(roots, dirty, tree, remote, base)
            dirty = dirty | roots
        self._scanner.require_root()  # a vanished root must never read as "all deleted"
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
        self._identify_new(local, base)
        plan = reconcile(local.keys() | remote.keys() | base.keys(), local, remote, base, deferred)
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
            for item in self._scanner.scan(entry.key).items.values():
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
        result: CycleResult,
    ) -> None:
        """Snapshot `root` and everything below it: on disk, on the server and in the index."""
        scan = self._scanner.scan(root)
        local.update(scan.items)
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
        base.update(self._index.entries_under(root))

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
        for done, action in enumerate(plan.actions):
            if self._stop.is_set():
                raise SyncStoppedError()
            self._progress(len(plan.actions) - done)
            done_ok, created = self._run(executor, action, tree, result)
            if not done_ok:
                if action.op in (Op.MOVE_LOCAL, Op.MOVE_REMOTE):
                    self._hold_move_together(action, result)
                continue
            self._attempts.pop(action.key, None)
            if created is not None:
                result.retry[created] = 0.0
            if action.op != Op.RECORD:
                result.changed_anything = True
        self._progress(0)

    def _run(
        self, executor: Executor, action: Action, tree: RemoteTree, result: CycleResult
    ) -> tuple[bool, str | None]:
        """(done, key of an item the step created). A failure that isn't fatal is recorded
        in `result` (what to retry and when) and reported as not done."""
        try:
            return True, executor.run(action)
        except _FATAL as exc:
            self._transfers.waiting(action.key, _stopped_because(exc))
            raise
        except TransferCancelledError as exc:
            self._transfers.waiting(action.key, _stopped_because(SyncStoppedError()))
            raise SyncStoppedError() from exc
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
        elif exc.code in _PROBLEM_REASONS or 400 <= exc.status < 500:
            reason = _PROBLEM_REASONS.get(exc.code, exc.message)
            if exc.request_id:
                reason = f"{reason} (reference {exc.request_id})"
            self._cant_sync(key, path, reason)
        else:
            result.retry[key] = self._backoff(key)

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
        for item in self._scanner.scan(key).items.values():
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


def _delete_keys(plan: Plan) -> frozenset[str]:
    return frozenset(a.key for a in plan.actions if a.op in (Op.DELETE_LOCAL, Op.DELETE_REMOTE))
