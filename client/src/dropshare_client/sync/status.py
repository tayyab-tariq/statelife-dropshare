"""What the sync index says, for the file browser (FR-FILE-01) and the Activity and Can't sync
views (FR-CLIENT-03, FR-SYNC-07): read on a worker thread, never the engine's, without writing
(store.index read_*). The only way code outside the engine sees the index."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dropshare_client.store.index import read_activity, read_entries, read_problems
from dropshare_client.store.records import Activity, Problem


@dataclass(frozen=True)
class Synced:
    """A node as last synced to this computer."""

    path: str  # in the sync folder, as spelled on disk
    sha256: str | None  # None for folders


def synced(index_path: Path, node_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Synced]:
    return {
        node_id: Synced(entry.path, entry.sha256)
        for node_id, entry in read_entries(index_path, node_ids).items()
    }


def recent_activity(index_path: Path, limit: int) -> list[Activity]:
    """The latest sync events, newest first (FR-CLIENT-03)."""
    return read_activity(index_path, limit)


def cant_sync(index_path: Path) -> list[Problem]:
    """Items that can't be synced (FR-SYNC-07)."""
    return read_problems(index_path)
