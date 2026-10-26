import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.resources import DbSession
from dropshare_server.services import changes as change_service
from dropshare_server.services import nodes as node_service
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.nodes import NamespaceOut
from dropshare_shared.schemas.sync import ChangesQuery, ChangesResponse

router = APIRouter(prefix="/namespaces", tags=["namespaces"])


@router.get("", response_model=DataResponse[list[NamespaceOut]])
def list_namespaces(current: ActiveSession, db: DbSession) -> DataResponse[list[NamespaceOut]]:
    # Not paginated: a user has one personal namespace plus their mounted shared folders.
    return DataResponse(data=node_service.list_namespaces(db, current.user.id))


@router.get("/{namespace_id}/changes", response_model=ChangesResponse)
def list_changes(
    namespace_id: uuid.UUID,
    query: Annotated[ChangesQuery, Query()],
    current: ActiveSession,
    db: DbSession,
) -> ChangesResponse:
    return change_service.list_changes(db, current.user.id, namespace_id, query)
