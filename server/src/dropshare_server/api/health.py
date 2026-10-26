"""Liveness and readiness probes, outside /api/v1 and without auth (NFR-OPS-02, NFR-SEC-02)."""

from fastapi import APIRouter

from dropshare_server.dependencies.resources import DbSession, Storage
from dropshare_server.services import health as health_service
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.health import CheckStatus, HealthOut, ReadinessOut

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", response_model=DataResponse[HealthOut])
def liveness() -> DataResponse[HealthOut]:
    return DataResponse(data=HealthOut(status=CheckStatus.OK))


@router.get("/ready", response_model=DataResponse[ReadinessOut])
def readiness(db: DbSession, storage: Storage) -> DataResponse[ReadinessOut]:
    return DataResponse(data=health_service.check_readiness(db, storage))
