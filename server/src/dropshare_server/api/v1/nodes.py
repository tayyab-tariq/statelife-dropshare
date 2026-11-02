import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from dropshare_server.api.v1._content import content_response, parse_range
from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Storage
from dropshare_server.services import nodes as node_service
from dropshare_shared.schemas.envelope import DataResponse, ListResponse, PageQuery
from dropshare_shared.schemas.nodes import FolderCreateIn, NodeDeleteQuery, NodeOut, NodeUpdateIn

router = APIRouter(prefix="/nodes", tags=["nodes"])


@router.post("", status_code=201, response_model=DataResponse[NodeOut])
def create_folder(
    body: FolderCreateIn, current: ActiveSession, db: DbSession, ctx: Context
) -> DataResponse[NodeOut]:
    folder = node_service.create_folder(db, ctx.as_actor(current.actor), current.user.id, body)
    return DataResponse(data=folder)


@router.get("/{node_id}", response_model=DataResponse[NodeOut])
def get_node(node_id: uuid.UUID, current: ActiveSession, db: DbSession) -> DataResponse[NodeOut]:
    return DataResponse(data=node_service.get_node(db, current.user.id, node_id))


@router.get("/{node_id}/children", response_model=ListResponse[NodeOut])
def list_children(
    node_id: uuid.UUID,
    page: Annotated[PageQuery, Query()],
    current: ActiveSession,
    db: DbSession,
) -> ListResponse[NodeOut]:
    children, meta = node_service.list_children(db, current.user.id, node_id, page)
    return ListResponse(data=children, meta=meta)


@router.patch("/{node_id}", response_model=DataResponse[NodeOut])
def update_node(
    node_id: uuid.UUID, body: NodeUpdateIn, current: ActiveSession, db: DbSession, ctx: Context
) -> DataResponse[NodeOut]:
    """Rename and/or move (FR-FILE-04, FR-SYNC-10)."""
    node = node_service.move_node(db, ctx.as_actor(current.actor), current.user.id, node_id, body)
    return DataResponse(data=node)


@router.delete("/{node_id}", status_code=204)
def trash_node(
    node_id: uuid.UUID,
    query: Annotated[NodeDeleteQuery, Query()],
    current: ActiveSession,
    db: DbSession,
    ctx: Context,
) -> Response:
    node_service.trash_node(
        db,
        ctx.as_actor(current.actor),
        current.user.id,
        node_id,
        query.if_revision,
        query.if_cursor,
        query.if_mount_cursor,
    )
    return Response(status_code=204)


@router.get("/{node_id}/content")
def download(
    node_id: uuid.UUID,
    request: Request,
    current: ActiveSession,
    db: DbSession,
    storage: Storage,
    ctx: Context,
) -> StreamingResponse:
    """The file's current content; honours one `Range: bytes=` range (FR-XFER-02)."""
    content = node_service.open_content(
        db,
        storage,
        ctx.as_actor(current.actor),
        current.user.id,
        node_id,
        parse_range(request.headers.get("Range")),
    )
    return content_response(content)
