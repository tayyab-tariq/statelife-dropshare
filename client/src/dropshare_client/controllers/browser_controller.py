"""The file browser's data (FR-FILE-01): where the user is, that folder's items page by page
from the server, and each item's sync state from the local index and live transfers.

Every load runs on the worker pool; results that arrive after the user moved on are dropped.
Where an item is on this computer always comes from the index, by node ID, so a folder
renamed elsewhere while it is open doesn't leave stale paths behind (OQ-23).
"""

import logging
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.file_actions_controller import Item
from dropshare_client.controllers.transfers_controller import TransfersController
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.errors import ApiError
from dropshare_client.sync import paths
from dropshare_client.sync.status import Synced, synced
from dropshare_client.sync.transfers import Direction, TransferEvent, TransferStatus
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import PAGE_LIMIT_MAX, ListResponse
from dropshare_shared.schemas.nodes import NamespaceKind, NamespaceOut, NodeKind, NodeOut

logger = logging.getLogger(__name__)

PAGE_SIZE = PAGE_LIMIT_MAX


class Files(Protocol):
    """What the browser needs from the server (services.files.RemoteFiles)."""

    def namespaces(self) -> list[NamespaceOut]: ...

    def children(
        self, folder_id: uuid.UUID, page: int, limit: int = ...
    ) -> ListResponse[NodeOut]: ...


class SyncState(StrEnum):
    """The browser's sync column, in words (never by colour alone)."""

    SYNCED = "Synced"
    UPLOADING = "Uploading"
    DOWNLOADING = "Downloading"
    QUEUED = "Waiting to sync"
    WAITING = "Waiting"
    FAILED = "Can't sync"
    NOT_SYNCED = "Not on this computer yet"


@dataclass(frozen=True)
class Crumb:
    node_id: uuid.UUID
    name: str  # as the server named it when it was opened; the title only


@dataclass(frozen=True)
class BrowserRow:
    node: NodeOut
    local_path: str | None  # where it is in the sync folder; None while not here (OQ-28)
    key: str  # matches the sync engine's key for this item (transfers)
    state: SyncState
    modified_by_me: bool
    synced_hash: str | None = None  # the content last synced, for the state

    @property
    def is_folder(self) -> bool:
        return self.node.kind == NodeKind.FOLDER

    @property
    def is_known_kind(self) -> bool:
        """A kind a newer server added is shown, but nothing is offered on it."""
        return isinstance(self.node.kind, NodeKind)

    @property
    def item(self) -> Item | None:
        """For an action (FileActionsController); None while it isn't on this computer."""
        if self.local_path is None or not self.is_known_kind:
            return None
        return Item(self.local_path, self.node.id)


@dataclass(frozen=True)
class _Page:
    nodes: list[NodeOut]
    total: int
    entries: dict[uuid.UUID, Synced]
    folder: Synced | None  # the folder shown, as on this computer


class BrowserController(QObject):
    location_changed = Signal()  # read crumbs()
    loading = Signal(bool)  # True: keep what is shown while it reloads (a refresh)
    loaded = Signal()  # rows were replaced; read rows()
    appended = Signal(int, int)  # first new row, how many
    states_changed = Signal(list)  # row numbers whose sync state changed
    failed = Signal(str)
    notice = Signal(str)  # something the user should know, not an error (folder trashed)

    def __init__(
        self,
        files: Files,
        index_path: Path,
        transfers: TransfersController,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._files = files
        self._index_path = index_path
        self._transfers = transfers
        self._worker = worker or Worker()
        self._user_id: uuid.UUID | None = None
        self._crumbs: list[Crumb] = []
        self._folder: Synced | None = None  # the folder shown, on this computer
        self._rows: list[BrowserRow] = []
        self._total = 0
        self._busy = False
        self._generation = 0  # results for an older location are dropped
        self._reloading_entries = False
        transfers.rows_changed.connect(self._restate)

    # --- state ---------------------------------------------------------------------------

    def crumbs(self) -> list[Crumb]:
        return list(self._crumbs)

    def rows(self) -> list[BrowserRow]:
        return list(self._rows)

    @property
    def can_load_more(self) -> bool:
        return bool(self._crumbs) and not self._busy and len(self._rows) < self._total

    @property
    def current_folder(self) -> Item | None:
        """The folder shown, for New folder and uploads; None while it isn't on this
        computer. The top of My files is the sync folder itself (path "")."""
        if len(self._crumbs) == 1:
            return Item("", self._crumbs[0].node_id)
        if self._folder is None or not self._crumbs:
            return None
        return Item(self._folder.path, self._crumbs[-1].node_id)

    @property
    def current_local_path(self) -> str | None:
        folder = self.current_folder
        return None if folder is None else folder.path

    # --- actions -------------------------------------------------------------------------

    def start(self, user_id: uuid.UUID) -> None:
        """Signed in: show the top of My files."""
        self._user_id = user_id
        self._go([], keep=False)
        self._generation += 1
        generation = self._generation
        self._busy = True
        self._worker.submit(
            self._personal_root,
            lambda root: self._root_found(generation, root),
            lambda exc: self._failed(generation, exc),
        )

    def clear(self) -> None:
        """Signed out: nothing of that account stays on screen."""
        self._user_id = None
        self._generation += 1
        self._crumbs, self._folder, self._busy = [], None, False
        self._rows, self._total = [], 0
        self.location_changed.emit()
        self.loaded.emit()

    def open(self, row: BrowserRow) -> None:
        if row.is_folder and row.node.name is not None:
            self._go([*self._crumbs, Crumb(row.node.id, row.node.name)])

    def open_crumb(self, index: int) -> None:
        if 0 <= index < len(self._crumbs) - 1:
            self._go(self._crumbs[: index + 1])

    def go_up(self) -> None:
        self.open_crumb(len(self._crumbs) - 2)

    def refresh(self) -> None:
        """Load the current folder again, as many pages as are shown now, keeping them on
        screen until the answer comes (a refresh never shortens a scrolled list)."""
        if self._crumbs:
            pages = max(1, -(-len(self._rows) // PAGE_SIZE))
            self.loading.emit(True)
            self._load(page=1, replace_rows=True, pages=pages)
        elif self._user_id is not None:
            self.start(self._user_id)

    def load_more(self) -> None:
        if self.can_load_more:
            self._load(page=len(self._rows) // PAGE_SIZE + 1, replace_rows=False)

    # --- loading -------------------------------------------------------------------------

    def _go(self, crumbs: list[Crumb], *, keep: bool = False) -> None:
        """Somewhere else: nothing of the previous folder stays on screen meanwhile."""
        self._crumbs, self._folder = crumbs, None
        self._rows, self._total = [], 0
        self.location_changed.emit()
        self.loaded.emit()
        self.loading.emit(keep)
        if crumbs:
            self._load(page=1, replace_rows=True)

    def _personal_root(self) -> uuid.UUID:
        for namespace in self._files.namespaces():
            if namespace.kind == NamespaceKind.PERSONAL:
                return namespace.root_node_id
        raise LookupError("no personal folder")

    def _root_found(self, generation: int, root: uuid.UUID) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self._go([Crumb(root, messages.MY_FILES)])

    def _load(self, *, page: int, replace_rows: bool, pages: int = 1) -> None:
        self._generation += 1
        generation = self._generation
        self._busy = True
        folder = self._crumbs[-1].node_id
        self._worker.submit(
            lambda: self._fetch(folder, page, pages),
            lambda result: self._fetched(generation, result, replace_rows),
            lambda exc: self._failed(generation, exc),
        )

    def _fetch(self, folder: uuid.UUID, first: int, pages: int) -> _Page:
        """On the pool: `pages` pages from `first` on (duplicates dropped: the folder can
        change between pages), and what the index knows of them and of the folder."""
        nodes: dict[uuid.UUID, NodeOut] = {}
        total = 0
        for page in range(first, first + pages):
            answer = self._files.children(folder, page, PAGE_SIZE)
            nodes.update((node.id, node) for node in answer.data)
            total = answer.meta.total
            if (first - 1) * PAGE_SIZE + len(nodes) >= total or not answer.data:
                break
        entries = synced(self._index_path, [*nodes, folder])
        return _Page(list(nodes.values()), total, entries, entries.pop(folder, None))

    def _fetched(self, generation: int, page: _Page, replace_rows: bool) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self._total = page.total
        self._folder = page.folder
        latest = self._transfers.latest_by_key()
        shown = set() if replace_rows else {row.node.id for row in self._rows}
        new = [
            self._row(node, page.entries.get(node.id), latest)
            for node in page.nodes
            if node.id not in shown
        ]
        if replace_rows:
            self._rows = new
            self.loaded.emit()
        else:
            first = len(self._rows)
            self._rows.extend(new)
            self.appended.emit(first, len(new))

    def _failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        self._busy = False
        if isinstance(exc, LookupError):
            self.failed.emit(messages.NO_PERSONAL_FOLDER)
            return
        if isinstance(exc, ApiError) and exc.code == ErrorCode.NOT_FOUND and len(self._crumbs) > 1:
            # The open folder went to the trash (or moved away) elsewhere: step up, then say
            # why (going somewhere clears the status line).
            self._go(self._crumbs[:-1])
            self.notice.emit(messages.FOLDER_TRASHED)
            return
        logger.info("Couldn't load the folder: %s", exc)
        self.failed.emit(messages.message_for(exc))

    # --- sync states ---------------------------------------------------------------------

    def _row(
        self, node: NodeOut, entry: Synced | None, latest: dict[str, TransferEvent]
    ) -> BrowserRow:
        if entry is not None:
            key = paths.key_of(entry.path)
        else:  # not here yet: where the engine will put it (its transfer's key)
            parent = self._folder.path if self._folder is not None else ""
            key = paths.key_of(paths.join(parent, node.name or ""))
        row = BrowserRow(
            node,
            entry.path if entry is not None else None,
            key,
            SyncState.NOT_SYNCED,
            node.modified_by_user_id == self._user_id,
            entry.sha256 if entry is not None else None,
        )
        return replace(row, state=_state(row, latest.get(key)))

    def _restate(self) -> None:
        """Transfers moved on: new states for the rows that changed, and a fresh index entry
        for files that just finished downloading (their actions turn on)."""
        if not self._rows:
            return
        latest = self._transfers.latest_by_key()
        changed: list[int] = []
        arrived: list[uuid.UUID] = []
        for number, row in enumerate(self._rows):
            state = _state(row, latest.get(row.key))
            if state is not row.state:
                self._rows[number] = replace(row, state=state)
                changed.append(number)
            if state is SyncState.SYNCED and row.local_path is None:
                arrived.append(row.node.id)
        if changed:
            self.states_changed.emit(changed)
        if arrived and not self._reloading_entries:
            self._reload_entries(arrived)

    def _reload_entries(self, node_ids: list[uuid.UUID]) -> None:
        self._reloading_entries = True
        generation = self._generation

        def done(entries: dict[uuid.UUID, Synced]) -> None:
            self._reloading_entries = False
            if generation != self._generation:
                return
            changed = []
            for number, row in enumerate(self._rows):
                entry = entries.get(row.node.id)
                if entry is not None and row.local_path is None:
                    self._rows[number] = replace(
                        row, local_path=entry.path, synced_hash=entry.sha256
                    )
                    changed.append(number)
            if changed:
                self.states_changed.emit(changed)

        def failed(exc: Exception) -> None:
            self._reloading_entries = False
            logger.info("Couldn't re-read the sync index: %s", exc)

        self._worker.submit(lambda: synced(self._index_path, node_ids), done, failed)


def _state(row: BrowserRow, transfer: TransferEvent | None) -> SyncState:
    if transfer is not None:
        match transfer.status:
            case TransferStatus.ACTIVE if transfer.direction is Direction.UPLOAD:
                return SyncState.UPLOADING
            case TransferStatus.ACTIVE:
                return SyncState.DOWNLOADING
            case TransferStatus.WAITING:
                return SyncState.WAITING
            case TransferStatus.QUEUED:
                return SyncState.QUEUED
            case TransferStatus.FAILED:
                return SyncState.FAILED
            # By size: transfer events carry no hash. A same-size newer version reads as
            # synced until the next refresh, which then compares hashes.
            case TransferStatus.DONE if transfer.total == row.node.size_bytes:
                return SyncState.SYNCED
    if row.local_path is None:
        return SyncState.NOT_SYNCED
    if row.is_folder or row.synced_hash == row.node.content_hash:
        return SyncState.SYNCED
    return SyncState.QUEUED
