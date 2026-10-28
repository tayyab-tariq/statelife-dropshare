"""All /api/v1 routes. Every request must carry a supported X-Client-Version (FR-UPD-04)."""

from fastapi import APIRouter, Depends

from dropshare_server.api.v1 import (
    auth,
    changes,
    directory,
    me,
    namespaces,
    nodes,
    sharing,
    uploads,
)
from dropshare_server.dependencies.client_version import require_client_version

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_client_version)])
router.include_router(auth.router)
router.include_router(me.router)
router.include_router(namespaces.router)
router.include_router(nodes.router)
router.include_router(uploads.router)
router.include_router(changes.router)
router.include_router(directory.router)
