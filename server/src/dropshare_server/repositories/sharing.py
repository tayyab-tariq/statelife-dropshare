"""Members of shared folders and single-file shares. The only code that sets `grantee_key`."""

import hashlib
import uuid
from collections.abc import Collection

from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.orm import Session

from dropshare_server.models import FileShare, GroupMember, NamespaceMember, Node
from dropshare_shared.schemas.sharing import GranteeKind, ShareRole

type Share = NamespaceMember | FileShare


def grantee_key(target_id: uuid.UUID, user_id: uuid.UUID | None, group_id: uuid.UUID | None) -> str:
    """One row per user or group for a namespace (or file): unique without a composite index
    over nullable columns, which Oracle can't do (database.md §5)."""
    if user_id is not None:
        grantee = f"{GranteeKind.USER.value}/{user_id}"
    else:
        grantee = f"{GranteeKind.GROUP.value}/{group_id}"
    return hashlib.sha256(f"{target_id}/{grantee}".encode()).hexdigest()


def _yours(
    model: type[NamespaceMember] | type[FileShare], user_id: uuid.UUID
) -> ColumnElement[bool]:
    """The rows that give `user_id` a role: their own, or one of their groups'."""
    groups = select(GroupMember.group_id).where(GroupMember.user_id == user_id)
    return or_(model.user_id == user_id, model.group_id.in_(groups))


def namespace_roles(
    session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID
) -> list[ShareRole]:
    return list(
        session.scalars(
            select(NamespaceMember.role).where(
                NamespaceMember.namespace_id == namespace_id,
                _yours(NamespaceMember, user_id),
            )
        )
    )


def file_roles(session: Session, user_id: uuid.UUID, node_id: uuid.UUID) -> list[ShareRole]:
    return list(
        session.scalars(
            select(FileShare.role).where(
                FileShare.node_id == node_id,
                _yours(FileShare, user_id),
            )
        )
    )


def namespace_memberships(session: Session, user_id: uuid.UUID) -> list[NamespaceMember]:
    """Every row giving the user (directly or through a group) a role on a shared folder."""
    return list(
        session.scalars(
            select(NamespaceMember)
            .where(_yours(NamespaceMember, user_id))
            .order_by(NamespaceMember.created_at, NamespaceMember.id)
        )
    )


def file_shares_for(session: Session, user_id: uuid.UUID) -> list[FileShare]:
    """Every row giving the user (directly or through a group) a role on a live file."""
    return list(
        session.scalars(
            select(FileShare)
            .join(Node, Node.id == FileShare.node_id)
            .where(
                _yours(FileShare, user_id),
                Node.trashed_at.is_(None),
            )
            .order_by(FileShare.created_at, FileShare.id)
        )
    )


def add(session: Session, share: Share, target_id: uuid.UUID) -> None:
    share.grantee_key = grantee_key(target_id, share.user_id, share.group_id)
    session.add(share)
    session.flush()


def get_by_grantee[M: (NamespaceMember, FileShare)](
    session: Session,
    model: type[M],
    target_id: uuid.UUID,
    user_id: uuid.UUID | None,
    group_id: uuid.UUID | None,
) -> M | None:
    key = grantee_key(target_id, user_id, group_id)
    return session.scalars(select(model).where(model.grantee_key == key)).one_or_none()


def get(session: Session, member_id: uuid.UUID) -> Share | None:
    """A member of a shared folder or of a file share: both kinds share one ID space."""
    member = session.get(NamespaceMember, member_id)
    if member is not None:
        return member
    return session.get(FileShare, member_id)


def namespace_members(session: Session, namespace_id: uuid.UUID) -> list[NamespaceMember]:
    return list(
        session.scalars(
            select(NamespaceMember)
            .where(NamespaceMember.namespace_id == namespace_id)
            .order_by(NamespaceMember.created_at, NamespaceMember.id)
        )
    )


def file_members(session: Session, node_id: uuid.UUID) -> list[FileShare]:
    return list(
        session.scalars(
            select(FileShare)
            .where(FileShare.node_id == node_id)
            .order_by(FileShare.created_at, FileShare.id)
        )
    )


def remove(session: Session, share: Share) -> None:
    session.delete(share)
    session.flush()


def namespaces_shared_with_group(session: Session, group_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.scalars(
            select(NamespaceMember.namespace_id).where(NamespaceMember.group_id == group_id)
        )
    )


def remove_group(session: Session, group_id: uuid.UUID) -> None:
    """Every share with a group that is being deleted."""
    session.execute(delete(NamespaceMember).where(NamespaceMember.group_id == group_id))
    session.execute(delete(FileShare).where(FileShare.group_id == group_id))
    session.flush()


def mount_holders(session: Session, namespace_ids: Collection[uuid.UUID]) -> list[Node]:
    """Live mounts of these namespaces, in anyone's tree. Callers pass a handful of IDs."""
    if not namespace_ids:
        return []
    return list(
        session.scalars(
            select(Node).where(
                Node.mount_namespace_id.in_(list(namespace_ids)), Node.trashed_at.is_(None)
            )
        )
    )
