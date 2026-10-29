"""Change journal and long-poll support (FR-SYNC-03, requirements §10.1)."""

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import Change, Node
from dropshare_server.repositories import changes as change_repository
from dropshare_server.repositories import namespaces as namespace_repository
from dropshare_server.repositories import nodes as node_repository
from dropshare_server.services import access
from dropshare_shared.schemas.sync import (
    ChangeOut,
    ChangesMeta,
    ChangesQuery,
    ChangesResponse,
    ChangeType,
)


def record(
    session: Session, node: Node, change_type: ChangeType, actor_user_id: uuid.UUID, at: datetime
) -> None:
    """Append a journal row in the caller's transaction, so it commits with the node change.

    The cursor is the namespace's next sequence number, taken with the namespace row locked
    until commit: writers to one namespace therefore commit in cursor order, and a client that
    has seen cursor N can never later miss a change numbered below N (audit B1). Callers take
    this lock last, after any node or user locks, so lock order stays acyclic.
    """
    record_many(session, [node], change_type, actor_user_id, at)


def record_many(
    session: Session,
    nodes: Sequence[Node],
    change_type: ChangeType,
    actor_user_id: uuid.UUID,
    at: datetime,
) -> None:
    """`record` for several nodes of one namespace, in this order, under one lock."""
    if not nodes:
        return
    namespace_id = nodes[0].namespace_id
    if any(node.namespace_id != namespace_id for node in nodes):
        raise ValueError("record_many takes the nodes of one namespace")
    namespace = namespace_repository.get_for_update(session, namespace_id)
    if namespace is None:
        raise RuntimeError(f"Namespace {namespace_id} is missing")
    first = namespace.last_change_seq + 1
    namespace.last_change_seq += len(nodes)
    change_repository.add_all(
        session,
        [
            Change(
                namespace_id=namespace_id,
                seq=first + offset,
                node_id=node.id,
                change_type=change_type,
                occurred_at=at,
                actor_user_id=actor_user_id,
                parent_id=node.parent_id,
                name=node.name,
                kind=node.kind,
                size_bytes=node.size_bytes,
                content_hash=node.content_hash,
                revision=node.revision,
                modified_at=node.modified_at,
                modified_by_user_id=node.modified_by_user_id,
            )
            for offset, node in enumerate(nodes)
        ],
    )


def _to_out(change: Change, mount_namespace_id: uuid.UUID | None) -> ChangeOut:
    return ChangeOut(
        cursor=change.seq,
        type=change.change_type,
        occurred_at=change.occurred_at,
        node_id=change.node_id,
        parent_id=change.parent_id,
        name=change.name,
        kind=change.kind,
        size_bytes=change.size_bytes,
        content_hash=change.content_hash,
        revision=change.revision,
        modified_at=change.modified_at,
        modified_by_user_id=change.modified_by_user_id,
        mount_namespace_id=mount_namespace_id,
    )


def list_changes(
    session: Session, user_id: uuid.UUID, namespace_id: uuid.UUID, query: ChangesQuery
) -> ChangesResponse:
    access.require_namespace(session, user_id, namespace_id)
    rows = change_repository.list_after(session, namespace_id, query.cursor, query.limit + 1)
    page = rows[: query.limit]
    return ChangesResponse(
        data=[_to_out(change, mount) for change, mount in page],
        meta=ChangesMeta(
            cursor=page[-1][0].seq if page else query.cursor, has_more=len(rows) > len(page)
        ),
    )


def prepare_wait(
    session: Session, user_id: uuid.UUID, namespace_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Check access to every namespace, then end the transaction. Returns the namespaces the
    user can no longer read but had in their tree (a shared folder whose access ended): the wait
    answers with them at once, so the client lists its namespaces again and lets the folder go
    (FR-SHARE-04). Any other namespace without access is 404, as if it didn't exist.

    Ending the transaction returns the pooled connection: a long-poll holds for ~55 s, and
    keeping a connection that long per waiting client would exhaust the pool.
    """
    personal = namespace_repository.get_personal(session, user_id)
    lost = []
    for namespace_id in namespace_ids:
        if access.namespace_role(session, user_id, namespace_id) is not None:
            continue
        if personal is None or not node_repository.ever_mounted(session, personal.id, namespace_id):
            access.require_namespace(session, user_id, namespace_id)  # 404, a denial if it exists
        lost.append(namespace_id)
    session.commit()
    return sorted(lost, key=str)


def changed_namespaces(
    session_factory: sessionmaker[Session], cursors: Mapping[uuid.UUID, int]
) -> list[uuid.UUID]:
    """Namespaces with a change newer than the client's cursor, each in a short transaction."""
    with session_factory() as session:
        latest = change_repository.latest_cursors(session, list(cursors))
    return sorted(
        (
            namespace_id
            for namespace_id, cursor in cursors.items()
            if latest.get(namespace_id, 0) > cursor
        ),
        key=str,
    )
