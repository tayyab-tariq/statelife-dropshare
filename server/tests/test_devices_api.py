"""Listing and revoking signed-in devices (FR-AUTH-07, FR-AUDIT-01)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.models import AuditEvent, Device
from dropshare_shared.schemas.audit import AuditAction, AuditOutcome

type Login = Callable[..., Response]
type SignIn = Callable[..., dict[str, str]]
type AuditEvents = Callable[..., list[AuditEvent]]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _second_device(login: Login, user: dict[str, str], name: str = "Desktop") -> dict[str, str]:
    response = login(user["username"], user["password"], device=name)
    assert response.status_code == 200, response.text
    tokens: dict[str, str] = response.json()["data"]
    return tokens


def _devices(api: TestClient, access_token: str) -> list[dict[str, object]]:
    response = api.get("/api/v1/me/devices", headers=_bearer(access_token))
    assert response.status_code == 200, response.text
    devices: list[dict[str, object]] = response.json()["data"]
    return devices


def test_lists_own_devices_with_the_current_one_marked(
    api: TestClient, signed_in: dict[str, str], login: Login, sign_in: SignIn
) -> None:
    desktop = _second_device(login, signed_in)
    sign_in("bob")  # another user's device is never listed

    devices = _devices(api, desktop["access_token"])

    assert [(d["name"], d["is_current"]) for d in devices] == [
        ("Desktop", True),
        ("Laptop", False),
    ]
    assert set(devices[0]) == {
        "id",
        "name",
        "os_name",
        "client_version",
        "created_at",
        "last_seen_at",
        "is_current",
    }
    assert devices[0]["os_name"] == "Windows 11"


def test_revoked_and_long_unseen_devices_are_not_listed(
    api: TestClient,
    signed_in: dict[str, str],
    login: Login,
    session_factory: sessionmaker[Session],
) -> None:
    _second_device(login, signed_in, "Old PC")
    _second_device(login, signed_in, "Signed out")
    long_ago = datetime.now(UTC) - timedelta(days=31)
    with session_factory.begin() as session:
        session.execute(update(Device).where(Device.name == "Old PC").values(last_seen_at=long_ago))
        session.execute(
            update(Device).where(Device.name == "Signed out").values(revoked_at=long_ago)
        )

    assert [d["name"] for d in _devices(api, signed_in["access_token"])] == ["Laptop"]


def test_revoking_a_device_ends_its_session_and_is_audited(
    api: TestClient, signed_in: dict[str, str], login: Login, audit_events: AuditEvents
) -> None:
    desktop = _second_device(login, signed_in)
    (listed,) = [d for d in _devices(api, signed_in["access_token"]) if d["name"] == "Desktop"]

    response = api.delete(
        f"/api/v1/me/devices/{listed['id']}", headers=_bearer(signed_in["access_token"])
    )

    assert response.status_code == 204
    # Its access token stops working on the next request, and it can't refresh.
    assert api.get("/api/v1/me", headers=_bearer(desktop["access_token"])).status_code == 401
    refreshed = api.post("/api/v1/auth/refresh", json={"refresh_token": desktop["refresh_token"]})
    assert refreshed.status_code == 401
    # The revoking device keeps working.
    assert api.get("/api/v1/me", headers=_bearer(signed_in["access_token"])).status_code == 200
    (event,) = audit_events(AuditAction.AUTH_DEVICE_REVOKED)
    assert (event.target_type, event.target_id, event.target_name) == (
        "device",
        listed["id"],
        "Desktop",
    )
    assert [d["name"] for d in _devices(api, signed_in["access_token"])] == ["Laptop"]


def test_revoking_the_current_device_signs_it_out(
    api: TestClient, signed_in: dict[str, str]
) -> None:
    (current,) = _devices(api, signed_in["access_token"])

    response = api.delete(
        f"/api/v1/me/devices/{current['id']}", headers=_bearer(signed_in["access_token"])
    )

    assert response.status_code == 204
    assert api.get("/api/v1/me", headers=_bearer(signed_in["access_token"])).status_code == 401


def test_another_users_unknown_or_revoked_device_is_not_found(
    api: TestClient,
    signed_in: dict[str, str],
    sign_in: SignIn,
    login: Login,
    audit_events: AuditEvents,
) -> None:
    bob = sign_in("bob")
    (bobs_device,) = _devices(api, bob["access_token"])
    desktop = _second_device(login, signed_in)
    (listed,) = [d for d in _devices(api, desktop["access_token"]) if d["is_current"]]
    assert (
        api.delete(
            f"/api/v1/me/devices/{listed['id']}", headers=_bearer(signed_in["access_token"])
        ).status_code
        == 204
    )

    for device_id in (bobs_device["id"], "00000000-0000-0000-0000-000000000000", listed["id"]):
        response = api.delete(
            f"/api/v1/me/devices/{device_id}", headers=_bearer(signed_in["access_token"])
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"
    # Bob is still signed in.
    assert api.get("/api/v1/me", headers=_bearer(bob["access_token"])).status_code == 200
    # Only trying Bob's device is a denial (FR-AUDIT-01); unknown and revoked ones aren't.
    (denied,) = audit_events(AuditAction.ACCESS_DENIED)
    assert (denied.target_type, denied.target_id) == ("device", bobs_device["id"])
    assert denied.outcome is AuditOutcome.DENIED


def test_a_malformed_device_id_is_a_validation_error(
    api: TestClient, signed_in: dict[str, str]
) -> None:
    response = api.delete(
        "/api/v1/me/devices/not-a-uuid", headers=_bearer(signed_in["access_token"])
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_device_endpoints_require_sign_in(api: TestClient) -> None:
    listed = api.get("/api/v1/me/devices")
    revoked = api.delete("/api/v1/me/devices/00000000-0000-0000-0000-000000000000")

    assert (listed.status_code, revoked.status_code) == (401, 401)
