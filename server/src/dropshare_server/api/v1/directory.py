from typing import Annotated

from fastapi import APIRouter, Query

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.resources import DbSession
from dropshare_server.services import directory as directory_service
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.sharing import GroupHit, SearchQuery, UserHit

router = APIRouter(tags=["sharing"])


# Not paginated: at most SEARCH_RESULTS_MAX hits; the user types more to narrow it down.
@router.get("/users/search", response_model=DataResponse[list[UserHit]])
def search_users(
    query: Annotated[SearchQuery, Query()], _current: ActiveSession, db: DbSession
) -> DataResponse[list[UserHit]]:
    return DataResponse(data=directory_service.search_users(db, query))


@router.get("/groups/search", response_model=DataResponse[list[GroupHit]])
def search_groups(
    query: Annotated[SearchQuery, Query()], _current: ActiveSession, db: DbSession
) -> DataResponse[list[GroupHit]]:
    return DataResponse(data=directory_service.search_groups(db, query))
