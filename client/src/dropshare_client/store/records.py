"""What the local index holds, as plain values the sync engine works with (algorithm.md §2)."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from dropshare_shared.schemas.nodes import NodeKind


@dataclass(frozen=True)
class Entry:
    """An item as it was when it was last in sync on both sides: the base of the three-way
    comparison. `key` is the normalized relative path, `path` the on-disk spelling."""

    key: str
    path: str
    node_id: uuid.UUID
    kind: NodeKind
    revision: int
    sha256: str | None  # None for folders
    size: int
    mtime_ns: int
    file_id: str | None


@dataclass(frozen=True)
class RemoteNode:
    """A live node as the change journal last described it."""

    node_id: uuid.UUID
    namespace_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str | None
    kind: NodeKind
    size: int
    sha256: str | None
    revision: int
    modified_at: datetime
    # Set on a mount: this folder shows that shared namespace (OQ-31).
    mount_namespace_id: uuid.UUID | None = None


@dataclass(frozen=True)
class NamespaceCursor:
    namespace_id: uuid.UUID
    root_node_id: uuid.UUID
    cursor: int
    # A shared folder's: the mount it is followed through. Another mount (the folder left and
    # added again) means starting over; none means letting it go (sync/core._follow_mounts).
    mount_node_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Binding:
    """Whose files the index describes. Anything else in the sync folder isn't theirs."""

    server_url: str
    user_id: uuid.UUID
    sync_root: str


@dataclass(frozen=True)
class JournalItem:
    """An operation that was started; removed once it is fully recorded (algorithm.md §10)."""

    id: int
    kind: str
    key: str
    data: dict[str, str]


@dataclass(frozen=True)
class Activity:
    at: datetime
    kind: str
    path: str
    detail: str


@dataclass(frozen=True)
class Problem:
    """An item that can't be synced, and why (FR-SYNC-07 "Can't sync")."""

    key: str
    path: str
    reason: str
