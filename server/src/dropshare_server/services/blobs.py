"""Blob cleanup (NFR-REL-05, requirements §8): v1 keeps only each file's current version.

The gc-blobs job removes versions a newer one replaced, and blobs no version points at (a purge
whose blob deletion failed, or an upload that stored its blob but never recorded it). Both only
once they are older than the grace period, so a download that started before isn't cut off,
and a blob whose version is still being recorded is never taken.

Deleting what no row points at is irreversible, and a job run against the wrong database (or a
database restored from an older backup) would see every blob as unreferenced. So the sweep
refuses when the unreferenced blobs look like more than leftovers, unless forced.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.repositories import nodes as node_repository
from dropshare_server.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_BATCH = 500
# The sweep refuses above both: a few leftovers are normal, a large share of storage is not.
ORPHAN_REFUSE_COUNT = 100
ORPHAN_REFUSE_SHARE = 0.10


class TooManyOrphansError(Exception):
    """More blobs than expected have no version row: the database may be the wrong one."""

    def __init__(self, orphans: int, blobs: int) -> None:
        super().__init__(f"{orphans} of {blobs} stored blobs have no file version")
        self.orphans = orphans
        self.blobs = blobs


@dataclass(frozen=True)
class GcResult:
    versions: int  # superseded versions removed
    orphans: int  # blobs removed that no version pointed at


def delete_blobs(storage: StorageBackend, blob_keys: Iterable[str]) -> None:
    """After commit: remove content nothing points at any more. A failure is logged and left
    for the next gc-blobs run."""
    for key in blob_keys:
        try:
            storage.delete_blob(key)
        except OSError:
            logger.warning("Couldn't delete blob %s; gc-blobs will retry", key, exc_info=True)


def collect(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    now: datetime,
    grace_hours: int,
    *,
    force: bool = False,
) -> GcResult:
    """Idempotent, in batches, and safe to run while the API is serving. Raises
    TooManyOrphansError (having removed no unreferenced blob) unless `force`."""
    before = now - timedelta(hours=grace_hours)
    orphans = _find_orphans(session_factory, storage, before, force=force)

    versions = 0
    while True:
        with session_factory.begin() as session:
            batch = node_repository.superseded_versions(session, before, _BATCH)
            keys = [version.blob_key for version in batch]
            node_repository.delete_versions(session, [version.id for version in batch])
        delete_blobs(storage, keys)
        versions += len(batch)
        if len(batch) < _BATCH:
            break

    for key in orphans:
        logger.info("Removing blob %s: no file version points at it", key)
    delete_blobs(storage, orphans)
    return GcResult(versions=versions, orphans=len(orphans))


def _find_orphans(
    session_factory: sessionmaker[Session],
    storage: StorageBackend,
    before: datetime,
    *,
    force: bool,
) -> list[str]:
    """Blobs older than `before` with no version row; checked against the guard first."""
    blobs = 0
    orphans: list[str] = []
    candidates: list[str] = []
    for key, written_at in storage.blobs():
        blobs += 1
        if written_at < before:
            candidates.append(key)
        if len(candidates) >= _BATCH:
            orphans.extend(_unknown(session_factory, candidates))
            candidates = []
    orphans.extend(_unknown(session_factory, candidates))
    too_many = len(orphans) > ORPHAN_REFUSE_COUNT and len(orphans) > blobs * ORPHAN_REFUSE_SHARE
    if not force and (too_many or (orphans and len(orphans) == blobs)):
        raise TooManyOrphansError(len(orphans), blobs)
    return orphans


def _unknown(session_factory: sessionmaker[Session], keys: list[str]) -> list[str]:
    if not keys:
        return []
    with session_factory() as session:
        known = node_repository.known_blob_keys(session, keys)
    return [key for key in keys if key not in known]
