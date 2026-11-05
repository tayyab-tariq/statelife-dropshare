"""All /api/v1 routes. Every request carries a valid X-Client-Version, and all but token refresh
and the release endpoints require a supported one (FR-UPD-04, OQ-49). A new router is included
with `_SUPPORTED`; tests check that every route checks the version."""

from fastapi import APIRouter, Depends

from dropshare_server.api.v1 import (
    auth,
    changes,
    directory,
    file_policy,
    me,
    namespaces,
    nodes,
    releases,
    sharing,
    trash,
    uploads,
)
from dropshare_server.dependencies.client_version import (
    require_client_header,
    require_client_version,
)

_SUPPORTED = [Depends(require_client_version)]

router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_client_header)])
router.include_router(auth.router)  # login and logout check per route; refresh accepts any
router.include_router(me.router, dependencies=_SUPPORTED)
router.include_router(namespaces.router, dependencies=_SUPPORTED)
router.include_router(nodes.router, dependencies=_SUPPORTED)
router.include_router(uploads.router, dependencies=_SUPPORTED)
router.include_router(changes.router, dependencies=_SUPPORTED)
router.include_router(directory.router, dependencies=_SUPPORTED)
router.include_router(sharing.router, dependencies=_SUPPORTED)
router.include_router(trash.router, dependencies=_SUPPORTED)
router.include_router(file_policy.router, dependencies=_SUPPORTED)
router.include_router(releases.router)  # any version: an unsupported client fetches its update
