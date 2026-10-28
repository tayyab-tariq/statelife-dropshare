"""What the sync index says about server items, for the file browser (FR-FILE-01): read on a
worker thread, never the engine's, without writing (store.index.read_entries). The only way
code outside the engine sees the index."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from dropshare_client.store.index import read_entries


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
