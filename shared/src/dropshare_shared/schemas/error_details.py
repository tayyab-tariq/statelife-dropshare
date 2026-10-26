"""Typed `error.details` for errors the client must act on, not just display."""

from datetime import datetime

from pydantic import BaseModel


class AccountLockedDetails(BaseModel):
    """423 ACCOUNT_LOCKED (FR-AUTH-06). Also sent as the Retry-After header."""

    locked_until: datetime
    retry_after_seconds: int


class UpgradeRequiredDetails(BaseModel):
    """426 UPGRADE_REQUIRED (FR-UPD-04)."""

    min_version: str
