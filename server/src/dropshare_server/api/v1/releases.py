"""Client updates (FR-UPD-03, OQ-49). Reachable by clients below the minimum version."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from dropshare_server.api.v1._content import content_response, parse_range
from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.resources import DbSession, Storage
from dropshare_server.services import releases as release_service
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.releases import LatestReleaseQuery, ReleaseOut

router = APIRouter(prefix="/client/releases", tags=["releases"])


@router.get("/latest", response_model=DataResponse[ReleaseOut])
def latest_release(
    query: Annotated[LatestReleaseQuery, Query()], current: ActiveSession, db: DbSession
) -> DataResponse[ReleaseOut]:
    """The newest release on the user's channel; 404 RELEASE_NOT_FOUND if none."""
    return DataResponse(data=release_service.latest_for(db, current.user, query.platform))


@router.get("/{release_id}/installer")
def download_installer(
    release_id: uuid.UUID, request: Request, current: ActiveSession, db: DbSession, storage: Storage
) -> StreamingResponse:
    """The signed installer; honours one `Range: bytes=` range so downloads resume."""
    content = release_service.open_installer(
        db, storage, release_id, parse_range(request.headers.get("Range"))
    )
    return content_response(content)
