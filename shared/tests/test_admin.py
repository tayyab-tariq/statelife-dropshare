import uuid

import pytest
from pydantic import ValidationError

from dropshare_shared.schemas.admin import (
    QUOTA_MAX_BYTES,
    AdminUserCreateIn,
    AdminUserOut,
    AdminUserUpdateIn,
    EmailOutcome,
    TemporaryPasswordOut,
)
from dropshare_shared.schemas.users import UserRole, UserStatus

_CREATE = {
    "username": "bob",
    "email": "bob@example.com",
    "display_name": "Bob",
    "role": "user",
    "quota_bytes": 50 * 1024**3,
}


def test_create_accepts_a_complete_user() -> None:
    created = AdminUserCreateIn.model_validate(_CREATE)

    assert created.role is UserRole.USER


@pytest.mark.parametrize(
    "change",
    [
        {"role": "superuser"},
        {"quota_bytes": -1},
        {"quota_bytes": QUOTA_MAX_BYTES + 1},
        {"username": ""},
        {"display_name": ""},
        {"password": "chosen-by-the-admin"},  # unknown fields are refused
    ],
)
def test_create_refuses_bad_or_unknown_fields(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AdminUserCreateIn.model_validate({**_CREATE, **change})


def test_update_needs_something_to_change() -> None:
    with pytest.raises(ValidationError):
        AdminUserUpdateIn()

    assert AdminUserUpdateIn(status=UserStatus.DISABLED).role is None
    assert AdminUserUpdateIn(quota_bytes=0).quota_bytes == 0


def test_update_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AdminUserUpdateIn.model_validate({"role": "admin", "username": "renamed"})


def test_values_from_a_newer_server_still_parse() -> None:
    user = {
        "id": str(uuid.uuid4()),
        "username": "bob",
        "email": "bob@example.com",
        "display_name": "Bob",
        "role": "auditor",
        "quota_bytes": 1,
        "status": "suspended",
    }

    out = TemporaryPasswordOut.model_validate(
        {"user": user, "temporary_password": "x", "email": "queued"}
    )

    assert isinstance(out.user, AdminUserOut)
    assert out.user.status == "suspended"
    assert out.email == "queued"
    assert TemporaryPasswordOut.model_validate({**out.model_dump(), "email": "sent"}).email is (
        EmailOutcome.SENT
    )
