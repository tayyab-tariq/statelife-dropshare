"""Decide what to do for each path from three states: local now, server now, and the last
synced base (requirements §10.2, algorithm.md §4). A pure function: no disk, no network.

The rules that keep data safe live here:
- A side that didn't change never wins over one that did; an edit beats a delete (FR-CONF-02).
- Different content on both sides keeps both: the local item is renamed to a conflicted copy
  and both names sync in the next cycle (FR-CONF-01).
- A folder delete is applied only if nothing below it changed on the other side; otherwise
  the folder is recreated where it was deleted and only the unchanged items go (FR-CONF-03).
- Without a base (first sync, rebuilt index) nothing is ever deleted: items are linked when
  equal and kept twice when not.
- A rename or move keeps the item's identity (FR-SYNC-10): a local one is found by file ID (or
  by size and hash), a remote one by node ID. If both sides moved it differently, the
  server's location wins (OQ-27). A move whose target is taken on the other side isn't paired:
  the ordinary rules keep both items.
"""

import bisect
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from enum import StrEnum

from dropshare_client.store.records import Entry
from dropshare_client.sync import paths
from dropshare_client.sync.remote_tree import RemoteItem
from dropshare_client.sync.scanner import LocalItem
from dropshare_shared.schemas.nodes import NodeKind

# FR-SYNC-08: more than this many files, or this share of the synced files, in one cycle.
MASS_DELETE_FILES = 500
MASS_DELETE_SHARE = 0.25
# The share rule only counts from this many files on, so deleting 1 of 3 files doesn't ask
# (audit decision D1, 2026-10-27; recorded in requirements §15).
MASS_DELETE_SHARE_MIN_FILES = 10


class Op(StrEnum):
    CONFLICT = "conflict"  # rename the local item to a conflicted copy
    MAKE_LOCAL_FOLDER = "make_local_folder"
    MAKE_REMOTE_FOLDER = "make_remote_folder"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    RECORD = "record"  # index only: link equal items, refresh the base, or forget it
    DELETE_LOCAL = "delete_local"  # to the Recycle Bin (FR-SYNC-09)
    DELETE_REMOTE = "delete_remote"  # to the server trash
    MOVE_LOCAL = "move_local"  # rename/move on disk to where the server has it
    MOVE_REMOTE = "move_remote"  # rename/move on the server to where it is on disk


# Ops that keep content on the local side / the server side below a deleted folder.
_KEEPS_LOCAL = frozenset({Op.UPLOAD, Op.MAKE_REMOTE_FOLDER, Op.CONFLICT})
_KEEPS_REMOTE = frozenset({Op.DOWNLOAD, Op.MAKE_LOCAL_FOLDER})
_ORDER = {
    Op.CONFLICT: 0,
    Op.MAKE_LOCAL_FOLDER: 1,
    Op.MAKE_REMOTE_FOLDER: 1,
    Op.MOVE_LOCAL: 2,
    Op.MOVE_REMOTE: 2,
    Op.UPLOAD: 3,
    Op.DOWNLOAD: 3,
    Op.RECORD: 4,
    Op.DELETE_LOCAL: 5,
    Op.DELETE_REMOTE: 5,
}


@dataclass(frozen=True)
class Action:
    """For a move, `key` is where the item ends up, `local` and `remote` are the item where it
    is on each side now, and `base` is the entry at its old key."""

    op: Op
    key: str
    local: LocalItem | None
    remote: RemoteItem | None
    base: Entry | None

    @property
    def path(self) -> str:
        for item in (self.local, self.remote, self.base):
            if item is not None:
                return item.path
        return self.key


@dataclass(frozen=True)
class Plan:
    actions: tuple[Action, ...]
    again: bool  # some items need another cycle once these actions are done
    local_deletes: int  # files that would leave this computer (to the Recycle Bin)
    remote_deletes: int  # files that would go to the server trash

    @property
    def deletes(self) -> int:
        return self.local_deletes + self.remote_deletes


def is_mass_delete(deletes: int, synced_files: int) -> bool:
    """FR-SYNC-08, over both directions together."""
    return deletes > MASS_DELETE_FILES or (
        deletes > MASS_DELETE_SHARE_MIN_FILES and deletes > synced_files * MASS_DELETE_SHARE
    )


def needs_hash(local: LocalItem, base: Entry | None) -> bool:
    """The local file's content must be known to decide: it's new or its size/mtime moved."""
    if local.kind != NodeKind.FILE or local.sha256 is not None:
        return False
    return (
        base is None
        or base.kind != NodeKind.FILE
        or (local.size, local.mtime_ns) != (base.size, base.mtime_ns)
    )


def reconcile(
    keys: Iterable[str],
    local: Mapping[str, LocalItem],
    remote: Mapping[str, RemoteItem],
    base: Mapping[str, Entry],
    deferred: Collection[str] = (),
) -> Plan:
    """Actions for every key in `keys`. Keys at or below a `deferred` key are left alone."""
    deferred_set = set(deferred)
    ordered = sorted(set(keys))
    moves, waiting = _find_moves(base, local, remote, deferred_set)
    move_keys = {k for move in moves for k in move.keys}
    decisions: dict[str, Op] = {}
    again = bool(waiting)
    for key in ordered:
        if _is_deferred(key, deferred_set) or _within_any(key, waiting):
            continue
        if _within_any(key, move_keys):
            # Moves with its folder, or waits for the move: the next cycle compares it with
            # the base at its new key.
            again = again or key not in move_keys
            continue
        item = local.get(key)
        if item is not None and needs_hash(item, base.get(key)):
            deferred_set.add(key)  # content unknown (unreadable or changing): not this time
            continue
        decided = _decide(item, remote.get(key), base.get(key))
        if decided is not None:
            op, retry = decided
            decisions[key] = op
            again = again or retry

    again = _folder_deletes(ordered, decisions, local, remote, base, deferred_set) or again
    again = _drop_below_conflicts(ordered, decisions) or again

    for move in moves:
        decisions[move.key] = move.op
        again = True  # the moved item's own changes, and what's inside it, come next
    actions = tuple(_ordered_actions(decisions, local, remote, base, moves))
    # Files a move takes out of a deleted folder aren't deleted (FR-SYNC-08 counts).
    moved_out = {move.base.key for move in moves}
    files = sorted(
        key
        for key, entry in base.items()
        if entry.kind == NodeKind.FILE and not _within_any(key, moved_out)
    )
    local_deletes = sum(_files_at(a, files) for a in actions if a.op == Op.DELETE_LOCAL)
    remote_deletes = sum(_files_at(a, files) for a in actions if a.op == Op.DELETE_REMOTE)
    return Plan(actions, again, local_deletes, remote_deletes)


@dataclass(frozen=True)
class _Move:
    op: Op
    key: str  # where the item ends up
    local_key: str  # where it is on disk now
    local: LocalItem
    remote: RemoteItem  # the node where the server has it now
    base: Entry  # its entry, at the old key

    @property
    def keys(self) -> tuple[str, str, str]:
        return self.base.key, self.key, self.local_key

    def action(self) -> Action:
        return Action(self.op, self.key, self.local, self.remote, self.base)


@dataclass
class _Candidates:
    """Items new on disk (no base), by file ID and by content: where a moved item now is."""

    by_id: dict[str, list[str]] = field(default_factory=dict)
    by_content: dict[tuple[int, str], list[str]] = field(default_factory=dict)
    claimed: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, local: Mapping[str, LocalItem], base: Mapping[str, Entry]) -> "_Candidates":
        found = cls()
        for key, item in local.items():
            if key in base:
                continue
            if item.file_id is not None:
                found.by_id.setdefault(item.file_id, []).append(key)
            # Empty files are all alike: their content says nothing about identity.
            if item.kind == NodeKind.FILE and item.sha256 is not None and item.size > 0:
                found.by_content.setdefault((item.size, item.sha256), []).append(key)
        return found


def _find_moves(
    base: Mapping[str, Entry],
    local: Mapping[str, LocalItem],
    remote: Mapping[str, RemoteItem],
    deferred: set[str],
) -> tuple[list[_Move], set[str]]:
    """Synced items now somewhere else on either side, and the old keys of moves that wait a
    cycle (another item in the way here, or overlapping a move found first). Outermost
    first; an item inside a moved folder follows in the next cycle, when its base has moved
    along. A waiting key is left alone, never read as deleted (audit S1)."""
    remote_at = {item.node.node_id: key for key, item in remote.items()}
    candidates = _Candidates.of(local, base)
    moves: list[_Move] = []
    waiting: set[str] = set()
    taken: set[str] = set()  # keys a move found so far covers
    for key in sorted(base, key=lambda k: (paths.depth(k), k)):
        if _is_deferred(key, deferred) or _within_any(key, taken):
            continue
        entry = base[key]
        local_key = _local_location(key, entry, local, candidates)
        remote_key = remote_at.get(entry.node_id)
        if local_key is None or remote_key is None:
            continue  # gone on a side: the ordinary rules decide
        here, there = local[local_key], remote[remote_key]
        if here.kind != entry.kind or there.node.kind != entry.kind:
            continue
        if remote_key != key or _renamed(there.path, entry.path):
            decided = _server_move(key, local_key, remote_key, local, base)
        elif local_key != key or _renamed(here.path, entry.path):
            decided = _local_move(entry, local_key, remote, base)
        else:
            continue
        if decided is None:
            continue  # the ordinary rules keep both items
        op, target = decided
        if op is None or _is_deferred(target, deferred) or _within_any(target, taken):
            waiting.add(key)
            continue
        if _within_any(local_key, taken):
            waiting.add(key)
            continue
        candidates.claimed.add(local_key)
        move = _Move(op, target, local_key, here, there, entry)
        taken.update(move.keys)
        moves.append(move)
    return moves, waiting


def _server_move(
    key: str,
    local_key: str,
    target: str,
    local: Mapping[str, LocalItem],
    base: Mapping[str, Entry],
) -> tuple[Op | None, str] | None:
    """Moved on the server (and maybe here too: the server's location wins, OQ-27).
    (None, target) to wait a cycle; None when the ordinary rules decide."""
    if target != local_key and target in local and target not in base:
        # Another item is in the way here: it becomes a conflicted copy this cycle (the
        # ordinary rules), and the move follows in the next.
        return None, target
    if target != local_key and target in base and target != key:
        return None
    if _parent_gone(target, base, local):
        return None  # its new folder was deleted here: FR-CONF-03 brings it back
    return Op.MOVE_LOCAL, target


def _local_move(
    entry: Entry,
    target: str,
    remote: Mapping[str, RemoteItem],
    base: Mapping[str, Entry],
) -> tuple[Op | None, str] | None:
    """Moved here only. None when the ordinary rules decide."""
    other = remote.get(target)
    if other is not None and other.node.node_id != entry.node_id:
        return None  # something else has that name on the server
    if _parent_gone(target, base, remote):
        return None  # its new folder went to the server trash: FR-CONF-03 again
    return Op.MOVE_REMOTE, target


def _within_any(key: str, keys: AbstractSet[str]) -> bool:
    """`key` is one of `keys` or below one: O(depth), whatever the number of keys."""
    if not keys:
        return False
    return key in keys or any(a in keys for a in paths.ancestors(key) if a)


def _parent_gone(key: str, base: Mapping[str, Entry], side: Mapping[str, object]) -> bool:
    """The folder `key` would go into was synced, but is gone on `side` now. (A folder that
    is new there is fine: it is created before moves run.)"""
    parent = paths.parent_of(key)
    return bool(parent) and parent in base and parent not in side


def _local_location(
    key: str, entry: Entry, local: Mapping[str, LocalItem], candidates: _Candidates
) -> str | None:
    """Where the synced item `entry` is on disk now. An item of the same kind still at its
    key is taken to be it, edited in place: an editor that renames the original aside as a
    backup (`report.txt~`) or a hard link elsewhere must not take its identity (audit S2).
    Otherwise by file ID, then (files only) by identical size and content. None when it's
    gone or ambiguous."""
    here = local.get(key)
    if here is not None:
        return key if here.kind == entry.kind else None
    # Renamed to a conflicted copy by the conflict step (FR-CONF-01), not by the user: the
    # server's version keeps the original name, so this is never sent as a move.
    copy_ok = paths.is_conflict_copy(paths.name_of(entry.path))

    def usable(k: str) -> bool:
        return k not in candidates.claimed and (
            copy_ok or not paths.is_conflict_copy(paths.name_of(k))
        )

    if entry.file_id is not None:
        found = [k for k in candidates.by_id.get(entry.file_id, []) if usable(k)]
        if len(found) == 1:
            return found[0]
    if entry.kind == NodeKind.FILE and entry.sha256 is not None and entry.size > 0:
        content = candidates.by_content.get((entry.size, entry.sha256), [])
        found = [k for k in content if usable(k)]
        if len(found) == 1:
            return found[0]
    return None


def _renamed(path: str, base_path: str) -> bool:
    """The name itself changed spelling (case, or anything else the key folds away)."""
    return _nfc(paths.name_of(path)) != _nfc(paths.name_of(base_path))


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _decide(
    local: LocalItem | None, remote: RemoteItem | None, base: Entry | None
) -> tuple[Op, bool] | None:
    """(op, needs another cycle), or None when there is nothing to do."""
    local_same = _local_same(local, base)
    remote_same = _remote_same(remote, base)
    if local_same and remote_same:
        return (Op.RECORD, False) if _stale_base(local, remote, base) else None

    if remote_same:  # changed here only
        if local is None:
            return Op.DELETE_REMOTE, False
        if base is not None and local.kind != base.kind:
            return Op.DELETE_REMOTE, True  # replaced by the other kind: remove, then add
        return (Op.UPLOAD if local.kind == NodeKind.FILE else Op.MAKE_REMOTE_FOLDER), False

    if local_same:  # changed on the server only
        if remote is None:
            return Op.DELETE_LOCAL, False
        if local is not None and remote.node.kind != local.kind:
            return Op.DELETE_LOCAL, True
        if remote.node.kind == NodeKind.FILE:
            return Op.DOWNLOAD, False
        return (Op.MAKE_LOCAL_FOLDER if local is None else Op.RECORD), False

    # Changed on both sides.
    if local is None and remote is None:
        return Op.RECORD, False  # deleted on both: forget it
    if local is None:
        assert remote is not None  # noqa: S101 - narrowed above
        # Deleted here, changed there: the edit wins and comes back (FR-CONF-02).
        return (Op.DOWNLOAD if remote.node.kind == NodeKind.FILE else Op.MAKE_LOCAL_FOLDER), False
    if remote is None:
        # Changed here, deleted there: the edit wins and goes back as a new item.
        return (Op.UPLOAD if local.kind == NodeKind.FILE else Op.MAKE_REMOTE_FOLDER), False
    if local.kind != remote.node.kind:
        return Op.CONFLICT, True
    if local.kind == NodeKind.FOLDER or local.sha256 == remote.node.sha256:
        return Op.RECORD, False  # the same on both sides already: link them
    return Op.CONFLICT, True


def _local_same(local: LocalItem | None, base: Entry | None) -> bool:
    if local is None or base is None:
        return local is None and base is None
    if local.kind != base.kind:
        return False
    if local.kind == NodeKind.FOLDER:
        return True
    if (local.size, local.mtime_ns) == (base.size, base.mtime_ns):
        return True
    return local.sha256 is not None and local.sha256 == base.sha256


def _remote_same(remote: RemoteItem | None, base: Entry | None) -> bool:
    if remote is None or base is None:
        return remote is None and base is None
    node = remote.node
    if node.kind != base.kind or node.node_id != base.node_id:
        return False
    return node.kind == NodeKind.FOLDER or node.sha256 == base.sha256


def _stale_base(local: LocalItem | None, remote: RemoteItem | None, base: Entry | None) -> bool:
    """Nothing to sync, but the base should catch up: a new server revision without new
    content, a file touched without being changed, or a new spelling on disk."""
    if local is None or remote is None or base is None:
        return False
    if remote.node.revision != base.revision or local.path != base.path:
        return True
    if base.file_id is None and local.file_id is not None:
        return True  # an entry from before file IDs were kept for folders (move detection)
    return local.kind == NodeKind.FILE and (local.size, local.mtime_ns) != (
        base.size,
        base.mtime_ns,
    )


def _is_deferred(key: str, deferred: set[str]) -> bool:
    return key in deferred or any(a in deferred for a in paths.ancestors(key) if a)


def _below(ordered: list[str], key: str) -> list[str]:
    """Keys strictly below `key` in the sorted list."""
    if not key:
        return [k for k in ordered if k]
    start = bisect.bisect_left(ordered, f"{key}/")
    end = bisect.bisect_left(ordered, f"{key}0")
    return ordered[start:end]


def _folder_deletes(
    ordered: list[str],
    decisions: dict[str, Op],
    local: Mapping[str, LocalItem],
    remote: Mapping[str, RemoteItem],
    base: Mapping[str, Entry],
    deferred: set[str],
) -> bool:
    """FR-CONF-03, deepest folders first so a kept child keeps its parents too. Returns
    whether another cycle is needed."""
    again = False
    folder_deletes = [
        key
        for key, op in decisions.items()
        if op in (Op.DELETE_LOCAL, Op.DELETE_REMOTE)
        and (entry := base.get(key)) is not None
        and entry.kind == NodeKind.FOLDER
    ]
    for key in sorted(folder_deletes, key=lambda k: (-paths.depth(k), k)):
        below = _below(ordered, key)
        if decisions[key] == Op.DELETE_LOCAL:
            keeps = any(decisions.get(k) in _KEEPS_LOCAL for k in below) or any(
                d != key and paths.is_within(d, key) for d in deferred
            )
            if keeps:
                # Recreate the folder on the server, unless a file took its name there.
                taken = remote.get(key) is not None
                decisions[key] = Op.CONFLICT if taken else Op.MAKE_REMOTE_FOLDER
                again = again or taken
                continue
            subsumed = (Op.DELETE_LOCAL, Op.RECORD)
        else:
            if any(decisions.get(k) in _KEEPS_REMOTE for k in below):
                taken = local.get(key) is not None
                decisions[key] = Op.CONFLICT if taken else Op.MAKE_LOCAL_FOLDER
                again = again or taken
                continue
            subsumed = (Op.DELETE_REMOTE, Op.RECORD)
        for child in below:  # the folder delete takes these with it
            if decisions.get(child) in subsumed:
                del decisions[child]
    return again


def _drop_below_conflicts(ordered: list[str], decisions: dict[str, Op]) -> bool:
    """A folder renamed to a conflicted copy takes its contents along; they sync next cycle."""
    dropped = False
    for key in [k for k, op in decisions.items() if op == Op.CONFLICT]:
        for child in _below(ordered, key):
            if decisions.pop(child, None) is not None:
                dropped = True
    return dropped


def _ordered_actions(
    decisions: Mapping[str, Op],
    local: Mapping[str, LocalItem],
    remote: Mapping[str, RemoteItem],
    base: Mapping[str, Entry],
    moves: Iterable[_Move],
) -> list[Action]:
    """Conflict renames, then folder creates and folder links (parents first, so uploads find
    the right parent), transfers, other index-only updates, and deletes last (children first)."""

    def order(key: str) -> tuple[int, int, str]:
        op = decisions[key]
        rank = _ORDER[op]
        item = remote.get(key) or local.get(key)
        if op == Op.RECORD and item is not None and _kind(item) == NodeKind.FOLDER:
            rank = _ORDER[Op.MAKE_LOCAL_FOLDER]
        level = paths.depth(key)
        return rank, -level if rank == _ORDER[Op.DELETE_LOCAL] else level, key

    move_actions = {move.key: move.action() for move in moves}
    return [
        move_actions.get(key)
        or Action(decisions[key], key, local.get(key), remote.get(key), base.get(key))
        for key in sorted(decisions, key=order)
    ]


def _kind(item: LocalItem | RemoteItem) -> NodeKind:
    return item.node.kind if isinstance(item, RemoteItem) else item.kind


def _files_at(action: Action, files: list[str]) -> int:
    """How many synced files a delete removes: the file itself, or those in the folder."""
    if action.base is None:
        return 0
    if action.base.kind == NodeKind.FILE:
        return 1
    return len(_below(files, action.key))
