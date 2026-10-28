"""Carry out one planned action (algorithm.md §1, §6, §8; transfers.md).

Every step follows the same order: check the item is still what the plan saw, do the work on
the server or disk, then record it in the index. A crash between any two steps leaves the
content on disk, on the server or in a trash, and the next cycle's three-way comparison
finishes the job (tests/sync/test_crash_recovery.py). `crash_point` lets tests stop there.
"""

import contextlib
import dataclasses
import logging
import os
import stat
import threading
import unicodedata
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from dropshare_client.platform import rename_no_replace
from dropshare_client.platform.base import FileAttributes, RecycleBin
from dropshare_client.services.errors import ApiError, OfflineError
from dropshare_client.services.files import NewFile, NewVersion, SourceFile
from dropshare_client.store.index import Index
from dropshare_client.store.records import Entry, Problem, RemoteNode
from dropshare_client.sync import paths
from dropshare_client.sync.errors import (
    CantSyncError,
    LocalChangedError,
    RetryLaterError,
    UnsafePathError,
)
from dropshare_client.sync.reconciler import Action, Op
from dropshare_client.sync.remote_tree import RemoteTree, TreeDelta, from_node
from dropshare_client.sync.scanner import LocalItem, Scanner
from dropshare_client.sync.server import FileServer
from dropshare_client.sync.transfers import Direction, TransferReporter
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.error_details import NameConflictDetails
from dropshare_shared.schemas.nodes import NodeKind, NodeOut

logger = logging.getLogger(__name__)

_DELETED_DIR = "deleted"
_TOO_LONG = (
    "The full path would be too long for Windows. Rename it, or move it nearer the top of "
    "your DropShare folder, on another computer or in the browser."
)


class ActivityLog(Protocol):
    def activity(self, kind: str, path: str, detail: str = "") -> None: ...

    def conflict(self, path: str, copy_path: str) -> None: ...


def no_crash(_: str) -> None:
    return None


class Executor:
    def __init__(
        self,
        scanner: Scanner,
        index: Index,
        server: FileServer,
        tree: RemoteTree,
        recycle_bin: RecycleBin,
        attributes: FileAttributes,
        log: ActivityLog,
        *,
        display_name: str,
        cursor: Callable[[], int],
        stop: threading.Event,
        crash_point: Callable[[str], None] = no_crash,
        transfers: TransferReporter | None = None,
    ) -> None:
        self._scanner = scanner
        self._index = index
        self._server = server
        self._tree = tree
        self._recycle_bin = recycle_bin
        self._attributes = attributes
        self._log = log
        self._display_name = display_name
        self._cursor = cursor
        self._stop = stop
        self._crash_point = crash_point
        self._transfers = transfers or TransferReporter(lambda _: None)

    def run(self, action: Action) -> str | None:
        """Carry out `action`. Returns the key of an item the step created that the next
        cycle must look at (a conflicted copy), if any."""
        if action.op == Op.CONFLICT:
            return self._conflict(action)
        handlers: dict[Op, Callable[[Action], None]] = {
            Op.MAKE_LOCAL_FOLDER: self._make_local_folder,
            Op.MAKE_REMOTE_FOLDER: self._make_remote_folder,
            Op.UPLOAD: self._upload,
            Op.DOWNLOAD: self._download,
            Op.RECORD: self._record,
            Op.DELETE_LOCAL: self._delete_local,
            Op.DELETE_REMOTE: self._delete_remote,
            Op.MOVE_LOCAL: self._move_local,
            Op.MOVE_REMOTE: self._move_remote,
        }
        handlers[action.op](action)
        return None

    @contextlib.contextmanager
    def _journaled(
        self,
        kind: str,
        key: str,
        data: dict[str, str],
        unsure: tuple[type[Exception], ...] = (),
    ) -> Iterator[int]:
        """A journal row for the step inside. A failure that shows the step didn't happen
        removes it again; `unsure` failures (no answer) and crashes keep it for recovery."""
        journal = self._index.begin(kind, key, data)
        try:
            yield journal
        except unsure:
            raise
        except Exception:
            self._index.finish(journal)
            raise

    # --- conflicts (FR-CONF-01) ------------------------------------------------------------

    def _conflict(self, action: Action) -> str:
        """Rename the local item out of the way. Its content then uploads under the new name
        and the server's version comes down under the original one, in the next cycle."""
        local = _required(action.local)
        parent = paths.parent_of(local.path)
        copy_name = paths.conflict_name(
            _nfc(paths.name_of(local.path)),
            self._display_name,
            datetime.now(UTC),
            lambda name: self._taken(parent, name),
        )
        copy_path = paths.join(parent, copy_name)
        source, target = self._disk(local.path), self._disk(copy_path)
        if target.exists():
            raise RetryLaterError("the conflicted-copy name was just taken")
        with self._journaled("conflict", action.key, {"copy": copy_path}) as journal:
            source.rename(target)  # PermissionError while another program has it open: retried
        self._crash_point("conflict.renamed")
        self._index.finish(journal)
        self._log.activity("conflict", local.path, copy_path)
        self._log.conflict(local.path, copy_path)
        return paths.key_of(_nfc(copy_path))

    def _taken(self, parent: str, name: str) -> bool:
        key = paths.key_of(paths.join(parent, _nfc(name)))
        try:
            on_disk = self._scanner.resolve(key) is not None
        except UnsafePathError:
            on_disk = True
        return on_disk or self._tree.node_at(key) is not None

    # --- folders -------------------------------------------------------------------------

    def _make_local_folder(self, action: Action) -> None:
        remote = _required(action.remote)
        path = paths.join(self._local_parent(action.key), paths.name_of(remote.path))
        if self._scanner.too_long(path, folder=True):
            raise CantSyncError(_TOO_LONG)
        disk = self._disk(path)
        try:
            disk.mkdir()
        except FileExistsError:
            if not disk.is_dir():
                raise LocalChangedError(action.key) from None
            path = self._scanner.resolve(action.key) or path  # created meanwhile: link it
        self._crash_point("make_local_folder.created")
        self._index.put_entry(
            _entry(
                action.key,
                path,
                remote.node,
                disk.stat().st_mtime_ns,
                self._scanner.file_id(path),
            )
        )

    def _make_remote_folder(self, action: Action) -> None:
        local = _required(action.local)
        parent_id = self._remote_parent(action.key)
        try:
            node = self._server.create_folder(parent_id, _nfc(paths.name_of(local.path)))
        except ApiError as exc:
            if exc.code != ErrorCode.NAME_CONFLICT:
                raise
            node = self._existing_folder(exc)  # a retry, or made elsewhere too
        self._crash_point("make_remote_folder.created")
        remembered = self._remember(node)
        self._index.put_entry(
            _entry(action.key, local.path, remembered, local.mtime_ns, local.file_id)
        )
        self._log.activity("folder_added", local.path)

    def _existing_folder(self, exc: ApiError) -> NodeOut:
        try:
            details = NameConflictDetails.model_validate(exc.details) if exc.details else None
        except ValidationError:
            details = None
        if details is None or details.kind != NodeKind.FOLDER:
            # A file has the name (a conflict next cycle), or the server didn't say what.
            raise RetryLaterError("the name is taken on the server") from exc
        return self._server.get_node(details.node_id)

    # --- transfers -----------------------------------------------------------------------

    def _upload(self, action: Action) -> None:
        local = _required(action.local)
        if local.sha256 is None:
            raise LocalChangedError(action.key)
        remote = action.remote
        target: NewFile | NewVersion
        if remote is not None and remote.node.kind == NodeKind.FILE:
            target = NewVersion(remote.node.node_id, remote.node.revision)
        else:
            target = NewFile(self._remote_parent(action.key), _nfc(paths.name_of(local.path)))
        source = SourceFile(self._disk(local.path), local.sha256, local.size, local.mtime_ns)
        self._transfers.started(action.key, local.path, Direction.UPLOAD, local.size)
        # Kept when no answer came: the upload may have completed (SyncCore.recover).
        with self._journaled(
            "upload", action.key, {"sha256": local.sha256}, unsure=(OfflineError,)
        ) as journal:
            node = self._server.upload(
                source, target, self._stop, partial(self._transfers.progress, action.key)
            )
        self._crash_point("upload.completed")
        remembered = self._remember(node)
        self._index.put_entry(
            Entry(
                key=action.key,
                path=local.path,
                node_id=remembered.node_id,
                kind=NodeKind.FILE,
                revision=remembered.revision,
                sha256=remembered.sha256,
                size=local.size,
                mtime_ns=local.mtime_ns,
                file_id=local.file_id,
            )
        )
        self._index.finish(journal)
        self._transfers.finished(action.key)
        self._log.activity("uploaded", local.path)

    def _download(self, action: Action) -> None:
        """Into a temp file, verified, then moved over the target in one step (FR-SYNC-06)."""
        remote = _required(action.remote)
        node = remote.node
        if node.sha256 is None:
            raise RetryLaterError("the server didn't give the file's hash")
        if action.local is not None:
            path = action.local.path  # keep the local spelling of an existing file
        else:
            path = paths.join(self._local_parent(action.key), paths.name_of(remote.path))
        if self._scanner.too_long(path):
            raise CantSyncError(_TOO_LONG)
        # Named by node only: a newer version replaces a stale partial download.
        part = self._temp_dir() / f"{node.node_id}.part"
        self._transfers.started(action.key, path, Direction.DOWNLOAD, node.size)
        self._server.download(
            node.node_id,
            node.sha256,
            node.size,
            part,
            self._stop,
            partial(self._transfers.progress, action.key),
        )
        self._crash_point("download.fetched")
        target = self._disk(path)
        with self._journaled("download", action.key, {"sha256": node.sha256}) as journal:
            self._expect(target, action.local)
            part.replace(target)  # PermissionError while the file is open elsewhere: retried
        self._crash_point("download.replaced")
        modified_ns = int(node.modified_at.timestamp() * 1_000_000_000)
        os.utime(target, ns=(modified_ns, modified_ns))
        info = target.stat()
        self._index.put_entry(
            Entry(
                key=action.key,
                path=path,
                node_id=node.node_id,
                kind=NodeKind.FILE,
                revision=node.revision,
                sha256=node.sha256,
                size=info.st_size,
                mtime_ns=info.st_mtime_ns,
                file_id=self._scanner.file_id(path),
            )
        )
        self._index.finish(journal)
        self._transfers.finished(action.key)
        self._log.activity("downloaded", path)

    # --- renames and moves (FR-SYNC-10) ---------------------------------------------------

    def _move_remote(self, action: Action) -> None:
        """Renamed or moved here: one PATCH, so the node keeps its ID and history. Everything
        inside a folder moves with it. A crash or lost answer is safe: the next cycle finds the
        item moved on both sides to the same place and only records it."""
        local, node = _required(action.local), _required(action.remote).node
        entry = _required(action.base)
        parent_id = self._remote_parent(action.key)
        name = _nfc(paths.name_of(local.path))
        new_parent = parent_id if parent_id != node.parent_id else None
        new_name = name if name != node.name else None
        if new_parent is None and new_name is None:
            # Already where it is here (the server's spelling matches): only the base moves.
            self._index.move_entries(entry, action.key, local.path)
            return
        with self._journaled(
            "move_remote", action.key, {"from": entry.key}, unsure=(OfflineError,)
        ) as journal:
            moved = self._server.move(
                node.node_id, node.revision, parent_id=new_parent, name=new_name
            )
        self._crash_point("move_remote.done")
        remembered = self._remember(moved)
        self._index.move_entries(
            dataclasses.replace(entry, revision=remembered.revision), action.key, local.path
        )
        self._index.finish(journal)
        self._log.activity("moved_on_server", local.path, entry.path)

    def _move_local(self, action: Action) -> None:
        """Renamed or moved on the server: the same here, never over an existing item. A
        local edit to the item stays and uploads once its base has moved too."""
        local, remote, entry = (
            _required(action.local),
            _required(action.remote),
            _required(action.base),
        )
        folder = local.kind == NodeKind.FOLDER
        target_path = paths.join(self._local_parent(action.key), paths.name_of(remote.path))
        if self._scanner.too_long(target_path, folder=folder):
            raise CantSyncError(_TOO_LONG)
        unchanged = folder or remote.node.sha256 == entry.sha256
        moved = dataclasses.replace(
            entry, revision=remote.node.revision if unchanged else entry.revision
        )
        if target_path == local.path:  # already in place (moved the same way on both sides)
            self._index.move_entries(moved, action.key, target_path)
            return
        source, target = self._disk(local.path), self._disk(target_path)
        if folder:
            if not source.is_dir() or source.is_symlink():
                raise LocalChangedError(action.key)
        else:
            self._expect(source, local)
        with self._journaled("move_local", action.key, {"from": local.path}) as journal:
            try:
                rename_no_replace(source, target)
            except FileExistsError:
                raise LocalChangedError(action.key) from None
            except PermissionError:
                # Windows reports a folder that appeared at a file's target as access
                # denied, not as "exists": that is a change to plan again, not a lock.
                if os.path.lexists(target):
                    raise LocalChangedError(action.key) from None
                raise
        self._crash_point("move_local.done")
        self._index.move_entries(moved, action.key, target_path)
        self._index.finish(journal)
        self._log.activity("moved_here", target_path, entry.path)

    # --- index only ----------------------------------------------------------------------

    def _record(self, action: Action) -> None:
        local, remote = action.local, action.remote
        if local is None or remote is None:
            self._index.remove_entries(action.key, subtree=False)
            return
        node = remote.node
        file_id = local.file_id or (action.base.file_id if action.base else None)
        self._index.put_entry(
            Entry(
                key=action.key,
                path=local.path,
                node_id=node.node_id,
                kind=node.kind,
                revision=node.revision,
                sha256=node.sha256,
                size=local.size,
                mtime_ns=local.mtime_ns,
                file_id=file_id,
            )
        )

    # --- deletes -------------------------------------------------------------------------

    def _delete_local(self, action: Action) -> None:
        """To the Recycle Bin (FR-SYNC-09), only if nothing changed since the plan."""
        local = _required(action.local)
        if local.kind == NodeKind.FILE:
            self._expect(self._disk(local.path), local)
        else:
            self._expect_folder_unchanged(action.key)
        with self._journaled("delete_local", action.key, {"path": local.path}) as journal:
            self._to_recycle_bin(action.key, local.path)
        self._crash_point("delete_local.done")
        self._index.remove_entries(action.key, subtree=True)
        self._index.finish(journal)
        self._log.activity("deleted_here", local.path)

    def _delete_remote(self, action: Action) -> None:
        """To the server trash. The cursor makes the server refuse if anything inside the
        folder changed since we last looked, so another device's new file isn't taken along
        (FR-CONF-03)."""
        node = _required(action.remote).node
        try:
            self._server.trash(node.node_id, node.revision, self._cursor())
        except ApiError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise  # REVISION_CONFLICT: changed there, so the edit wins (SyncCore replans)
            logger.info("%s was already gone from the server", action.path)
        self._crash_point("delete_remote.done")
        delta = TreeDelta()
        self._tree.remove(node.node_id, delta)
        self._index.save_remote([], delta.removed)
        self._index.remove_entries(action.key, subtree=True)
        self._log.activity("deleted_on_server", action.path)

    def _to_recycle_bin(self, key: str, path: str) -> None:
        disk = self._disk(path)
        try:
            self._recycle_bin.move_to_recycle_bin(disk)
            return
        except OSError:
            logger.warning("Couldn't move %s to the Recycle Bin", path, exc_info=True)
        # Never delete outright: keep it in the hidden folder and say where (algorithm.md §8).
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        holder = self._temp_dir() / _DELETED_DIR / stamp
        holder.mkdir(parents=True)
        disk.replace(holder / disk.name)
        self._index.set_problem(
            Problem(
                key,
                path,
                f"It was deleted on the server but couldn't go to the Recycle Bin, so it was "
                f"kept in {holder}.",
            )
        )
        self._log.activity("kept_instead_of_recycled", path, str(holder))

    # --- checks and helpers ----------------------------------------------------------------

    def _expect(self, target: Path, local: LocalItem | None) -> None:
        """The target is still absent, or still the file the plan was made from."""
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if local is None:
            if info is not None:
                raise LocalChangedError(str(target))
            return
        if (
            info is None
            or not stat.S_ISREG(info.st_mode)
            or (info.st_size, info.st_mtime_ns) != (local.size, local.mtime_ns)
        ):
            raise LocalChangedError(str(target))

    def _expect_folder_unchanged(self, key: str) -> None:
        """Everything in the folder is as last synced, so recycling it loses no edit."""
        scan = self._scanner.scan(key)
        base = self._index.entries_under(key)
        if scan.deferred or scan.problems:
            raise LocalChangedError(key)
        for item_key, item in scan.items.items():
            entry = base.get(item_key)
            if entry is None or entry.kind != item.kind:
                raise LocalChangedError(item_key)
            if item.kind == NodeKind.FILE and (item.size, item.mtime_ns) != (
                entry.size,
                entry.mtime_ns,
            ):
                raise LocalChangedError(item_key)

    def _local_parent(self, key: str) -> str:
        parent = paths.parent_of(key)
        if not parent:
            return ""
        path = self._scanner.resolve(parent)
        if path is None:
            raise RetryLaterError("the parent folder isn't here yet", key=parent)
        return path

    def _remote_parent(self, key: str) -> uuid.UUID:
        parent = paths.parent_of(key)
        if not parent:
            return self._tree.root_id
        entry = self._index.entry(parent)
        if entry is None or entry.kind != NodeKind.FOLDER:
            raise RetryLaterError("the parent folder isn't synced yet", key=parent)
        return entry.node_id

    def _remember(self, node: NodeOut) -> RemoteNode:
        """Put our own write in the remote tree now; the journal brings the same state later."""
        remote = from_node(node)
        if remote is None:
            raise RetryLaterError("the server answered with an item of an unknown kind")
        delta = TreeDelta()
        self._tree.upsert(remote, delta)
        self._index.save_remote(delta.upserts.values(), delta.removed)
        return remote

    def _temp_dir(self) -> Path:
        temp = self._disk(paths.TEMP_DIR)
        if not temp.is_dir():
            temp.mkdir()
            self._attributes.hide(temp)
        return temp

    def _disk(self, path: str) -> Path:
        return self._scanner.disk_path(path)


def _entry(key: str, path: str, node: RemoteNode, mtime_ns: int, file_id: str | None) -> Entry:
    return Entry(
        key=key,
        path=path,
        node_id=node.node_id,
        kind=node.kind,
        revision=node.revision,
        sha256=node.sha256,
        size=node.size if node.kind == NodeKind.FILE else 0,
        mtime_ns=mtime_ns,
        file_id=file_id,
    )


def _required[T](item: T | None) -> T:
    """The reconciler always passes the items an op needs; a missing one is a bug."""
    if item is None:
        raise RuntimeError("The plan is missing an item for this action")
    return item


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)
