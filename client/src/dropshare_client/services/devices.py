"""The account's signed-in devices (FR-AUTH-07). Blocking calls: controllers run them on the
pool."""

import uuid

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import ApiError
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.devices import DeviceOut
from dropshare_shared.schemas.envelope import DataResponse


class Devices:
    def __init__(self, api: ApiClient) -> None:
        self._api = api

    def list(self) -> list[DeviceOut]:
        """Most recently seen first; this device is marked `is_current`."""
        return self._api.envelope("GET", "/api/v1/me/devices", DataResponse[list[DeviceOut]]).data

    def revoke(self, device_id: uuid.UUID) -> None:
        """Signs the device out; it stops syncing on its next request. Already signed out
        counts as done."""
        try:
            self._api.no_content("DELETE", f"/api/v1/me/devices/{device_id}")
        except ApiError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise
