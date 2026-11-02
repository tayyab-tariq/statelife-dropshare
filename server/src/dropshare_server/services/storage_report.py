"""Storage report for the admin CLI (FR-ADMIN-01): quota and usage per user, and what is stored.

Bytes come from the database: the storage interface lists blobs without their sizes, so only the
blob count is read from storage (OQ-46). More blobs stored than versions recorded means uploads
finishing right now (a blob is stored before its version row) or leftovers that gc-blobs will
remove (NFR-REL-05).
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services.context import CallContext
from dropshare_server.storage.base import StorageBackend
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.users import UserStatus


@dataclass(frozen=True)
class UserUsage:
    username: str
    status: UserStatus
    quota_bytes: int
    used_bytes: int  # trash included
    trash_bytes: int


@dataclass(frozen=True)
class StorageReport:
    users: list[UserUsage]
    version_count: int
    version_bytes: int
    stored_blobs: int

    @property
    def used_bytes(self) -> int:
        return sum(user.used_bytes for user in self.users)

    @property
    def quota_bytes(self) -> int:
        return sum(user.quota_bytes for user in self.users)


def count_blobs(storage: StorageBackend) -> int:
    """Walks the whole store: done before the database transaction opens, not inside it.
    Raises OSError when the storage can't be read."""
    return sum(1 for _ in storage.blobs())


def build(session: Session, ctx: CallContext, stored_blobs: int) -> StorageReport:
    trash = node_repository.trash_bytes_by_owner(session)
    users = [
        UserUsage(
            username=user.username,
            status=user.status,
            quota_bytes=user.quota_bytes,
            used_bytes=user.used_bytes,
            trash_bytes=trash.get(user.id, 0),
        )
        for user in user_repository.list_all(session)
    ]
    version_count, version_bytes = node_repository.version_totals(session)
    audit_service.record(
        session,
        ctx,
        AuditAction.STORAGE_REPORTED,
        details={"users": len(users), "stored_blobs": stored_blobs},
    )
    return StorageReport(users, version_count, version_bytes, stored_blobs)
