import uuid
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Query, Response

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Storage
from dropshare_server.services import blobs as blob_service
from dropshare_server.services import trash as trash_service
from dropshare_shared.schemas.envelope import DataResponse, ListResponse, PageQuery
from dropshare_shared.schemas.nodes import NodeOut
from dropshare_shared.schemas.trash import EmptyTrashOut, TrashItemOut

router = APIRouter(prefix="/trash", tags=["trash"])


@router.get("", response_model=ListResponse[TrashItemOut])
def list_trash(
    page: Annotated[PageQuery, Query()], current: ActiveSession, db: DbSession
) -> ListResponse[TrashItemOut]:
    items, meta = trash_service.list_trash(db, current.user.id, page)
    return ListResponse(data=items, meta=meta)


@router.post("/{node_id}/restore", response_model=DataResponse[NodeOut])
def restore(
    node_id: uuid.UUID, current: ActiveSession, db: DbSession, ctx: Context
) -> DataResponse[NodeOut]:
    """Put an item back where it was (FR-TRASH-02)."""
    node = trash_service.restore(db, ctx.as_actor(current.actor), current.user.id, node_id)
    return DataResponse(data=node)


# The blobs go in a background task: those run after the response, so after the database
# session has committed (DbSession), and a purge that rolled back never loses content.


@router.delete("/{node_id}", status_code=204)
def delete_forever(
    node_id: uuid.UUID,
    current: ActiveSession,
    db: DbSession,
    storage: Storage,
    ctx: Context,
    background: BackgroundTasks,
) -> Response:
    """Permanently delete one item (FR-TRASH-03); only its owner may."""
    purged = trash_service.delete_forever(db, ctx.as_actor(current.actor), current.user.id, node_id)
    background.add_task(blob_service.delete_blobs, storage, purged.blob_keys)
    return Response(status_code=204)


@router.delete("", response_model=DataResponse[EmptyTrashOut])
def empty_trash(
    current: ActiveSession,
    db: DbSession,
    storage: Storage,
    ctx: Context,
    background: BackgroundTasks,
) -> DataResponse[EmptyTrashOut]:
    result, keys = trash_service.empty_trash(db, ctx.as_actor(current.actor), current.user.id)
    background.add_task(blob_service.delete_blobs, storage, keys)
    return DataResponse(data=result)
