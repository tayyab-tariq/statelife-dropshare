"""The trash (FR-TRASH-01 … FR-TRASH-04, requirements §9 Trash).

An item in the trash is the node a user deleted; what was inside it went to the trash with it
and comes back with it. Restoring keeps the IDs, so clients see the items created again.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from dropshare_shared.schemas.nodes import TolerantNodeKind


class TrashItemOut(BaseModel):
    id: uuid.UUID
    namespace_id: uuid.UUID
    name: str
    kind: TolerantNodeKind
    size_bytes: int  # a folder: the files deleted with it
    item_count: int  # items deleted with it (0 for a file)
    trashed_at: datetime
    trashed_by: str | None  # display name; None if that account no longer exists
    # The folder it was deleted from, "/"-separated from the top of My files or of the shared
    # folder; "" for the top itself.
    location: str
    # Set when it was deleted from a shared folder: that folder's name (OQ-37).
    shared_folder: str | None
    # Only the owner deletes permanently (FR-TRASH-03); an editor who deleted it may restore it.
    can_delete: bool


class EmptyTrashOut(BaseModel):
    removed: int  # items permanently deleted, of those the trash listed
    freed_bytes: int
    # More is left: one call deletes a limited number of items, so call again until False.
    more: bool = False
