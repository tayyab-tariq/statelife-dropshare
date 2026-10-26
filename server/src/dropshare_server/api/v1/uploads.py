import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Request, Response
from starlette.concurrency import run_in_threadpool

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Resources, Storage
from dropshare_server.services import uploads as upload_service
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.nodes import NodeOut
from dropshare_shared.schemas.uploads import CHUNK_SHA256_HEADER, UploadCreateIn, UploadOut

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("", status_code=201, response_model=DataResponse[UploadOut])
def start_upload(
    body: UploadCreateIn, current: ActiveSession, db: DbSession, resources: Resources
) -> DataResponse[UploadOut]:
    return DataResponse(data=upload_service.start(db, resources.settings, current.user.id, body))


@router.get("/{upload_id}", response_model=DataResponse[UploadOut])
def get_upload(
    upload_id: uuid.UUID, current: ActiveSession, db: DbSession, storage: Storage
) -> DataResponse[UploadOut]:
    return DataResponse(data=upload_service.get(db, storage, current.user.id, upload_id))


@router.put("/{upload_id}/chunks/{index}", status_code=204)
async def put_chunk(
    upload_id: uuid.UUID,
    index: Annotated[int, Path(ge=0)],
    request: Request,
    current: ActiveSession,
    db: DbSession,
    storage: Storage,
) -> Response:
    """Raw chunk bytes in the body, their SHA-256 in X-Chunk-SHA256. Re-sending is safe."""
    user_id = current.user.id
    expected = await run_in_threadpool(upload_service.chunk_limit, db, user_id, upload_id, index)
    # Never buffer more than one chunk: stop reading as soon as the body is too long.
    data = bytearray()
    async for piece in request.stream():
        data += piece
        if len(data) > expected:
            raise upload_service.wrong_chunk_size(index, expected)
    await run_in_threadpool(
        upload_service.put_chunk,
        db,
        storage,
        user_id,
        upload_id,
        index,
        bytes(data),
        request.headers.get(CHUNK_SHA256_HEADER),
    )
    return Response(status_code=204)


@router.post("/{upload_id}/complete", response_model=DataResponse[NodeOut])
def complete_upload(
    upload_id: uuid.UUID,
    current: ActiveSession,
    db: DbSession,
    resources: Resources,
    ctx: Context,
) -> DataResponse[NodeOut]:
    node = upload_service.complete(
        db,
        resources.session_factory,
        resources.storage,
        ctx.as_actor(current.actor),
        current.user.id,
        upload_id,
    )
    return DataResponse(data=node)


@router.delete("/{upload_id}", status_code=204)
def cancel_upload(
    upload_id: uuid.UUID, current: ActiveSession, db: DbSession, storage: Storage
) -> Response:
    upload_service.cancel(db, storage, current.user.id, upload_id)
    return Response(status_code=204)
