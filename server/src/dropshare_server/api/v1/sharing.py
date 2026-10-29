import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.context import Context
from dropshare_server.dependencies.resources import DbSession
from dropshare_server.services import sharing as sharing_service
from dropshare_shared.schemas.envelope import DataResponse, ListResponse, PageQuery
from dropshare_shared.schemas.nodes import NodeOut
from dropshare_shared.schemas.sharing import (
    MemberIn,
    MemberOut,
    MemberUpdateIn,
    MountIn,
    SharedItemOut,
    SharingOut,
)

router = APIRouter(tags=["sharing"])


@router.get("/nodes/{node_id}/members", response_model=DataResponse[SharingOut])
def get_members(
    node_id: uuid.UUID, current: ActiveSession, db: DbSession
) -> DataResponse[SharingOut]:
    return DataResponse(data=sharing_service.get_sharing(db, current.user.id, node_id))


@router.post("/nodes/{node_id}/members", status_code=201, response_model=DataResponse[MemberOut])
def add_member(
    node_id: uuid.UUID, body: MemberIn, current: ActiveSession, db: DbSession, ctx: Context
) -> DataResponse[MemberOut]:
    """Share a folder or file (FR-SHARE-01/03); only its owner may (FR-SHARE-04)."""
    member = sharing_service.add_member(
        db, ctx.as_actor(current.actor), current.user.id, node_id, body
    )
    return DataResponse(data=member)


@router.patch("/members/{member_id}", response_model=DataResponse[MemberOut])
def update_member(
    member_id: uuid.UUID,
    body: MemberUpdateIn,
    current: ActiveSession,
    db: DbSession,
    ctx: Context,
) -> DataResponse[MemberOut]:
    member = sharing_service.update_member(
        db, ctx.as_actor(current.actor), current.user.id, member_id, body
    )
    return DataResponse(data=member)


@router.delete("/members/{member_id}", status_code=204)
def remove_member(
    member_id: uuid.UUID, current: ActiveSession, db: DbSession, ctx: Context
) -> None:
    sharing_service.remove_member(db, ctx.as_actor(current.actor), current.user.id, member_id)


@router.get("/shared-with-me", response_model=ListResponse[SharedItemOut])
def shared_with_me(
    page: Annotated[PageQuery, Query()], current: ActiveSession, db: DbSession
) -> ListResponse[SharedItemOut]:
    items, meta = sharing_service.shared_with_me(db, current.user.id, page)
    return ListResponse(data=items, meta=meta)


@router.post("/mounts", status_code=201, response_model=DataResponse[NodeOut])
def add_mount(
    body: MountIn, current: ActiveSession, db: DbSession, ctx: Context
) -> DataResponse[NodeOut]:
    """Add a shared folder to the user's DropShare (FR-SHARE-02)."""
    node = sharing_service.mount(db, ctx.as_actor(current.actor), current.user.id, body)
    return DataResponse(data=node)


@router.delete("/mounts/{namespace_id}", status_code=204)
def remove_mount(
    namespace_id: uuid.UUID, current: ActiveSession, db: DbSession, ctx: Context
) -> None:
    """Leave a shared folder (FR-SHARE-02)."""
    sharing_service.unmount(db, ctx.as_actor(current.actor), current.user.id, namespace_id)
