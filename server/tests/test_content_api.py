"""Downloads with Range support (FR-XFER-02, FR-AUDIT-01)."""

import hashlib
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from dropshare_server.models import AuditEvent
from dropshare_shared.schemas.audit import AuditAction

type Account = dict[str, str]
type SignIn = Callable[..., Account]
type RootOf = Callable[[Account], str]
type UploadFile = Callable[..., Response]
type AuditEvents = Callable[..., list[AuditEvent]]

CONTENT = b"0123456789"


@pytest.fixture
def alice_file(
    sign_in: SignIn, root_of: RootOf, upload_file: UploadFile
) -> tuple[Account, dict[str, object]]:
    alice = sign_in()
    node = upload_file(alice, CONTENT, parent_id=root_of(alice), name="Résumé 2026.txt")
    return alice, node.json()["data"]


def _get(api: TestClient, user: Account, node_id: object, range_: str | None = None) -> Response:
    headers = {"Authorization": user["authorization"]}
    if range_ is not None:
        headers["Range"] = range_
    response: Response = api.get(f"/api/v1/nodes/{node_id}/content", headers=headers)
    return response


def test_download_returns_the_whole_file(
    api: TestClient, alice_file: tuple[Account, dict[str, object]], audit_events: AuditEvents
) -> None:
    alice, node = alice_file

    response = _get(api, alice, node["id"])

    assert response.status_code == 200
    assert response.content == CONTENT
    assert response.headers["Content-Length"] == "10"
    assert response.headers["Accept-Ranges"] == "bytes"
    assert response.headers["ETag"] == f'"{hashlib.sha256(CONTENT).hexdigest()}"'
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Content-Disposition"] == (
        "attachment; filename=\"R_sum_ 2026.txt\"; filename*=UTF-8''R%C3%A9sum%C3%A9%202026.txt"
    )
    [event] = audit_events(AuditAction.FILE_DOWNLOADED)
    assert event.target_id == node["id"]


@pytest.mark.parametrize(
    ("range_", "body", "content_range"),
    [
        ("bytes=2-5", b"2345", "bytes 2-5/10"),
        ("bytes=7-", b"789", "bytes 7-9/10"),  # resume from the last byte received
        ("bytes=-3", b"789", "bytes 7-9/10"),
        ("bytes=8-100", b"89", "bytes 8-9/10"),
    ],
)
def test_range_requests_return_partial_content(
    api: TestClient,
    alice_file: tuple[Account, dict[str, object]],
    range_: str,
    body: bytes,
    content_range: str,
) -> None:
    alice, node = alice_file

    response = _get(api, alice, node["id"], range_)

    assert response.status_code == 206
    assert (response.content, response.headers["Content-Range"]) == (body, content_range)
    assert response.headers["Content-Length"] == str(len(body))


@pytest.mark.parametrize("range_", ["bytes=10-", "bytes=10-12", "bytes=-0"])
def test_unsatisfiable_range_is_416(
    api: TestClient, alice_file: tuple[Account, dict[str, object]], range_: str
) -> None:
    alice, node = alice_file

    response = _get(api, alice, node["id"], range_)

    assert response.status_code == 416
    assert response.json()["error"]["code"] == "RANGE_NOT_SATISFIABLE"
    assert response.headers["Content-Range"] == "bytes */10"


@pytest.mark.parametrize("range_", ["bytes=5-2", "bytes=0-1,4-5", "items=0-1", "bytes=-"])
def test_ranges_we_dont_serve_fall_back_to_the_whole_file(
    api: TestClient, alice_file: tuple[Account, dict[str, object]], range_: str
) -> None:
    alice, node = alice_file

    response = _get(api, alice, node["id"], range_)

    assert (response.status_code, response.content) == (200, CONTENT)


def test_folders_have_no_content(api: TestClient, sign_in: SignIn, root_of: RootOf) -> None:
    alice = sign_in()

    assert _get(api, alice, root_of(alice)).status_code == 400


def test_another_users_file_is_not_found(
    api: TestClient,
    sign_in: SignIn,
    alice_file: tuple[Account, dict[str, object]],
    audit_events: AuditEvents,
) -> None:
    _, node = alice_file
    bob = sign_in("bob")

    response = _get(api, bob, node["id"])

    assert response.status_code == 404
    assert audit_events(AuditAction.FILE_DOWNLOADED) == []
    assert len(audit_events(AuditAction.ACCESS_DENIED)) == 1


def test_absurdly_long_range_numbers_are_ignored(
    api: TestClient, alice_file: tuple[Account, dict[str, object]]
) -> None:
    alice, node = alice_file

    response = _get(api, alice, node["id"], "bytes=" + "9" * 5000 + "-")

    assert (response.status_code, response.content) == (200, CONTENT)
