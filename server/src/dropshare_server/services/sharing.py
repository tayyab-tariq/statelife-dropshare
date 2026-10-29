"""Sharing folders and files, Shared with me, and mounts (FR-SHARE-01 … FR-SHARE-06).

Sharing a folder for the first time turns it into its own namespace, with the same node IDs,
and mounts it for the owner where it was (OQ-30). A recipient's accepted folder is a mount at the
top of their tree (FR-SHARE-02), which they may rename and move like any folder (OQ-31). Only the
owner manages sharing (FR-SHARE-04). When someone's access ends, their mount goes to the trash,
so their devices let the folder go at their next sync.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.models import FileShare, Group, Namespace, NamespaceMember, Node, User
from dropshare_server.repositories import groups as group_repository
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import sharing as sharing_repository
from dropshare_server.repositories import users as user_repository
from dropshare_server.repositories.nodes import LiveNameTakenError, MountTakenError
from dropshare_server.repositories.sharing import Share
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import changes as change_service
from dropshare_server.services import nodes as node_service
from dropshare_server.services.audit import AuditDetails, AuditTarget
from dropshare_server.services.context import CallContext
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.envelope import Meta, PageQuery
from dropshare_shared.schemas.nodes import AccessRole, NamespaceKind, NodeKind, NodeOut
from dropshare_shared.schemas.sharing import (
    GranteeKind,
    MemberIn,
    MemberOut,
    MemberUpdateIn,
    MountIn,
    SharedItemOut,
    SharingOut,
    UserHit,
)
from dropshare_shared.schemas.sync import ChangeType
from dropshare_shared.schemas.users import UserStatus

_FREE_NAME_TRIES = 100
_SHARED_FOLDER = "Shared folder"  # if the owner's own mount is ever missing


def _not_allowed(message: str) -> AppError:
    return AppError(409, ErrorCode.SHARE_NOT_ALLOWED, message)


def _owner_only() -> AppError:
    return AppError(403, ErrorCode.FORBIDDEN, "Only the owner can change who has access.")


@dataclass(frozen=True)
class _Target:
    """What the sharing dialog is about: a shared (or shareable) folder, or a file."""

    node: Node  # the folder's root in its shared namespace, the folder itself, or the file
    namespace: Namespace  # the namespace the node is in now
    role: AccessRole
    shared: bool  # a folder that is already its own namespace

    @property
    def is_file(self) -> bool:
        return self.node.kind == NodeKind.FILE


def _target(
    session: Session, user_id: uuid.UUID, node_id: uuid.UUID, *, reading: bool = False
) -> _Target:
    """The item behind `node_id`: a mount stands for its shared folder; a folder inside a
    shared folder is managed on that folder: refused for changes, and, when `reading`, that
    folder's sharing is what is shown."""
    node = access.require_node(session, user_id, node_id, follow_mount=True, file_share=True)
    namespace = namespace_repository.get(session, node.namespace_id)
    if namespace is None:
        raise RuntimeError(f"Node {node.id} belongs to a missing namespace")
    role = access.node_role(session, user_id, node, file_share=True)
    if role is None:  # require_node checked it
        raise access.not_found()
    if node.kind == NodeKind.FILE:
        return _Target(node, namespace, role, shared=False)
    if node.parent_id is None:
        if namespace.kind == NamespaceKind.SHARED:
            return _Target(node, namespace, role, shared=True)
        raise _not_allowed("Your DropShare folder itself can't be shared; share a folder in it.")
    if namespace.kind == NamespaceKind.SHARED:
        root = node_repository.get_root(session, namespace.id)
        if reading and root is not None:
            return _Target(root, namespace, role, shared=True)
        raise _not_allowed(
            "This folder is inside a shared folder; manage access on the shared folder itself."
        )
    return _Target(node, namespace, role, shared=False)


def _hit(user: User) -> UserHit:
    return UserHit(id=user.id, display_name=user.display_name, email=user.email)


def _user(session: Session, user_id: uuid.UUID) -> User:
    user = user_repository.get(session, user_id)
    if user is None:
        raise RuntimeError(f"User {user_id} is missing")
    return user


def _member_out(session: Session, share: Share) -> MemberOut:
    if share.user_id is not None:
        user = _user(session, share.user_id)
        return MemberOut(
            id=share.id,
            kind=GranteeKind.USER,
            user_id=user.id,
            group_id=None,
            name=user.display_name,
            email=user.email,
            role=share.role,
        )
    group = group_repository.get(session, share.group_id) if share.group_id else None
    if group is None:
        raise RuntimeError(f"Share {share.id} has no user or group")
    return MemberOut(
        id=share.id,
        kind=GranteeKind.GROUP,
        user_id=None,
        group_id=group.id,
        name=group.name,
        email=None,
        role=share.role,
    )


def _shares_of(session: Session, target: _Target) -> list[Share]:
    if target.is_file:
        return list(sharing_repository.file_members(session, target.node.id))
    if target.shared:
        return list(sharing_repository.namespace_members(session, target.namespace.id))
    return []


def folder_name(session: Session, namespace: Namespace) -> str:
    """A shared folder's name is its owner's name for it: their mount's."""
    personal = namespace_repository.get_personal(session, namespace.owner_user_id)
    mount = (
        None if personal is None else node_repository.live_mount(session, personal.id, namespace.id)
    )
    return mount.name if mount is not None and mount.name else _SHARED_FOLDER


def _audit_target(session: Session, target: _Target) -> AuditTarget:
    if target.is_file or not target.shared:
        return node_service.node_target(target.node)
    name = folder_name(session, target.namespace)
    return AuditTarget(type="node", id=str(target.node.id), name=name)


def get_sharing(session: Session, user_id: uuid.UUID, node_id: uuid.UUID) -> SharingOut:
    """Everyone with access, for anyone who has access (FR-SHARE-04). For a folder inside a
    shared folder, the shared folder's (its `node_id` says which)."""
    target = _target(session, user_id, node_id, reading=True)
    owner = _user(session, target.namespace.owner_user_id)
    return SharingOut(
        node_id=target.node.id,
        owner=_hit(owner),
        members=[_member_out(session, share) for share in _shares_of(session, target)],
        can_manage=target.role == AccessRole.OWNER,
    )


def _grantee(session: Session, owner_id: uuid.UUID, body: MemberIn) -> User | Group:
    if body.user_id is not None:
        user = user_repository.get(session, body.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise access.not_found("Person")
        if user.id == owner_id:
            raise _not_allowed("You already own this item.")
        return user
    group = group_repository.get(session, body.group_id) if body.group_id else None
    if group is None:
        raise access.not_found("Group")
    return group


def _grantee_details(share: Share) -> AuditDetails:
    if share.user_id is not None:
        return {"grantee_type": GranteeKind.USER.value, "grantee_id": str(share.user_id)}
    return {"grantee_type": GranteeKind.GROUP.value, "grantee_id": str(share.group_id)}


def add_member(
    session: Session, ctx: CallContext, user_id: uuid.UUID, node_id: uuid.UUID, body: MemberIn
) -> MemberOut:
    """Give a user or group a role on a folder or file. The first member of a folder turns it
    into a shared folder (`_share_folder`)."""
    target = _target(session, user_id, node_id)
    if target.role != AccessRole.OWNER:
        raise _owner_only()
    grantee = _grantee(session, target.namespace.owner_user_id, body)
    is_user = isinstance(grantee, User)
    now = datetime.now(UTC)
    user_ref = grantee.id if is_user else None
    group_ref = None if is_user else grantee.id
    taken: Share | None
    if target.is_file:
        share: Share = FileShare(node_id=target.node.id)
        target_id = target.node.id
        taken = sharing_repository.get_by_grantee(
            session, FileShare, target_id, user_ref, group_ref
        )
    else:
        if not target.shared:  # a new namespace has nobody in it yet
            target = _share_folder(session, user_id, target, now)
        share = NamespaceMember(namespace_id=target.namespace.id)
        target_id = target.namespace.id
        taken = sharing_repository.get_by_grantee(
            session, NamespaceMember, target_id, user_ref, group_ref
        )
    if taken is not None:
        raise AppError(
            409, ErrorCode.ALREADY_SHARED, "They already have access; change their role instead."
        )
    share.user_id, share.group_id, share.role = user_ref, group_ref, body.role
    share.created_at = share.updated_at = now
    share.created_by_user_id = user_id
    sharing_repository.add(session, share, target_id)
    audit_service.record(
        session,
        ctx,
        AuditAction.SHARE_CREATED,
        target=_audit_target(session, target),
        details={**_grantee_details(share), "role": body.role.value},
    )
    return _member_out(session, share)


def _share_folder(session: Session, user_id: uuid.UUID, target: _Target, now: datetime) -> _Target:
    """Turn a folder of the owner's own namespace into a shared folder, in one transaction:
    the folder and everything in it move to a new namespace with their IDs, and a mount takes
    the folder's place in the owner's tree (OQ-30). The owner's journal says the folder left
    (REMOVED) and the mount appeared (CREATED); the new namespace's journal lists every live
    item, parents first, so clients can read it from cursor 0.
    """
    personal = target.namespace
    # Moves in this namespace lock its root first (services/nodes.move_node): taking it here
    # too means no move can carry something into or out of the folder meanwhile.
    node_repository.lock_root(session, personal.id)
    folder = access.require_node(session, user_id, target.node.id, write=True, lock=True)
    live_ids = set(node_repository.lock_live_descendant_ids(session, folder.id))
    if node_repository.mounts_among(session, list(live_ids)):
        raise _not_allowed(
            "This folder contains a shared folder; folders can't be shared inside each other."
        )
    descendants = node_repository.subtree(session, folder.id)
    assert folder.parent_id is not None and folder.name is not None  # noqa: S101 - not a root
    parent_id, name, normalized = folder.parent_id, folder.name, folder.name_normalized

    folder.revision += 1
    change_service.record(session, folder, ChangeType.REMOVED, user_id, now)
    shared = Namespace(
        kind=NamespaceKind.SHARED, owner_user_id=personal.owner_user_id, created_at=now
    )
    namespace_repository.add(session, shared)
    node_repository.rehome(session, folder, descendants, shared.id)
    live = [folder, *(node for node in descendants if node.id in live_ids)]
    change_service.record_many(session, live, ChangeType.CREATED, user_id, now)

    mount = Node(
        namespace_id=personal.id,
        parent_id=parent_id,
        name=name,
        name_normalized=normalized,
        kind=NodeKind.FOLDER,
        size_bytes=0,
        revision=1,
        created_at=now,
        created_by_user_id=user_id,
        modified_at=now,
        modified_by_user_id=user_id,
        mount_namespace_id=shared.id,
    )
    node_service.add_live(session, mount)  # the folder's old place, freed by rehome
    change_service.record(session, mount, ChangeType.CREATED, user_id, now)
    return _Target(folder, shared, AccessRole.OWNER, shared=True)


def _require_member(
    session: Session, user_id: uuid.UUID, member_id: uuid.UUID
) -> tuple[Share, _Target]:
    share = sharing_repository.get(session, member_id)
    if share is None:
        raise access.not_found("Member")
    if isinstance(share, FileShare):
        node_id = share.node_id
    else:
        root = node_repository.get_root(session, share.namespace_id)
        if root is None:
            raise access.not_found("Member")
        node_id = root.id
    try:
        target = _target(session, user_id, node_id)
    except AppError as exc:
        if exc.status_code == 404:  # someone else's share: as if it didn't exist
            raise access.not_found(
                "Member", denied=True, target=("member", str(member_id))
            ) from None
        raise
    if target.role != AccessRole.OWNER:
        raise _owner_only()
    return share, target


def update_member(
    session: Session,
    ctx: CallContext,
    user_id: uuid.UUID,
    member_id: uuid.UUID,
    body: MemberUpdateIn,
) -> MemberOut:
    """Takes effect on the server at once, on clients at their next sync (FR-SHARE-04)."""
    share, target = _require_member(session, user_id, member_id)
    if share.role != body.role:
        old = share.role
        share.role, share.updated_at = body.role, datetime.now(UTC)
        session.flush()
        audit_service.record(
            session,
            ctx,
            AuditAction.SHARE_CHANGED,
            target=_audit_target(session, target),
            details={**_grantee_details(share), "old_role": old.value, "role": body.role.value},
        )
    return _member_out(session, share)


def remove_member(
    session: Session, ctx: CallContext, user_id: uuid.UUID, member_id: uuid.UUID
) -> None:
    share, target = _require_member(session, user_id, member_id)
    details = _grantee_details(share)
    affected = _people(session, share)
    sharing_repository.remove(session, share)
    audit_service.record(
        session,
        ctx,
        AuditAction.SHARE_REMOVED,
        target=_audit_target(session, target),
        details=details,
    )
    if isinstance(share, NamespaceMember):
        end_lost_mounts(session, affected, [share.namespace_id], actor_user_id=user_id)


def _people(session: Session, share: Share) -> list[uuid.UUID]:
    if share.user_id is not None:
        return [share.user_id]
    return group_repository.member_ids(session, share.group_id) if share.group_id else []


def end_lost_mounts(
    session: Session,
    user_ids: list[uuid.UUID],
    namespace_ids: list[uuid.UUID],
    *,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    """Trash the mounts of these shared folders held by people who no longer have any access to
    them, so their devices let the folders go at the next sync (FR-SHARE-04, OQ-33). Someone
    with access another way (directly and through a group) keeps theirs. The journal row's
    actor is `actor_user_id`, or the folder's owner when the CLI changed a group."""
    if not user_ids or not namespace_ids:
        return
    now = datetime.now(UTC)
    for namespace_id in namespace_ids:
        namespace = namespace_repository.get(session, namespace_id)
        if namespace is None:
            continue
        actor = actor_user_id or namespace.owner_user_id
        for user_id in user_ids:
            if access.role_in(session, namespace, user_id) is not None:
                continue
            personal = namespace_repository.get_personal(session, user_id)
            mount = (
                None
                if personal is None
                else node_repository.live_mount(session, personal.id, namespace_id)
            )
            if mount is None:
                continue
            mount = node_repository.get_for_update(session, mount.id) or mount
            mount.revision += 1
            node_repository.trash(session, mount, [], actor, now)
            change_service.record(session, mount, ChangeType.TRASHED, actor, now)


def shared_with_me(
    session: Session, user_id: uuid.UUID, page: PageQuery
) -> tuple[list[SharedItemOut], Meta]:
    """Folders and files other people share with the user, directly or through a group, oldest
    share first; for folders, whether they added it to their DropShare (FR-FILE-01)."""
    personal = namespace_repository.get_personal(session, user_id)
    items: dict[uuid.UUID, SharedItemOut] = {}
    for member in sharing_repository.namespace_memberships(session, user_id):
        namespace = namespace_repository.get(session, member.namespace_id)
        root = node_repository.get_root(session, member.namespace_id)
        if namespace is None or root is None or namespace.owner_user_id == user_id:
            continue
        role = access.role_in(session, namespace, user_id)
        if root.id in items or role is None:
            continue
        mount = (
            None
            if personal is None
            else node_repository.live_mount(session, personal.id, namespace.id)
        )
        items[root.id] = SharedItemOut(
            node_id=root.id,
            namespace_id=namespace.id,
            kind=NodeKind.FOLDER,
            name=folder_name(session, namespace),
            owner=_hit(_user(session, namespace.owner_user_id)),
            role=role,
            shared_at=member.created_at,
            size_bytes=0,
            modified_at=root.modified_at,
            mount_node_id=None if mount is None else mount.id,
        )
    for share in sharing_repository.file_shares_for(session, user_id):
        node = node_repository.get(session, share.node_id)
        if node is None or node.id in items:
            continue
        role = access.node_role(session, user_id, node, file_share=True)
        namespace = namespace_repository.get(session, node.namespace_id)
        if role is None or role == AccessRole.OWNER or namespace is None:
            continue  # their own file, or one they reach through a shared folder anyway
        items[node.id] = SharedItemOut(
            node_id=node.id,
            namespace_id=None,
            kind=NodeKind.FILE,
            name=node.name or "",
            owner=_hit(_user(session, namespace.owner_user_id)),
            role=role,
            shared_at=share.created_at,
            size_bytes=node.size_bytes,
            modified_at=node.modified_at,
            mount_node_id=None,
        )
    ordered = sorted(items.values(), key=lambda item: (item.shared_at, str(item.node_id)))
    start = (page.page - 1) * page.limit
    return ordered[start : start + page.limit], Meta(
        page=page.page, limit=page.limit, total=len(ordered)
    )


def mount(session: Session, ctx: CallContext, user_id: uuid.UUID, body: MountIn) -> NodeOut:
    """Add a shared folder to the top of the user's tree (FR-SHARE-02). Adding one already added
    returns that mount. A name in use gets " (1)", " (2)", … as uploads do."""
    namespace = namespace_repository.get(session, body.namespace_id)
    if namespace is None or namespace.kind != NamespaceKind.SHARED:
        raise access.not_found("Shared folder")
    if access.role_in(session, namespace, user_id) is None:
        raise access.not_found(
            "Shared folder", denied=True, target=("namespace", str(namespace.id))
        )
    personal = namespace_repository.get_personal(session, user_id)
    root = None if personal is None else node_repository.get_root(session, personal.id)
    if personal is None or root is None:
        raise RuntimeError(f"User {user_id} has no personal namespace")
    existing = node_repository.live_mount(session, personal.id, namespace.id)
    if existing is not None:
        return node_service.to_out(existing)
    wanted = body.name if body.name is not None else folder_name(session, namespace)
    name, _ = node_service.valid_name(wanted, ["body", "name"])
    now = datetime.now(UTC)
    for attempt in range(_FREE_NAME_TRIES):
        candidate = name if attempt == 0 else f"{name} ({attempt})"
        candidate, normalized = node_service.valid_name(candidate, ["body", "name"])
        if node_repository.get_live_child(session, personal.id, root.id, normalized) is not None:
            continue
        node = Node(
            namespace_id=personal.id,
            parent_id=root.id,
            name=candidate,
            name_normalized=normalized,
            kind=NodeKind.FOLDER,
            size_bytes=0,
            revision=1,
            created_at=now,
            created_by_user_id=user_id,
            modified_at=now,
            modified_by_user_id=user_id,
            mount_namespace_id=namespace.id,
        )
        try:
            node_repository.add(session, node)
        except LiveNameTakenError:
            continue  # another request took this name meanwhile: try the next number
        except MountTakenError:  # added from another device at the same moment
            raced = node_repository.live_mount(session, personal.id, namespace.id)
            if raced is None:
                raise
            return node_service.to_out(raced)
        change_service.record(session, node, ChangeType.CREATED, user_id, now)
        audit_service.record(
            session,
            ctx,
            AuditAction.SHARE_ACCEPTED,
            target=node_service.node_target(node),
            details={"namespace_id": str(namespace.id)},
        )
        return node_service.to_out(node)
    raise AppError(409, ErrorCode.NAME_CONFLICT, "Choose another name for this shared folder.")


def unmount(
    session: Session, ctx: CallContext, user_id: uuid.UUID, namespace_id: uuid.UUID
) -> None:
    """Leave a shared folder: only the user's mount goes, nobody else is affected
    (FR-SHARE-02). The owner can't leave their own folder."""
    personal = namespace_repository.get_personal(session, user_id)
    found = (
        None if personal is None else node_repository.live_mount(session, personal.id, namespace_id)
    )
    if found is None:
        raise access.not_found("Shared folder")
    namespace = namespace_repository.get(session, namespace_id)
    if namespace is not None and namespace.owner_user_id == user_id:
        raise _not_allowed("You own this folder; remove everyone's access instead of leaving it.")
    node = node_repository.get_for_update(session, found.id) or found
    now = datetime.now(UTC)
    node.revision += 1
    node_repository.trash(session, node, [], user_id, now)
    change_service.record(session, node, ChangeType.TRASHED, user_id, now)
    audit_service.record(
        session,
        ctx,
        AuditAction.SHARE_LEFT,
        target=node_service.node_target(node),
        details={"namespace_id": str(namespace_id)},
    )


def group_member_removed(session: Session, group_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Called by the group service: folders shared with the group may no longer be theirs."""
    end_lost_mounts(
        session, [user_id], sharing_repository.namespaces_shared_with_group(session, group_id)
    )


def group_deleted(session: Session, group_id: uuid.UUID, member_ids: list[uuid.UUID]) -> None:
    """Called by the group service before the group row goes: its shares go first (they
    reference it), and with them the access they gave."""
    namespace_ids = sharing_repository.namespaces_shared_with_group(session, group_id)
    sharing_repository.remove_group(session, group_id)
    end_lost_mounts(session, member_ids, namespace_ids)
