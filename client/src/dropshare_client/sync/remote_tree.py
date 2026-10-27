"""The server's live items, kept current from the change journal (requirements §10.1)."""

import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field

from dropshare_client.store.records import Problem, RemoteNode
from dropshare_client.sync import paths
from dropshare_shared.schemas.nodes import NodeKind, NodeOut
from dropshare_shared.schemas.sync import ChangeOut, ChangeType
from dropshare_shared.schemas.uploads import SHA256_PATTERN

logger = logging.getLogger(__name__)

_UNSAFE_NAME = "The server sent a name this computer can't use safely, so it isn't synced."


@dataclass(frozen=True)
class RemoteItem:
    path: str  # the server's spelling, relative to the sync root
    node: RemoteNode


@dataclass
class TreeDelta:
    """What applying changes did: the keys to look at again, and what to save."""

    dirty: set[str] = field(default_factory=set)
    upserts: dict[uuid.UUID, RemoteNode] = field(default_factory=dict)
    removed: set[uuid.UUID] = field(default_factory=set)
    # Changes of a type this client doesn't know (a newer server): ask for these nodes.
    refetch: set[uuid.UUID] = field(default_factory=set)


def is_safe(node: RemoteNode) -> bool:
    """Whether the node's name and hash can be used on disk. The server checks names too
    (NFR-COMPAT-02), but a name from the network never decides a local path unchecked: on
    Windows "..\\x" or "C:\\x" would be a path, not a name."""
    if node.name is None or paths.name_problem(node.name) is not None:
        return False
    if paths.key_of(node.name) in ("", ".", ".."):
        return False
    if node.kind == NodeKind.FILE:
        return node.sha256 is not None and SHA256_PATTERN.fullmatch(node.sha256) is not None
    return True


def from_node(node: NodeOut) -> RemoteNode | None:
    """None for kinds this client doesn't know: they're left alone."""
    if not isinstance(node.kind, NodeKind):
        return None
    return RemoteNode(
        node_id=node.id,
        namespace_id=node.namespace_id,
        parent_id=node.parent_id,
        name=node.name,
        kind=node.kind,
        size=node.size_bytes,
        sha256=node.content_hash,
        revision=node.revision,
        modified_at=node.modified_at,
    )


def _from_change(namespace_id: uuid.UUID, change: ChangeOut) -> RemoteNode | None:
    if not isinstance(change.kind, NodeKind):
        return None
    return RemoteNode(
        node_id=change.node_id,
        namespace_id=namespace_id,
        parent_id=change.parent_id,
        name=change.name,
        kind=change.kind,
        size=change.size_bytes,
        sha256=change.content_hash,
        revision=change.revision,
        modified_at=change.modified_at,
    )


class RemoteTree:
    """One namespace's tree. Only the engine's thread uses it."""

    def __init__(self, root_id: uuid.UUID, nodes: Iterable[RemoteNode] = ()) -> None:
        self.root_id = root_id
        self._nodes: dict[uuid.UUID, RemoteNode] = {}
        self._children: dict[uuid.UUID, dict[str, uuid.UUID]] = {}
        # Revisions of nodes seen going to the trash, so a late echo of an older state (a
        # replayed upload) can't bring them back.
        self._gone: dict[uuid.UUID, int] = {}
        for node in nodes:
            self._put(node)

    def __len__(self) -> int:
        return len(self._nodes)

    def apply(self, namespace_id: uuid.UUID, changes: Iterable[ChangeOut]) -> TreeDelta:
        delta = TreeDelta()
        for change in changes:
            if change.type in (ChangeType.CREATED, ChangeType.MODIFIED):
                node = _from_change(namespace_id, change)
                if node is None:
                    logger.info("Ignoring node %s of unknown kind %s", change.node_id, change.kind)
                    continue
                self.upsert(node, delta)
            elif change.type == ChangeType.TRASHED:
                self.remove(change.node_id, delta, revision=change.revision)
            else:
                logger.info(
                    "Unknown change type %s for %s: asking for the node",
                    change.type,
                    change.node_id,
                )
                delta.refetch.add(change.node_id)
        return delta

    def upsert(self, node: RemoteNode, delta: TreeDelta) -> None:
        """Add or update a node, unless we already have a newer revision of it (our own
        write, recorded before the journal caught up)."""
        if node.parent_id is None:
            return  # the namespace root: it is the sync folder itself
        old = self._nodes.get(node.node_id)
        if old is not None and old.revision > node.revision:
            return
        if node.revision <= self._gone.get(node.node_id, 0):
            return  # trashed at this revision or later
        if old is not None:
            self._mark(old.node_id, delta)
        self._put(node)
        self._mark(node.node_id, delta)
        delta.upserts[node.node_id] = node
        delta.removed.discard(node.node_id)

    def remove(self, node_id: uuid.UUID, delta: TreeDelta, *, revision: int | None = None) -> None:
        """Drop a node and everything below it (it went to the trash). Recorded as removed
        even when it isn't in memory, so a stale saved copy goes too."""
        node = self._nodes.get(node_id)
        if node is not None and revision is not None and node.revision > revision:
            return
        gone_at = revision if revision is not None else (node.revision if node else None)
        if gone_at is not None:
            self._gone[node_id] = max(self._gone.get(node_id, 0), gone_at)
        if node is not None:
            self._mark(node_id, delta)
        pending = [node_id]
        while pending:
            current = pending.pop()
            pending.extend(self._children.pop(current, {}).values())
            gone = self._nodes.pop(current, None)
            if gone is not None:
                self._gone[current] = max(self._gone.get(current, 0), gone.revision)
                if gone.parent_id is not None and gone.name is not None:
                    siblings = self._children.get(gone.parent_id, {})
                    if siblings.get(paths.key_of(gone.name)) == current:
                        del siblings[paths.key_of(gone.name)]
            delta.removed.add(current)
            delta.upserts.pop(current, None)

    def path_of(self, node_id: uuid.UUID) -> str | None:
        """The node's path, or None if it isn't connected to the root."""
        names: list[str] = []
        current = node_id
        while current != self.root_id:
            node = self._nodes.get(current)
            if node is None or node.parent_id is None or node.name is None:
                return None
            if len(names) > len(self._nodes):
                logger.warning("The server's tree has a cycle at %s", node_id)
                return None
            names.append(node.name)
            current = node.parent_id
        return "/".join(reversed(names))

    def node_at(self, key: str) -> uuid.UUID | None:
        current = self.root_id
        for part in key.split("/") if key else []:
            found = self._children.get(current, {}).get(part)
            if found is None:
                return None
            current = found
        return current

    def items_under(
        self, key: str, problems: dict[str, Problem] | None = None
    ) -> dict[str, RemoteItem]:
        """Every syncable item at and below `key` (the root itself excluded). Items whose
        name or hash can't be used safely are left out, with everything below them, and
        listed in `problems`."""
        start = self.node_at(key)
        if start is None:
            return {}
        result: dict[str, RemoteItem] = {}
        pending: list[tuple[uuid.UUID, str]] = []
        if start == self.root_id:
            pending.append((start, ""))
        else:
            path = self.path_of(start)
            node = self._nodes[start]
            if path is None or not self._usable(node, path, problems):
                return {}
            result[paths.key_of(path)] = RemoteItem(path, node)
            pending.append((start, path))
        while pending:
            parent, parent_path = pending.pop()
            for child_id in self._children.get(parent, {}).values():
                child = self._nodes[child_id]
                path = paths.join(parent_path, child.name or "")
                if not self._usable(child, path, problems):
                    continue
                result[paths.key_of(path)] = RemoteItem(path, child)
                if child.kind == NodeKind.FOLDER:
                    pending.append((child_id, path))
        return result

    @staticmethod
    def _usable(node: RemoteNode, path: str, problems: dict[str, Problem] | None) -> bool:
        if node.name is not None and paths.is_ignored(node.name):
            return False
        if is_safe(node):
            return True
        logger.warning("Not syncing server item %s: unusable name or hash", node.node_id)
        if problems is not None:
            shown = "".join(c if c.isprintable() else "?" for c in path)
            key = paths.key_of(shown)
            problems[key] = Problem(key, shown, _UNSAFE_NAME)
        return False

    def _put(self, node: RemoteNode) -> None:
        old = self._nodes.get(node.node_id)
        if old is not None and old.parent_id is not None and old.name is not None:
            siblings = self._children.get(old.parent_id, {})
            if siblings.get(paths.key_of(old.name)) == node.node_id:
                del siblings[paths.key_of(old.name)]
        self._nodes[node.node_id] = node
        if node.parent_id is not None and node.name is not None:
            self._children.setdefault(node.parent_id, {})[paths.key_of(node.name)] = node.node_id

    def _mark(self, node_id: uuid.UUID, delta: TreeDelta) -> None:
        path = self.path_of(node_id)
        if path is not None:
            delta.dirty.add(paths.key_of(path))
