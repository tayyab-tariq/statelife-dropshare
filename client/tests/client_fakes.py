"""Fakes shared by the client tests: a fake server, credential store and volumes, and a
client wired to them like app.main does."""

import json
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.platform.base import VolumeInfo, VolumeKind
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.auth import AuthService
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.tokens import TokenStore
from dropshare_shared.version import Version, parse_version

SERVER_URL = "http://127.0.0.1:8000"
USER_PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-password-42"


LOCKOUT_THRESHOLD = 5


@dataclass
class Account:
    password: str
    disabled: bool = False
    must_change: bool = False
    failures: int = 0


class FakeServer:
    """Just enough of the server's auth API, served through httpx.MockTransport, behaving
    like the real one where the client depends on it (server/services/auth.py): the version
    header, case-insensitive username-or-email sign-in, lockout with typed details, disabled
    accounts refused on refresh, and logout ending the device's access too."""

    def __init__(self) -> None:
        self.accounts: dict[str, Account] = {"alice": Account(password=USER_PASSWORD)}
        self.access: dict[str, str] = {}  # token -> username
        self.refresh: dict[str, str] = {}
        self.offline = False
        self.min_version: Version = (0, 0, 0)
        self.calls: list[str] = []
        self.hosts: list[str] = []
        self.me_offline = False  # /me alone unreachable, e.g. a drop right after a change
        # Requests to these paths wait until the event is set (to test "while pending").
        self.gates: dict[str, threading.Event] = {}
        self._next = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        self.hosts.append(f"{request.url.host}:{request.url.port}")
        gate = self.gates.get(request.url.path)
        if gate is not None:
            gate.wait(timeout=10)
        if self.offline or (self.me_offline and request.url.path == "/api/v1/me"):
            raise httpx.ConnectError("offline", request=request)
        try:
            client_version = parse_version(request.headers.get("X-Client-Version", ""))
        except ValueError:
            return _error(400, "VALIDATION_ERROR", "The X-Client-Version header is missing.")
        if client_version < self.min_version:
            minimum = ".".join(str(part) for part in self.min_version)
            return _error(426, "UPGRADE_REQUIRED", "Update DropShare.", {"min_version": minimum})
        route = {
            ("POST", "/api/v1/auth/login"): self._login,
            ("POST", "/api/v1/auth/refresh"): self._refresh,
            ("POST", "/api/v1/auth/logout"): self._logout,
            ("GET", "/api/v1/me"): self._me,
            ("POST", "/api/v1/me/password"): self._change_password,
        }.get((request.method, request.url.path))
        if route is None:
            return _error(404, "NOT_FOUND", "Not found")
        return route(request)

    def issue(self, username: str) -> dict[str, object]:
        self._next += 1
        access, refresh = f"access-{self._next}", f"refresh-{self._next}"
        self.access[access], self.refresh[refresh] = username, username
        return {
            "access_token": access,
            "refresh_token": refresh,
            "token_type": "bearer",
            "expires_in": 900,
            "must_change_password": self.accounts[username].must_change,
        }

    def _find(self, login: str) -> str | None:
        wanted = login.strip().casefold()
        for username in self.accounts:
            if wanted in (username.casefold(), f"{username}@example.com"):
                return username
        return None

    def _login(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        username = self._find(body["login"])
        if username is None:
            return _error(401, "UNAUTHENTICATED", "Invalid username or password.")
        account = self.accounts[username]
        if account.failures >= LOCKOUT_THRESHOLD:
            details = {"locked_until": "2026-10-27T10:15:00Z", "retry_after_seconds": 900}
            return _error(423, "ACCOUNT_LOCKED", "Too many attempts.", details)
        if account.password != body["password"]:
            account.failures += 1
            return _error(401, "UNAUTHENTICATED", "Invalid username or password.")
        account.failures = 0
        if account.disabled:
            return _error(403, "ACCOUNT_DISABLED", "Your account is disabled.")
        return _data(self.issue(username))

    def _refresh(self, request: httpx.Request) -> httpx.Response:
        username = self.refresh.pop(json.loads(request.content)["refresh_token"], None)
        # The real server answers 401 for unknown, expired, revoked and disabled alike.
        if username is None or self.accounts[username].disabled:
            return _error(401, "UNAUTHENTICATED", "Sign in to continue.")
        return _data(self.issue(username))

    def _user(self, request: httpx.Request) -> str | None:
        username = self.access.get(request.headers.get("Authorization", "").removeprefix("Bearer "))
        if username is None or self.accounts[username].disabled:
            return None
        return username

    def _me(self, request: httpx.Request) -> httpx.Response:
        username = self._user(request)
        if username is None:
            return _error(401, "UNAUTHENTICATED", "Sign in to continue.")
        if self.accounts[username].must_change:
            return _error(403, "PASSWORD_CHANGE_REQUIRED", "Change your password.")
        return _data(
            {
                "id": str(uuid.UUID(int=1)),
                "username": username,
                "email": f"{username}@example.com",
                "display_name": username.title(),
                "role": "user",
                "quota_bytes": 1024,
            }
        )

    def _change_password(self, request: httpx.Request) -> httpx.Response:
        username = self._user(request)
        if username is None:
            return _error(401, "UNAUTHENTICATED", "Sign in to continue.")
        body = json.loads(request.content)
        account = self.accounts[username]
        if body["current_password"] != account.password:
            issue = {
                "loc": ["body", "current_password"],
                "msg": "Your current password is incorrect.",
                "type": "password_incorrect",
            }
            return _error(400, "VALIDATION_ERROR", "The request is not valid", [issue])
        if body["new_password"] == body["current_password"]:
            issue = {
                "loc": ["body", "new_password"],
                "msg": "Choose a password different from the current one.",
                "type": "password_reused",
            }
            return _error(400, "VALIDATION_ERROR", "The request is not valid", [issue])
        account.password, account.must_change = body["new_password"], False
        self.access = {t: u for t, u in self.access.items() if u != username}
        self.refresh = {t: u for t, u in self.refresh.items() if u != username}
        return _data(self.issue(username))

    def _logout(self, request: httpx.Request) -> httpx.Response:
        username = self._user(request)
        if username is None:
            return _error(401, "UNAUTHENTICATED", "Sign in to continue.")
        # The real server revokes the device: its access token stops working at once too.
        self.refresh = {t: u for t, u in self.refresh.items() if u != username}
        self.access = {t: u for t, u in self.access.items() if u != username}
        return httpx.Response(204)


def _data(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": data}, headers={"X-Request-ID": "req-1"})


def _error(status: int, code: str, message: str, details: object = None) -> httpx.Response:
    body = {"error": {"code": code, "message": message, "details": details}}
    return httpx.Response(status, json=body, headers={"X-Request-ID": "req-1"})


class MemoryCredentials:
    def __init__(self) -> None:
        self.secret: str | None = None

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = None


class FakeVolumes:
    """Fixed NTFS everywhere, except `network/` below `root` and another sync tool's folder."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def describe(self, path: Path) -> VolumeInfo:
        if path.is_relative_to(self.root / "network"):
            return VolumeInfo(VolumeKind.NETWORK, None)
        return VolumeInfo(VolumeKind.FIXED, "NTFS")

    def supported_file_systems(self) -> frozenset[str]:
        return frozenset({"NTFS"})

    def other_sync_roots(self) -> list[Path]:
        return [self.root / "OneDrive"]


@dataclass
class Client:
    """One client install, wired like app.main but against the fake server."""

    server: FakeServer
    credentials: MemoryCredentials
    config: ConfigStore
    tokens: TokenStore
    api: ApiClient
    auth: AuthService
    session: SessionController
    folders: SyncFolderController
    trash: Path
    app_data: Path
