import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from dropshare_server.models import Group, GroupMember


def add(session: Session, group: Group) -> None:
    session.add(group)
    session.flush()


def get(session: Session, group_id: uuid.UUID) -> Group | None:
    return session.get(Group, group_id)


def get_by_name(session: Session, name_normalized: str) -> Group | None:
    return session.scalars(
        select(Group).where(Group.name_normalized == name_normalized)
    ).one_or_none()


def delete_group(session: Session, group: Group) -> None:
    session.execute(delete(GroupMember).where(GroupMember.group_id == group.id))
    session.delete(group)
    session.flush()


def is_member(session: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return session.get(GroupMember, (group_id, user_id)) is not None


def add_member(session: Session, member: GroupMember) -> None:
    session.add(member)
    session.flush()


def remove_member(session: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    member = session.get(GroupMember, (group_id, user_id))
    if member is None:
        return False
    session.delete(member)
    session.flush()
    return True


def member_ids(session: Session, group_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.scalars(select(GroupMember.user_id).where(GroupMember.group_id == group_id))
    )


def group_ids_of(session: Session, user_id: uuid.UUID) -> list[uuid.UUID]:
    return list(session.scalars(select(GroupMember.group_id).where(GroupMember.user_id == user_id)))


def list_with_counts(
    session: Session, name_prefix: str | None = None, limit: int | None = None
) -> list[tuple[Group, int]]:
    """Groups by name with their member counts; with `name_prefix` (normalized, LIKE-escaped),
    only those whose name starts with it."""
    count = (
        select(func.count())
        .select_from(GroupMember)
        .where(GroupMember.group_id == Group.id)
        .correlate(Group)
        .scalar_subquery()
    )
    query = select(Group, count).order_by(Group.name_normalized, Group.id)
    if name_prefix is not None:
        query = query.where(Group.name_normalized.like(f"{name_prefix}%", escape="\\"))
    if limit is not None:
        query = query.limit(limit)
    return [(group, members) for group, members in session.execute(query)]
