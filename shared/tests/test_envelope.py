"""Clients built today must keep parsing responses from newer servers (FR-UPD-04)."""

from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.envelope import DataResponse, ErrorResponse, ListResponse
from dropshare_shared.schemas.health import HealthOut


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
