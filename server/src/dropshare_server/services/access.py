"""Who may do what with a namespace or node. Every service touching a node goes through here.

The one effective-role rule (FR-SHARE-01/03/04): the owner of a namespace has full access; on a
shared folder's namespace, a member (directly or through a group) has the highest role any of
their rows gives; a single shared file gives its role on that file only, and only where the
caller asks for it (`file_share`). Anyone else gets 404, so other people's items stay invisible.
"""

import uuid

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.models import Namespace, Node
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.repositories import sharing as sharing_repository
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.nodes import AccessRole, NamespaceKind
from dropshare_shared.schemas.sharing import ShareRole

_WRITE_ROLES = frozenset({AccessRole.OWNER, AccessRole.EDITOR})


def not_found(
    resource: str = "Item", *, denied: bool = False, target: tuple[str, str] | None = None
) -> AppError:
    """404 for missing items and, with `denied`, for other people's: never reveal which.
    A denial is audited with its `target` (type, id)."""
    return AppError(
        404, ErrorCode.NOT_FOUND, f"{resource} not found.", denied=denied, target=target
    )


def view_only() -> AppError:
    return AppError(403, ErrorCode.FORBIDDEN, "You can view this item but not change it.")


def _highest(roles: list[ShareRole]) -> AccessRole | None:
    if not roles:
        return None
    return AccessRole.EDITOR if ShareRole.EDITOR in roles else AccessRole.VIEWER


def role_in(session: Session, namespace: Namespace, user_id: uuid.UUID) -> AccessRole | None:
    if namespace.owner_user_id == user_id:
        return AccessRole.OWNER
    if namespace.kind != NamespaceKind.SHARED:
        return None
    return _highest(sharing_repository.namespace_roles(session, user_id, namespace.id))


def namespace_role(
    session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID
) -> AccessRole | None:
    namespace = namespace_repository.get(session, namespace_id)
    return None if namespace is None else role_in(session, namespace, user_id)


def require_namespace(session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID) -> AccessRole:
    namespace = namespace_repository.get(session, namespace_id)
    if namespace is None:
        raise not_found("Namespace")
    role = role_in(session, namespace, user_id)
    if role is None:
        raise not_found("Namespace", denied=True, target=("namespace", str(namespace_id)))
    return role


def node_role(
    session: Session, user_id: uuid.UUID, node: Node, *, file_share: bool = False
) -> AccessRole | None:
    role = namespace_role(session, user_id, node.namespace_id)
    if role is None and file_share:
        role = _highest(sharing_repository.file_roles(session, user_id, node.id))
    return role


def require_node(
    session: Session,
    user_id: uuid.UUID,
    node_id: uuid.UUID,
    *,
    write: bool = False,
    lock: bool = False,
    allow_trashed: bool = False,
    resource: str = "Item",
    follow_mount: bool = False,
    file_share: bool = False,
) -> Node:
    """A node the user may read (or, with `write`, change); live unless `allow_trashed`.

    Access is checked before anything else, so probing another user's item, trashed or not,
    is always audited as a denial. `lock` locks the row only after access is confirmed.

    `follow_mount`: a mount stands for the shared folder it shows, so listing it or adding to
    it means its namespace's root, with the role there (OQ-31). `file_share`: a single shared
    file counts too; only reading and uploading a new version allow it (OQ-32).
    """
    node = node_repository.get(session, node_id)
    if node is None:
        raise not_found(resource)
    role = node_role(session, user_id, node, file_share=file_share)
    if role is None:
        raise not_found(resource, denied=True, target=("node", str(node_id)))
    if follow_mount and node.mount_namespace_id is not None and node.trashed_at is None:
        root = node_repository.get_root(session, node.mount_namespace_id)
        role = namespace_role(session, user_id, node.mount_namespace_id)
        if root is None or role is None:  # access ended; the mount goes on the next sync
            raise not_found(resource, denied=True, target=("node", str(node_id)))
        node = root
    if lock:
        locked = node_repository.get_for_update(session, node.id)
        if locked is None:
            raise not_found(resource)
        node = locked
    if node.trashed_at is not None and not allow_trashed:
        raise not_found(resource)
    if write and role not in _WRITE_ROLES:
        raise view_only()
    return node
