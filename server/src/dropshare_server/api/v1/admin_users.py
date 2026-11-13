"""User administration for admins (FR-ADMIN-02, requirements §9 Admin)."""

import uuid

from fastapi import APIRouter

from dropshare_server.dependencies.admin import AdminSession, AdminWriteSession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession, Resources
from dropshare_server.services import admin_users as admin_service
from dropshare_shared.schemas.admin import (
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserUpdateIn,
    TemporaryPasswordOut,
)
from dropshare_shared.schemas.envelope import DataResponse

router = APIRouter(prefix="/admin/users", tags=["admin"])


@router.get("", response_model=DataResponse[list[AdminUserOut]])
def list_users(
    current: AdminSession, db: DbSession, ctx: Context
) -> DataResponse[list[AdminUserOut]]:
    return DataResponse(
        data=admin_service.list_users(db, ctx.as_actor(current.actor), current.user)
    )


@router.post("", status_code=201, response_model=DataResponse[TemporaryPasswordOut])
def create_user(
    body: AdminUserCreateIn,
    current: AdminWriteSession,
    db: DbSession,
    resources: Resources,
    ctx: Context,
) -> DataResponse[TemporaryPasswordOut]:
    """The temporary password is in this response only (FR-ADMIN-02)."""
    created = admin_service.create_user(
        db, ctx.as_actor(current.actor), resources.mailer, current.user, body
    )
    return DataResponse(data=created)


@router.patch("/{user_id}", response_model=DataResponse[AdminUserOut])
def update_user(
    user_id: uuid.UUID,
    body: AdminUserUpdateIn,
    current: AdminWriteSession,
    db: DbSession,
    ctx: Context,
) -> DataResponse[AdminUserOut]:
    updated = admin_service.update_user(
        db, ctx.as_actor(current.actor), current.user, user_id, body
    )
    return DataResponse(data=updated)


@router.post("/{user_id}/password-reset", response_model=DataResponse[TemporaryPasswordOut])
def reset_password(
    user_id: uuid.UUID,
    current: AdminWriteSession,
    db: DbSession,
    resources: Resources,
    ctx: Context,
) -> DataResponse[TemporaryPasswordOut]:
    reset = admin_service.reset_password(
        db, ctx.as_actor(current.actor), resources.mailer, current.user, user_id
    )
    return DataResponse(data=reset)
