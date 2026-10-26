"""Response envelope used by every endpoint (requirements §9).

Response models keep pydantic's default `extra="ignore"` so an installed client still parses
responses after the server adds fields (FR-UPD-04). Only request models use `extra="forbid"`.
"""

from typing import Annotated

from pydantic import BaseModel, Field

from dropshare_shared.errors import ErrorCode


class Meta(BaseModel):
    page: int
    limit: int
    total: int


class DataResponse[T](BaseModel):
    data: T


class ListResponse[T](BaseModel):
    data: list[T]
    meta: Meta


class ValidationIssue(BaseModel):
    """One entry of 400 VALIDATION_ERROR `details`: which field (`loc`, e.g. ["body",
    "new_password"]), a message to show, and a stable `type` the client can branch on."""

    loc: list[str | int]
    msg: str
    type: str


class ErrorBody(BaseModel):
    # Known codes parse to ErrorCode; codes newer than this client stay plain strings, so the
    # client can still show `message` and fall back to the HTTP status.
    code: Annotated[ErrorCode | str, Field(union_mode="left_to_right")]
    message: str
    details: dict[str, object] | list[object] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody
