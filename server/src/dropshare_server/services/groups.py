"""Groups for sharing, administered with the CLI (FR-ADMIN-01). No HTTP endpoints in v1."""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.models import Group, GroupMember, User
from dropshare_server.repositories import groups as group_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.services import audit as audit_service
from dropshare_server.services import sharing as sharing_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.context import CallContext
from dropshare_server.services.users import normalize_login
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.audit import AuditAction

GROUP_NAME_MAX_LENGTH = 255


@dataclass(frozen=True)
class GroupSummary:
    name: str
    member_count: int


def _target(group: Group) -> AuditTarget:
    return AuditTarget(type="group", id=str(group.id), name=group.name)


def _require(session: Session, name: str) -> Group:
    group = group_repository.get_by_name(session, normalize_name(name.strip()))
    if group is None:
        raise AppError(404, ErrorCode.NOT_FOUND, f"No group named '{name}'.")
    return group


def _require_user(session: Session, username: str) -> User:
    user = user_repository.get_by_username(session, normalize_login(username))
    if user is None:
        raise AppError(404, ErrorCode.NOT_FOUND, f"No user named '{username}'.")
    return user


def create_group(session: Session, ctx: CallContext, name: str) -> GroupSummary:
    """Names are unique ignoring case, like usernames."""
    name = name.strip()
    if not name or len(name) > GROUP_NAME_MAX_LENGTH:
        raise AppError(
            400,
            ErrorCode.VALIDATION_ERROR,
            f"Group name must be 1-{GROUP_NAME_MAX_LENGTH} characters.",
        )
    if group_repository.get_by_name(session, normalize_name(name)) is not None:
        raise AppError(409, ErrorCode.NAME_CONFLICT, f"Group '{name}' already exists.")
    group = Group(name=name, name_normalized=normalize_name(name), created_at=datetime.now(UTC))
    group_repository.add(session, group)
    audit_service.record(session, ctx, AuditAction.GROUP_CREATED, target=_target(group))
    return GroupSummary(group.name, 0)


def delete_group(session: Session, ctx: CallContext, name: str) -> None:
    """Access shared with the group ends with it (FR-SHARE-04)."""
    group = _require(session, name)
    members = group_repository.member_ids(session, group.id)
    audit_service.record(
        session,
        ctx,
        AuditAction.GROUP_DELETED,
        target=_target(group),
        details={"members": len(members)},
    )
    sharing_service.group_deleted(session, group.id, members)
    group_repository.delete_group(session, group)


def add_member(session: Session, ctx: CallContext, name: str, username: str) -> None:
    group = _require(session, name)
    user = _require_user(session, username)
    if group_repository.is_member(session, group.id, user.id):
        raise AppError(
            409, ErrorCode.NAME_CONFLICT, f"'{user.username}' is already in '{group.name}'."
        )
    group_repository.add_member(
        session, GroupMember(group_id=group.id, user_id=user.id, created_at=datetime.now(UTC))
    )
    audit_service.record(
        session,
        ctx,
        AuditAction.GROUP_MEMBER_ADDED,
        target=_target(group),
        details={"user_id": str(user.id), "username": user.username},
    )


def remove_member(session: Session, ctx: CallContext, name: str, username: str) -> None:
    group = _require(session, name)
    user = _require_user(session, username)
    if not group_repository.remove_member(session, group.id, user.id):
        raise AppError(404, ErrorCode.NOT_FOUND, f"'{user.username}' isn't in '{group.name}'.")
    # Access through the group ends on the user's next sync (FR-ADMIN-01).
    sharing_service.group_member_removed(session, group.id, user.id)
    audit_service.record(
        session,
        ctx,
        AuditAction.GROUP_MEMBER_REMOVED,
        target=_target(group),
        details={"user_id": str(user.id), "username": user.username},
    )


def list_groups(session: Session, ctx: CallContext) -> list[GroupSummary]:
    groups = [
        GroupSummary(group.name, count)
        for group, count in group_repository.list_with_counts(session)
    ]
    audit_service.record(session, ctx, AuditAction.GROUP_LISTED, details={"count": len(groups)})
    return groups
