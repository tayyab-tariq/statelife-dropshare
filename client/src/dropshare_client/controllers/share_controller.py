"""The Share dialog's work (FR-SHARE-01/03/04/07/08): who has access to an item, finding people
and groups as the user types, adding, changing and removing them, and the item's link. Every
request runs on the pool; answers that arrive after the dialog moved on are dropped."""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, QTimer, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.links import link_to, node_of
from dropshare_client.services.sharing import Sharing
from dropshare_shared.schemas.sharing import (
    SEARCH_MAX_LENGTH,
    SEARCH_MIN_LENGTH,
    GranteeKind,
    MemberOut,
    ShareRole,
    SharingOut,
)

logger = logging.getLogger(__name__)

SEARCH_DELAY_MS = 300  # wait for a pause in typing before asking the server

# Re-exported so ui/ needn't import the shared contract (architecture §1).
__all__ = [
    "SEARCH_DELAY_MS",
    "SEARCH_MIN_LENGTH",
    "Grantee",
    "GranteeKind",
    "MemberOut",
    "ShareController",
    "ShareRole",
    "SharingOut",
    "node_of",
]


@dataclass(frozen=True)
class Grantee:
    """A person or group found by the search."""

    kind: GranteeKind
    id: uuid.UUID
    name: str
    detail: str  # the email, or how many people the group has


class ShareController(QObject):
    loading = Signal(bool)  # True: keep the list shown (a reload after a change)
    loaded = Signal(object)  # SharingOut
    failed = Signal(str)  # loading failed: the dialog shows it with Retry
    found = Signal(list)  # list[Grantee], for the latest search text
    searching = Signal(bool)
    busy_changed = Signal(bool)  # a change is being sent
    changed = Signal(str)  # a confirmation for the status line
    change_failed = Signal(str)

    def __init__(self, sharing: Sharing, worker: Worker | None = None) -> None:
        super().__init__()
        self._sharing = sharing
        self._worker = worker or Worker()
        self._node_id: uuid.UUID | None = None
        self._target: uuid.UUID | None = None  # the item itself, once loaded
        self._generation = 0  # which item's answers are still wanted
        self._search_generation = 0
        self._text = ""
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SEARCH_DELAY_MS)
        self._timer.timeout.connect(self._search_now)

    @property
    def busy(self) -> bool:
        return self._busy

    def link(self) -> str:
        """`statelife-dropshare://open?node=<id>`: the ID only, never a token or a name."""
        if self._node_id is None:
            raise RuntimeError("open() first")
        # For a shared folder, the server answers with the folder itself: the mount the user
        # opened it from is theirs alone, so a link to it would open nothing for others.
        return link_to(self._target or self._node_id)

    def open(self, node_id: uuid.UUID) -> None:
        self._node_id, self._target = node_id, None
        self._generation += 1
        self._search_generation += 1
        self._timer.stop()
        self.reload(keep=False)

    def reload(self, *, keep: bool = True) -> None:
        node_id, generation = self._node_id, self._generation
        if node_id is None:
            return
        self.loading.emit(keep)
        self._worker.submit(
            lambda: self._sharing.sharing(node_id),
            lambda result: self._loaded(generation, result),
            lambda exc: self._failed(generation, exc),
        )

    def _loaded(self, generation: int, result: SharingOut) -> None:
        if generation == self._generation:
            self._target = result.node_id
            self.loaded.emit(result)

    def _failed(self, generation: int, exc: Exception) -> None:
        if generation == self._generation:
            logger.info("Couldn't load who has access: %s", exc)
            self.failed.emit(messages.message_for(exc))

    # --- search (FR-SHARE-08) --------------------------------------------------------------

    def search(self, text: str) -> None:
        """Called as the user types; asks the server once they pause."""
        self._text = text.strip()
        self._search_generation += 1
        if not SEARCH_MIN_LENGTH <= len(self._text) <= SEARCH_MAX_LENGTH:
            self._timer.stop()
            self.searching.emit(False)
            self.found.emit([])
            return
        self._timer.start()

    def _search_now(self) -> None:
        text, generation = self._text, self._search_generation
        self.searching.emit(True)

        def find() -> list[Grantee]:
            people = [
                Grantee(GranteeKind.USER, hit.id, hit.display_name, hit.email)
                for hit in self._sharing.find_people(text)
            ]
            groups = [
                Grantee(GranteeKind.GROUP, hit.id, hit.name, messages.group_size(hit.member_count))
                for hit in self._sharing.find_groups(text)
            ]
            return people + groups

        self._worker.submit(
            find,
            lambda hits: self._found(generation, hits),
            lambda exc: self._search_failed(generation, exc),
        )

    def _found(self, generation: int, hits: list[Grantee]) -> None:
        if generation == self._search_generation:
            self.searching.emit(False)
            self.found.emit(hits)

    def _search_failed(self, generation: int, exc: Exception) -> None:
        if generation == self._search_generation:
            self.searching.emit(False)
            self.found.emit([])
            self.change_failed.emit(messages.message_for(exc))

    # --- changes (owner only; the server enforces it too) -----------------------------------

    def add(self, grantee: Grantee, role: ShareRole) -> None:
        node_id = self._node_id
        if node_id is None:
            return
        user_id = grantee.id if grantee.kind == GranteeKind.USER else None
        group_id = grantee.id if grantee.kind == GranteeKind.GROUP else None
        self._change(
            lambda: self._sharing.add(node_id, role, user_id=user_id, group_id=group_id),
            messages.shared_with(grantee.name),
        )

    def change_role(self, member: MemberOut, role: ShareRole) -> None:
        self._change(
            lambda: self._sharing.change(member.id, role), messages.role_changed(member.name)
        )

    def remove(self, member: MemberOut) -> None:
        self._change(lambda: self._sharing.remove(member.id), messages.access_removed(member.name))

    def _change(self, send: Callable[[], object], done: str) -> None:
        if self._busy:
            self.change_failed.emit(messages.ACTION_ALREADY_RUNNING)
            return
        generation = self._generation
        self._set_busy(True)

        def succeeded(_: object) -> None:
            self._set_busy(False)
            if generation == self._generation:
                self.changed.emit(done)
                self.reload()

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            if generation == self._generation:
                logger.info("Sharing change failed: %s", exc)
                self.change_failed.emit(messages.message_for(exc))
                self.reload()

        self._worker.submit(send, succeeded, failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)
