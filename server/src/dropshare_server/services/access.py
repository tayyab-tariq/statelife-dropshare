"""Who may do what with a namespace or node. Every service touching a node goes through here.

v1 so far: the owner of a namespace has full access, nobody else has any. Sharing (roadmap F4)
adds namespace members and file shares here, without changing the callers.
"""

import uuid

from sqlalchemy.orm import Session

from dropshare_server.core.errors import AppError
from dropshare_server.models import Namespace, Node
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.nodes import AccessRole

_WRITE_ROLES = frozenset({AccessRole.OWNER, AccessRole.EDITOR})


def not_found(
    resource: str = "Item", *, denied: bool = False, target: tuple[str, str] | None = None
) -> AppError:
    """404 for missing items and, with `denied`, for other people's: never reveal which.
    A denial is audited with its `target` (type, id)."""
    return AppError(
        404, ErrorCode.NOT_FOUND, f"{resource} not found.", denied=denied, target=target
    )


def role_in(namespace: Namespace, user_id: uuid.UUID) -> AccessRole | None:
    return AccessRole.OWNER if namespace.owner_user_id == user_id else None


def namespace_role(
    session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID
) -> AccessRole | None:
    namespace = namespace_repository.get(session, namespace_id)
    return None if namespace is None else role_in(namespace, user_id)


def require_namespace(session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID) -> AccessRole:
    namespace = namespace_repository.get(session, namespace_id)
    if namespace is None:
        raise not_found("Namespace")
    role = role_in(namespace, user_id)
    if role is None:
        raise not_found("Namespace", denied=True, target=("namespace", str(namespace_id)))
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
) -> Node:
    """A node the user may read (or, with `write`, change); live unless `allow_trashed`.

    Access is checked before anything else, so probing another user's item, trashed or not,
    is always audited as a denial. `lock` locks the row only after access is confirmed.
    """
    node = node_repository.get(session, node_id)
    if node is None:
        raise not_found(resource)
    role = namespace_role(session, user_id, node.namespace_id)
    if role is None:
        raise not_found(resource, denied=True, target=("node", str(node_id)))
    if lock:
        node = node_repository.get_for_update(session, node_id)
        if node is None:
            raise not_found(resource)
    if node.trashed_at is not None and not allow_trashed:
        raise not_found(resource)
    if write and role not in _WRITE_ROLES:
        raise AppError(403, ErrorCode.FORBIDDEN, "You can view this item but not change it.")
    return node
