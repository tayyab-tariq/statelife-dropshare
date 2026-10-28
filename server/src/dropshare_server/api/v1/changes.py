import asyncio
import time

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.resources import DbSession, Resources
from dropshare_server.services import changes as change_service
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.sync import WaitIn, WaitOut

router = APIRouter(prefix="/changes", tags=["changes"])


@router.post("/wait", response_model=DataResponse[WaitOut])
async def wait_for_changes(
    body: WaitIn, request: Request, current: ActiveSession, db: DbSession, resources: Resources
) -> DataResponse[WaitOut]:
    """Long-poll (FR-SYNC-03): answer as soon as a namespace has news, or empty on timeout."""
    settings = resources.settings
    lost = await run_in_threadpool(
        change_service.prepare_wait, db, current.user.id, list(body.cursors)
    )
    if lost:
        return DataResponse(data=WaitOut(changed=lost))
    deadline = time.monotonic() + settings.long_poll_timeout_seconds
    while True:
        changed = await run_in_threadpool(
            change_service.changed_namespaces, resources.session_factory, body.cursors
        )
        if changed or time.monotonic() >= deadline or await request.is_disconnected():
            return DataResponse(data=WaitOut(changed=changed))
        await asyncio.sleep(min(settings.long_poll_interval_seconds, deadline - time.monotonic()))
