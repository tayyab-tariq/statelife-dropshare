from fastapi import APIRouter

from dropshare_server.dependencies.auth import ActiveSession, AnySession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Resources
from dropshare_server.services import auth as auth_service
from dropshare_server.services import users as user_service
from dropshare_shared.schemas.auth import PasswordChangeIn, TokenPairOut
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.users import UserOut

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=DataResponse[UserOut])
def get_me(current: ActiveSession) -> DataResponse[UserOut]:
    return DataResponse(data=user_service.to_out(current.user))


@router.post("/password", response_model=DataResponse[TokenPairOut])
def change_password(
    body: PasswordChangeIn,
    current: AnySession,  # allowed while the temporary password is still in use
    db: DbSession,
    resources: Resources,
    ctx: Context,
) -> DataResponse[TokenPairOut]:
    pair = auth_service.change_password(
        db,
        resources.session_factory,
        resources.settings,
        ctx.as_actor(current.actor),
        current,
        body,
    )
    return DataResponse(data=pair)
