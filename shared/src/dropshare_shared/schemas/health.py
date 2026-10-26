"""Health check responses (NFR-OPS-02)."""

from enum import StrEnum

from pydantic import BaseModel


class CheckStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


class HealthOut(BaseModel):
    status: CheckStatus


class ReadinessOut(BaseModel):
    status: CheckStatus
    database: CheckStatus
    storage: CheckStatus
