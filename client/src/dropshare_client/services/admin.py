"""User administration for admins (FR-ADMIN-02). Blocking calls: controllers run them on the
pool. The server decides who is an admin; a refusal comes back as ApiError FORBIDDEN."""

import uuid

from dropshare_client.services.api_client import ApiClient
from dropshare_shared.schemas.admin import (
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserUpdateIn,
    TemporaryPasswordOut,
)
from dropshare_shared.schemas.envelope import DataResponse

_USERS = "/api/v1/admin/users"


class AdminUsers:
    def __init__(self, api: ApiClient) -> None:
        self._api = api

    def list(self) -> list[AdminUserOut]:
        """Every account, enabled and disabled, by username."""
        return self._api.envelope("GET", _USERS, DataResponse[list[AdminUserOut]]).data

    def create(self, body: AdminUserCreateIn) -> TemporaryPasswordOut:
        return self._api.data(
            "POST", _USERS, TemporaryPasswordOut, json=body.model_dump(mode="json")
        )

    def update(self, user_id: uuid.UUID, body: AdminUserUpdateIn) -> AdminUserOut:
        return self._api.data(
            "PATCH",
            f"{_USERS}/{user_id}",
            AdminUserOut,
            json=body.model_dump(mode="json", exclude_none=True),
        )

    def reset_password(self, user_id: uuid.UUID) -> TemporaryPasswordOut:
        return self._api.data("POST", f"{_USERS}/{user_id}/password-reset", TemporaryPasswordOut)
