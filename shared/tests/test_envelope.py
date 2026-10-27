"""Clients built today must keep parsing responses from newer servers (FR-UPD-04)."""

import uuid

from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import DataResponse, ErrorResponse, ListResponse
from dropshare_shared.schemas.health import HealthOut
from dropshare_shared.schemas.sync import ChangeOut, ChangeType
from dropshare_shared.schemas.users import UserOut


def test_responses_tolerate_fields_added_by_newer_servers() -> None:
    parsed = DataResponse[HealthOut].model_validate(
        {"data": {"status": "ok", "uptime_s": 12}, "trace": "x"}
    )

    assert parsed.data.status == "ok"


def test_list_meta_tolerates_new_fields() -> None:
    parsed = ListResponse[int].model_validate(
        {"data": [1], "meta": {"page": 1, "limit": 50, "total": 1, "next": None}}
    )

    assert parsed.meta.total == 1


def test_known_error_code_parses_to_enum() -> None:
    parsed = ErrorResponse.model_validate(
        {"error": {"code": "NOT_FOUND", "message": "Not found", "details": None}}
    )

    assert parsed.error.code is ErrorCode.NOT_FOUND


def test_unknown_error_code_is_kept_as_text() -> None:
    parsed = ErrorResponse.model_validate(
        {"error": {"code": "SOME_FUTURE_CODE", "message": "Try again later", "extra": 1}}
    )

    assert parsed.error.code == "SOME_FUTURE_CODE"
    assert not isinstance(parsed.error.code, ErrorCode)
    assert parsed.error.message == "Try again later"


def test_response_enums_accept_values_from_a_newer_server() -> None:
    """A server may add a change type, node kind or upload status later (FR-UPD-04)."""
    change = ChangeOut.model_validate(
        {
            "cursor": 7,
            "type": "restored",
            "occurred_at": "2026-10-26T10:00:00Z",
            "node_id": str(uuid.uuid4()),
            "parent_id": None,
            "name": None,
            "kind": "symlink",
            "size_bytes": 0,
            "content_hash": None,
            "revision": 1,
            "modified_at": "2026-10-26T10:00:00Z",
            "modified_by_user_id": str(uuid.uuid4()),
        }
    )
    known = ChangeOut.model_validate({**change.model_dump(mode="json"), "type": "created"})

    assert (change.type, change.kind) == ("restored", "symlink")
    assert known.type is ChangeType.CREATED


def test_user_role_accepts_roles_from_a_newer_server() -> None:
    user = UserOut.model_validate(
        {
            "id": str(uuid.uuid4()),
            "username": "a",
            "email": "a@example.com",
            "display_name": "A",
            "role": "auditor",
            "quota_bytes": 1,
        }
    )

    assert user.role == "auditor"
