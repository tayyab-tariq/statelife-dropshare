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
    scanner: CheckStatus  # the malware scanner answers (FR-SAFE-01)
    # Hours since the scanner's signatures were published; None if it couldn't say.
    signatures_age_hours: float | None = None
