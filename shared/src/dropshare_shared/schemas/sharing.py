"""Sharing folders and files with users and groups (FR-SHARE-01 … FR-SHARE-08).

A shared folder is its own namespace. Each person who has it in their tree (the owner, and every
recipient who accepted it) has a mount: a folder node in their own namespace whose
`mount_namespace_id` points at it (OQ-30, OQ-31).
"""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dropshare_shared.schemas.nodes import (
    NAME_INPUT_MAX_LENGTH,
    TolerantNodeKind,
)

SEARCH_MIN_LENGTH = 2
SEARCH_MAX_LENGTH = 100
SEARCH_RESULTS_MAX = 20


class ShareRole(StrEnum):
    """The roles an owner can give (FR-SHARE-01); `owner` itself is never granted."""

    VIEWER = "viewer"
    EDITOR = "editor"


class GranteeKind(StrEnum):
    USER = "user"
    GROUP = "group"


type TolerantShareRole = Annotated[ShareRole | str, Field(union_mode="left_to_right")]
type TolerantGranteeKind = Annotated[GranteeKind | str, Field(union_mode="left_to_right")]


class SearchQuery(BaseModel):
    """`GET /users/search?q=` and `GET /groups/search?q=` (FR-SHARE-08): matches the start of a
    name (or username, or email), ignoring case."""

    model_config = ConfigDict(extra="forbid")

    q: str = Field(min_length=SEARCH_MIN_LENGTH, max_length=SEARCH_MAX_LENGTH)


class UserHit(BaseModel):
    """Only what the share dialog shows: never username, role, status or quota."""

    id: uuid.UUID
    display_name: str
    email: str


class GroupHit(BaseModel):
    id: uuid.UUID
    name: str
    member_count: int


class MemberIn(BaseModel):
    """`POST /nodes/{id}/members`: give one user or one group a role on a folder or file."""

    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    role: ShareRole

    @model_validator(mode="after")
    def _one_grantee(self) -> Self:
        if (self.user_id is None) == (self.group_id is None):
            raise ValueError("Give either a user or a group.")
        return self


class MemberUpdateIn(BaseModel):
    """`PATCH /members/{id}`: change the role."""

    model_config = ConfigDict(extra="forbid")

    role: ShareRole


class MemberOut(BaseModel):
    """One person or group with access. `id` is what `PATCH`/`DELETE /members/{id}` take."""

    id: uuid.UUID
    kind: TolerantGranteeKind
    user_id: uuid.UUID | None  # set when kind is user
    group_id: uuid.UUID | None  # set when kind is group
    name: str  # display name, or group name
    email: str | None  # users only
    role: TolerantShareRole


class SharingOut(BaseModel):
    """`GET /nodes/{id}/members`: the owner, everyone with access, and what the caller may do."""

    node_id: uuid.UUID
    owner: UserHit
    members: list[MemberOut]
    can_manage: bool  # only the owner manages sharing in v1 (FR-SHARE-04)


class SharedItemOut(BaseModel):
    """One entry of `GET /shared-with-me`: a folder (with its namespace) or a single file."""

    node_id: uuid.UUID
    namespace_id: uuid.UUID | None  # folders only: what `POST /mounts` takes
    kind: TolerantNodeKind
    name: str  # the owner's name for it
    owner: UserHit
    role: TolerantShareRole
    shared_at: datetime
    size_bytes: int  # 0 for folders
    modified_at: datetime
    # Folders only: the user's mount of it, if they added it to their DropShare (FR-SHARE-02).
    mount_node_id: uuid.UUID | None


class MountIn(BaseModel):
    """`POST /mounts`: add a shared folder to the top of the user's own tree. Without `name`,
    the owner's name is used; a name already taken gets " (1)", " (2)", … as with uploads."""

    model_config = ConfigDict(extra="forbid")

    namespace_id: uuid.UUID
    name: str | None = Field(default=None, min_length=1, max_length=NAME_INPUT_MAX_LENGTH)
