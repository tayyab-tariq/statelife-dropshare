"""Minimum supported client version (FR-UPD-04)."""

from typing import Annotated

from fastapi import Depends, Request

from dropshare_server.core.errors import AppError
from dropshare_server.dependencies.resources import Resources
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.error_details import UpgradeRequiredDetails
from dropshare_shared.version import CLIENT_VERSION_HEADER, parse_version


def require_client_version(request: Request, resources: Resources) -> str:
    """Every /api/v1 request carries X-Client-Version; too old means 426 UPGRADE_REQUIRED."""
    header = request.headers.get(CLIENT_VERSION_HEADER, "").strip()
    try:
        version = parse_version(header)
    except ValueError:
        raise AppError(
            400,
            ErrorCode.VALIDATION_ERROR,
            f"The {CLIENT_VERSION_HEADER} header is missing or not a version like 1.2.3.",
        ) from None
    minimum = resources.settings.min_client_version
    if version < parse_version(minimum):
        raise AppError(
            426,
            ErrorCode.UPGRADE_REQUIRED,
            "This version of DropShare is no longer supported. Install the update to continue.",
            UpgradeRequiredDetails(min_version=minimum).model_dump(mode="json"),
        )
    return header


ClientVersion = Annotated[str, Depends(require_client_version)]
