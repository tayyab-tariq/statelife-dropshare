from fastapi import APIRouter

from dropshare_server.dependencies.auth import ActiveSession
from dropshare_server.dependencies.resources import Resources
from dropshare_shared.schemas.envelope import DataResponse
from dropshare_shared.schemas.file_policy import FilePolicyOut

router = APIRouter(prefix="/file-policy", tags=["file-policy"])


@router.get("", response_model=DataResponse[FilePolicyOut])
def get_file_policy(_: ActiveSession, resources: Resources) -> DataResponse[FilePolicyOut]:
    """What the server accepts, so clients check before uploading (FR-SAFE-03). The server's
    own checks still decide (FR-SAFE-02)."""
    settings = resources.settings
    return DataResponse(
        data=FilePolicyOut(
            banned_extensions=sorted(resources.file_types.banned),
            max_file_size_bytes=settings.max_file_size_bytes,
        )
    )
