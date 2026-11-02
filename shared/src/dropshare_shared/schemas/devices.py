"""Signed-in devices (FR-AUTH-07, requirements §9 Me)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    os_name: str
    client_version: str  # as of its last sign-in or token refresh
    created_at: datetime  # signed in
    last_seen_at: datetime  # updated at sign-in and each token refresh (about every 15 min)
    is_current: bool  # the device making this request
