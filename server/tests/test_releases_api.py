"""Client update endpoints and the version check (FR-UPD-03, FR-UPD-04, OQ-49)."""

import hashlib
import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app
from dropshare_server.services import releases as release_service
from dropshare_server.services import users as user_service
from dropshare_server.services.context import cli_context
from dropshare_server.services.releases import MinimumVersion, NewRelease
from dropshare_server.storage.local import LocalFilesystemBackend
from dropshare_shared.schemas.releases import ReleaseChannel, ReleasePlatform
from dropshare_shared.signing import ExpectedSigner, Signer

from .conftest import SignIn

PUBLISHER = "StateLife Test"
LATEST = "/api/v1/client/releases/latest?platform=windows-x64"
TOO_OLD = {"X-Client-Version": "0.0.1"}

type Publish = Callable[..., str]


CERT = "ab" * 32


class _Verifier:
    def __init__(self, version: str) -> None:
        self._version = version

    def signer(self, path: Path) -> Signer:
        return Signer(PUBLISHER, CERT)

    def file_version(self, path: Path) -> str:
        return self._version


@pytest.fixture
def publish(
    session_factory: sessionmaker[Session], db_settings: Settings, tmp_path: Path
) -> Publish:
    """publish(version, channel=, minimum=): publish an installer whose bytes name its
    version; returns the release id."""

    def run(
        version: str, channel: ReleaseChannel = ReleaseChannel.STABLE, minimum: str = "0.1.0"
    ) -> str:
        content = f"installer {version} {channel}".encode()
        installer = tmp_path / f"{version}-{channel}.exe"
        installer.write_bytes(content)
        release = NewRelease(
            platform=ReleasePlatform.WINDOWS_X64,
            channel=channel,
            version=version,
            min_supported_version=minimum,
            notes=f"Notes for {version}",
            sha256=hashlib.sha256(content).hexdigest(),
        )
        with session_factory.begin() as session:
            out = release_service.publish(
                session,
                LocalFilesystemBackend(db_settings.storage_local_root),
                _Verifier(version),
                ExpectedSigner.of(PUBLISHER, [CERT]),
                cli_context("it-admin"),
                release,
                installer,
            )
        return str(out.id)

    return run


def _withdraw(session_factory: sessionmaker[Session], version: str) -> None:
    with session_factory.begin() as session:
        release_service.withdraw(
            session,
            cli_context("it-admin"),
            ReleasePlatform.WINDOWS_X64,
            ReleaseChannel.STABLE,
            version,
        )


def _beta(session_factory: sessionmaker[Session], username: str) -> None:
    with session_factory.begin() as session:
        user_service.set_update_channel(
            session, cli_context("it-admin"), username, ReleaseChannel.BETA
        )


def _auth(user: dict[str, str]) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def test_nothing_published_is_release_not_found(api: TestClient, sign_in: SignIn) -> None:
    response = api.get(LATEST, headers=_auth(sign_in()))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RELEASE_NOT_FOUND"


def test_stable_users_get_stable_and_beta_users_the_newest(
    api: TestClient, sign_in: SignIn, publish: Publish, session_factory: sessionmaker[Session]
) -> None:
    publish("1.2.0")
    publish("1.3.0", ReleaseChannel.BETA)
    alice, bob = sign_in("alice"), sign_in("bob")
    _beta(session_factory, "bob")

    stable = api.get(LATEST, headers=_auth(alice)).json()["data"]
    beta = api.get(LATEST, headers=_auth(bob)).json()["data"]
    me = api.get("/api/v1/me", headers=_auth(bob)).json()["data"]

    assert (stable["version"], stable["channel"], stable["notes"]) == (
        "1.2.0",
        "stable",
        "Notes for 1.2.0",
    )
    assert (beta["version"], beta["channel"]) == ("1.3.0", "beta")
    assert me["update_channel"] == "beta"

    publish("1.4.0")  # a stable release newer than the beta is offered to beta users too
    assert api.get(LATEST, headers=_auth(bob)).json()["data"]["version"] == "1.4.0"


def test_a_withdrawn_release_is_neither_offered_nor_downloadable(
    api: TestClient, sign_in: SignIn, publish: Publish, session_factory: sessionmaker[Session]
) -> None:
    publish("1.2.0")
    newer = publish("1.3.0")
    user = sign_in()
    _withdraw(session_factory, "1.3.0")

    assert api.get(LATEST, headers=_auth(user)).json()["data"]["version"] == "1.2.0"
    gone = api.get(f"/api/v1/client/releases/{newer}/installer", headers=_auth(user))
    assert (gone.status_code, gone.json()["error"]["code"]) == (404, "RELEASE_NOT_FOUND")


def test_the_installer_downloads_whole_or_from_a_byte(
    api: TestClient, sign_in: SignIn, publish: Publish
) -> None:
    release_id = publish("1.2.0")
    user = sign_in()
    url = f"/api/v1/client/releases/{release_id}/installer"
    content = b"installer 1.2.0 stable"

    whole = api.get(url, headers=_auth(user))
    rest = api.get(url, headers={**_auth(user), "Range": "bytes=10-"})
    beyond = api.get(url, headers={**_auth(user), "Range": "bytes=999-"})

    assert (whole.status_code, whole.content) == (200, content)
    assert whole.headers["ETag"] == f'"{hashlib.sha256(content).hexdigest()}"'
    assert "DropShare-1.2.0-setup.exe" in whole.headers["Content-Disposition"]
    assert (rest.status_code, rest.content) == (206, content[10:])
    assert rest.headers["Content-Range"] == f"bytes 10-{len(content) - 1}/{len(content)}"
    assert beyond.status_code == 416


@pytest.mark.parametrize(
    "url",
    [LATEST, f"/api/v1/client/releases/{uuid.uuid4()}/installer"],
)
def test_release_endpoints_need_sign_in(api: TestClient, url: str) -> None:
    assert api.get(url).status_code == 401


def test_an_unknown_platform_is_a_validation_error(api: TestClient, sign_in: SignIn) -> None:
    response = api.get("/api/v1/client/releases/latest?platform=linux", headers=_auth(sign_in()))

    assert response.status_code == 400


def test_a_client_too_old_to_sync_can_still_refresh_and_update(
    api: TestClient, sign_in: SignIn, publish: Publish
) -> None:
    release_id = publish("1.2.0")
    user = sign_in()

    blocked = api.get("/api/v1/me", headers={**_auth(user), **TOO_OLD})
    refreshed = api.post(
        "/api/v1/auth/refresh", json={"refresh_token": user["refresh_token"]}, headers=TOO_OLD
    )
    access = {"Authorization": f"Bearer {refreshed.json()['data']['access_token']}", **TOO_OLD}
    latest = api.get(LATEST, headers=access)
    installer = api.get(f"/api/v1/client/releases/{release_id}/installer", headers=access)

    assert blocked.status_code == 426
    assert refreshed.status_code == 200
    assert latest.status_code == 200
    assert installer.status_code == 200
    assert api.post("/api/v1/auth/logout", headers=access).status_code == 426


def test_a_stable_release_raises_the_minimum_version(
    db_settings: Settings, sign_in: SignIn, publish: Publish
) -> None:
    publish("1.0.0", minimum="1.0.0")
    publish("2.0.0", ReleaseChannel.BETA, minimum="2.0.0")  # a beta's minimum doesn't count
    # A fresh app: the minimum is read when first needed.
    with TestClient(create_app(db_settings), raise_server_exceptions=False) as client:
        old = client.get("/api/v1/me", headers={"X-Client-Version": "0.9.0"})
        current = client.get("/api/v1/me", headers={"X-Client-Version": "1.0.0"})

    assert old.status_code == 426
    assert old.json()["error"]["details"] == {"min_version": "1.0.0"}
    assert current.status_code == 401  # past the version check, then not signed in


def test_the_minimum_is_reread_after_a_minute(
    session_factory: sessionmaker[Session], publish: Publish
) -> None:
    now = [0.0]
    minimum = MinimumVersion("0.2.0", session_factory, clock=lambda: now[0])
    assert minimum.current() == "0.2.0"

    publish("1.0.0", minimum="1.0.0")
    now[0] = 59
    assert minimum.current() == "0.2.0"
    now[0] = 60
    assert minimum.current() == "1.0.0"

    _withdraw(session_factory, "1.0.0")  # withdrawing lowers it again; never below the setting
    now[0] = 120
    assert minimum.current() == "0.2.0"


class _Unreachable:
    """A session factory whose database can't be reached."""

    def __call__(self) -> Session:
        raise OperationalError("SELECT", {}, Exception("connection refused"))


def test_the_last_minimum_holds_while_the_database_cant_be_read(
    session_factory: sessionmaker[Session], publish: Publish
) -> None:
    publish("1.0.0", minimum="1.0.0")
    now = [0.0]
    minimum = MinimumVersion("0.2.0", session_factory, clock=lambda: now[0])
    assert minimum.current() == "1.0.0"

    minimum._sessions = cast(sessionmaker[Session], _Unreachable())
    now[0] = 60

    assert minimum.current() == "1.0.0"


_ANY_VERSION = {
    ("post", "/api/v1/auth/refresh"),
    ("get", "/api/v1/client/releases/latest"),
    ("get", "/api/v1/client/releases/{release_id}/installer"),
}


def test_every_route_refuses_unsupported_clients_except_refresh_and_updates(
    api: TestClient, offline_settings: Settings
) -> None:
    """Behavioural, so a new router included without the check fails here: the version is
    checked before sign-in and before the request is validated."""
    paths = create_app(offline_settings).openapi()["paths"]  # not served: docs are off
    checked = 0
    for path, operations in paths.items():
        if not path.startswith("/api/v1/"):
            continue
        url = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        for method in operations:
            response = api.request(method.upper(), url, headers=TOO_OLD)
            if (method, path) in _ANY_VERSION:
                assert response.status_code != 426, (method, path)
            else:
                assert response.status_code == 426, (method, path, response.status_code)
                checked += 1
    assert checked > 20
