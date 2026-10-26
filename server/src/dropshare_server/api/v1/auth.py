from fastapi import APIRouter, Response

from dropshare_server.dependencies.auth import AnySession
from dropshare_server.dependencies.client_version import ClientVersion
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Resources
from dropshare_server.services import auth as auth_service
from dropshare_shared.schemas.auth import LoginIn, RefreshIn, TokenPairOut
from dropshare_shared.schemas.envelope import DataResponse

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=DataResponse[TokenPairOut])
def login(
    body: LoginIn, db: DbSession, resources: Resources, ctx: Context, version: ClientVersion
) -> DataResponse[TokenPairOut]:
    pair = auth_service.login(db, resources.session_factory, resources.settings, ctx, body, version)
    return DataResponse(data=pair)


@router.post("/refresh", response_model=DataResponse[TokenPairOut])
def refresh(
    body: RefreshIn, db: DbSession, resources: Resources, ctx: Context, version: ClientVersion
) -> DataResponse[TokenPairOut]:
    pair = auth_service.refresh(db, resources.settings, ctx, body.refresh_token, version)
    return DataResponse(data=pair)


@router.post("/logout", status_code=204)
def logout(current: AnySession, db: DbSession, ctx: Context) -> Response:
    auth_service.logout(db, ctx.as_actor(current.actor), current)
    return Response(status_code=204)
