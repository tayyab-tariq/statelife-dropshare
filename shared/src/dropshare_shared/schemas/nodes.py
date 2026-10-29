"""Namespaces, files and folders (requirements §8, FR-FILE-04)."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Coarse bound on request size only; the real rules are dropshare_shared.naming (NFR-COMPAT-02),
# applied by the server so a rejected name comes back with its NameProblem code.
NAME_INPUT_MAX_LENGTH = 1024


class NamespaceKind(StrEnum):
    PERSONAL = "personal"
    SHARED = "shared"


class NodeKind(StrEnum):
    FILE = "file"
    FOLDER = "folder"


class AccessRole(StrEnum):
    """The caller's effective role on a namespace or node."""

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


# Response enums also accept values this client doesn't know yet, so a newer server can add
# one without breaking installed clients (FR-UPD-04). Clients treat an unknown value as
# "refetch this item" rather than failing.
type TolerantNamespaceKind = Annotated[NamespaceKind | str, Field(union_mode="left_to_right")]
type TolerantAccessRole = Annotated[AccessRole | str, Field(union_mode="left_to_right")]
type TolerantNodeKind = Annotated[NodeKind | str, Field(union_mode="left_to_right")]


class NamespaceOut(BaseModel):
    id: uuid.UUID
    kind: TolerantNamespaceKind
    root_node_id: uuid.UUID
    role: TolerantAccessRole


class NodeOut(BaseModel):
    id: uuid.UUID
    namespace_id: uuid.UUID
    parent_id: uuid.UUID | None  # None only for a namespace's root folder
    name: str | None  # None only for a namespace's root folder
    kind: TolerantNodeKind
    size_bytes: int  # 0 for folders
    content_hash: str | None  # hex SHA-256 of the current version; None for folders
    revision: int  # send back as the base revision of the next write (409 REVISION_CONFLICT)
    created_at: datetime
    modified_at: datetime
    modified_by_user_id: uuid.UUID
    # Set on a mount: this folder shows that shared namespace, whose root holds its contents
    # (OQ-31). Absent from servers from before sharing.
    mount_namespace_id: uuid.UUID | None = None


class FolderCreateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: uuid.UUID
    name: str = Field(min_length=1, max_length=NAME_INPUT_MAX_LENGTH)


class NodeUpdateIn(BaseModel):
    """`PATCH /nodes/{id}`: rename (`name`), move (`parent_id`), or both, within the node's
    namespace (FR-FILE-04, FR-SYNC-10). `if_revision` is the revision the client last saw."""

    model_config = ConfigDict(extra="forbid")

    if_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=NAME_INPUT_MAX_LENGTH)
    parent_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _changes_something(self) -> Self:
        if self.name is None and self.parent_id is None:
            raise ValueError("Give a new name, a new parent, or both.")
        return self


class NodeDeleteQuery(BaseModel):
    """`DELETE /nodes/{id}?if_revision=&if_cursor=`: the revision the client last saw, and
    optionally the namespace cursor it had read up to. With `if_cursor`, the delete is
    refused (409 REVISION_CONFLICT) if anything inside the folder changed after that cursor,
    so a file another device just added isn't taken to the trash with it (FR-CONF-03)."""

    model_config = ConfigDict(extra="forbid")

    if_revision: int = Field(ge=1)
    if_cursor: int | None = Field(default=None, ge=0)
    # Deleting your own shared folder (its mount): the cursor of the shared folder's journal you
    # read up to. Refused if anything inside changed after it, as `if_cursor` (OQ-36).
    if_mount_cursor: int | None = Field(default=None, ge=0)
