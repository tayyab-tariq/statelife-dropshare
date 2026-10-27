import re
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Storage
from dropshare_server.services import nodes as node_service
from dropshare_shared.schemas.envelope import DataResponse, ListResponse, PageQuery
from dropshare_shared.schemas.nodes import FolderCreateIn, NodeDeleteQuery, NodeOut

router = APIRouter(prefix="/nodes", tags=["nodes"])

# At most 19 digits (fits in 64 bits): longer values would make int() fail (audit S10).
_SINGLE_RANGE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")


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
        _parse_range(request.headers.get("Range")),
    )
    headers = {
        "Content-Length": str(content.end - content.start + 1),
        "Accept-Ranges": "bytes",
        "ETag": f'"{content.sha256}"',
        "Content-Disposition": _attachment(content.name),
        "X-Content-Type-Options": "nosniff",
    }
    if content.partial:
        headers["Content-Range"] = f"bytes {content.start}-{content.end}/{content.size_bytes}"
    return StreamingResponse(
        content.chunks,
        status_code=206 if content.partial else 200,
        media_type="application/octet-stream",
        headers=headers,
    )


def _parse_range(header: str | None) -> tuple[int | None, int | None] | None:
    """One `bytes=first-last`, `bytes=first-` or `bytes=-suffix` range. Anything else (several
    ranges, other units, first > last) is ignored and the whole file is sent (RFC 9110 §14.2)."""
    match = _SINGLE_RANGE.match((header or "").strip())
    if match is None or match.group(1) == match.group(2) == "":
        return None
    first = int(match.group(1)) if match.group(1) else None
    last = int(match.group(2)) if match.group(2) else None
    if first is not None and last is not None and first > last:
        return None
    return first, last


def _attachment(name: str) -> str:
    """RFC 6266 / 5987: an ASCII fallback plus the exact UTF-8 name."""
    fallback = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in name
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
