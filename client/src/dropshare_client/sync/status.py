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


# --- Malware (FR-SAFE-01): why an upload was refused, in plain language -----------------------

VIRUS_FOUND = "A virus was found in this file"
# What the server calls a file it couldn't scan in full: refused, but not as a virus.
_UNCHECKED = ("DropShare.Unscannable", "Heuristics.Limits.Exceeded")


def malware_reason(detection: str) -> str:
    """The Can't sync reason for an upload the malware scan refused. The file here is never
    changed, moved or deleted (OQ-57 (1))."""
    if detection.startswith(_UNCHECKED):
        return (
            "DropShare couldn't check this file for viruses (it is too large or complex to "
            "scan), so it wasn't uploaded. Your copy here hasn't been changed. Keep it outside "
            "your DropShare folder, or ask your administrator for help."
        )
    named = f" ({detection})" if detection else ""
    return (
        f"{VIRUS_FOUND}{named}, so it wasn't uploaded. Your copy here hasn't been changed: "
        "delete it, or ask your administrator if you think it's safe."
    )


def is_virus(problem: Problem) -> bool:
    return problem.reason.startswith(VIRUS_FOUND)
