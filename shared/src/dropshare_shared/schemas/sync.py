"""Change journal and long-poll notifications (FR-SYNC-03, requirements §10.1)."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from dropshare_shared.schemas.nodes import TolerantNodeKind

CHANGES_LIMIT_DEFAULT = 500
CHANGES_LIMIT_MAX = 1000
WAIT_MAX_NAMESPACES = 100


class ChangeType(StrEnum):
    """Clients that don't know a type ask for that node (`GET /nodes/{id}`) and carry on, so
    new types can be added. Restoring from the trash (F3) must send CREATED for the restored
    item and for every item below it: clients dropped the whole subtree on TRASHED."""

    CREATED = "created"
    MODIFIED = "modified"
    # The node and everything below it went to the trash; descendants get no rows of their own.
    TRASHED = "trashed"
    # Renamed and/or moved to another parent. Descendants move with it and get no rows.
    MOVED = "moved"
    # Moved to another namespace (a shared folder, or out of one): gone from this one, not
    # trashed. The destination namespace has a CREATED row for it and each item below it.
    REMOVED = "removed"


class ChangesQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cursor: int = Field(default=0, ge=0, description="Last cursor already applied; 0 for all")
    limit: int = Field(default=CHANGES_LIMIT_DEFAULT, ge=1, le=CHANGES_LIMIT_MAX)


class ChangeOut(BaseModel):
    """One journal entry, with the node as it was right after the change.

    A `type` this client doesn't know (a newer server) means: refetch that node.
    """

    cursor: int  # per namespace, increasing in commit order
    type: Annotated[ChangeType | str, Field(union_mode="left_to_right")]
    occurred_at: datetime
    node_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str | None
    kind: TolerantNodeKind
    size_bytes: int
    content_hash: str | None
    revision: int
    modified_at: datetime
    modified_by_user_id: uuid.UUID
    mount_namespace_id: uuid.UUID | None = None  # set on a mount (NodeOut.mount_namespace_id)


class ChangesMeta(BaseModel):
    cursor: int  # store this and send it back as `cursor` next time
    has_more: bool


class ChangesResponse(BaseModel):
    data: list[ChangeOut]
    meta: ChangesMeta


class WaitIn(BaseModel):
    """`POST /changes/wait`: the last cursor the client has for each namespace it syncs."""

    model_config = ConfigDict(extra="forbid")

    cursors: dict[uuid.UUID, Annotated[int, Field(ge=0)]] = Field(
        min_length=1, max_length=WAIT_MAX_NAMESPACES
    )


class WaitOut(BaseModel):
    changed: list[uuid.UUID]  # empty when the wait timed out with nothing new
