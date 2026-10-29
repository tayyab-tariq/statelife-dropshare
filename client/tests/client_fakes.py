"""Fakes shared by the client tests: a fake server, credential store and volumes, and a
client wired to them like app.main does."""

import json
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import ValidationError

from dropshare_client.controllers.browser_controller import BrowserController
from dropshare_client.controllers.file_actions_controller import FileActionsController
from dropshare_client.controllers.session_controller import SessionController
from dropshare_client.controllers.share_controller import ShareController
from dropshare_client.controllers.shared_controller import SharedController
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.sync_folder_controller import SyncFolderController
from dropshare_client.controllers.transfers_controller import TransfersController
from dropshare_client.controllers.upload_controller import UploadController
from dropshare_client.platform.base import VolumeInfo, VolumeKind
from dropshare_client.platform.macos.attributes import MacFileAttributes
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.auth import AuthService
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.tokens import TokenStore
from dropshare_client.store.index import Index
from dropshare_client.sync import binding
from dropshare_client.sync.engine import SyncListener, SyncState
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.envelope import DataResponse, ListResponse, Meta, PageQuery
from dropshare_shared.schemas.nodes import NodeKind, NodeOut
from dropshare_shared.schemas.sharing import (
    GroupHit,
    MemberIn,
    MemberOut,
    MemberUpdateIn,
    MountIn,
    SharedItemOut,
    SharingOut,
    UserHit,
)
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
    quota_bytes: int = 10**12
    used_bytes: int = 0


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
        # Alice's files for the browser: folder ID -> its children, as the server lists them.
        self.namespace_id = uuid.uuid4()
        self.root_id = uuid.uuid4()
        self.children: dict[uuid.UUID, list[NodeOut]] = {self.root_id: []}
        self.files: set[uuid.UUID] = set()
        # Sharing (server services/sharing.py): people and groups to find, who has access to
        # what, and what is shared with Alice.
        self.people: list[UserHit] = []
        self.groups: list[GroupHit] = []
        self.members: dict[uuid.UUID, list[MemberOut]] = {}
        self.can_manage = True
        self.shared: list[SharedItemOut] = []
        # Nodes Alice may read that aren't in any listed folder (a shared folder's root).
        self.extra_nodes: dict[uuid.UUID, NodeOut] = {}

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
        route: Callable[[httpx.Request], httpx.Response] | None = {
            ("POST", "/api/v1/auth/login"): self._login,
            ("POST", "/api/v1/auth/refresh"): self._refresh,
            ("POST", "/api/v1/auth/logout"): self._logout,
            ("GET", "/api/v1/me"): self._me,
            ("POST", "/api/v1/me/password"): self._change_password,
        }.get((request.method, request.url.path))
        if route is None and request.method == "GET":
            route = self._files_route(request.url.path)
        if route is None:
            route = self._sharing_route(request.method, request.url.path)
        if route is None:
            return _error(404, "NOT_FOUND", "Not found")
        return route(request)

    # --- files, for the browser (server api/v1/namespaces.py, nodes.py) ---------------------

    def add(self, parent_id: uuid.UUID, name: str, size: int | None = None) -> uuid.UUID:
        """A folder (no size) or a file in `parent_id`; returns its node ID."""
        node_id = uuid.uuid4()
        now = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
        self.children.setdefault(parent_id, []).append(
            NodeOut(
                id=node_id,
                namespace_id=self.namespace_id,
                parent_id=parent_id,
                name=name,
                kind=NodeKind.FOLDER if size is None else NodeKind.FILE,
                size_bytes=size or 0,
                content_hash=None if size is None else "0" * 64,
                revision=1,
                created_at=now,
                modified_at=now,
                modified_by_user_id=uuid.UUID(int=1),
            )
        )
        if size is None:
            self.children[node_id] = []
        else:
            self.files.add(node_id)
        return node_id

    def _files_route(self, path: str) -> Callable[[httpx.Request], httpx.Response] | None:
        if path == "/api/v1/namespaces":
            return self._namespaces
        parts = path.split("/")
        if len(parts) == 6 and parts[3] == "nodes" and parts[5] == "children":
            return lambda request: self._children(request, uuid.UUID(parts[4]))
        return None

    def _namespaces(self, request: httpx.Request) -> httpx.Response:
        if self._user(request) is None:
            return _error(401, "UNAUTHENTICATED", "Sign in to continue.")
        namespace = {
            "id": str(self.namespace_id),
            "kind": "personal",
            "root_node_id": str(self.root_id),
            "role": "owner",
        }
        return httpx.Response(200, json={"data": [namespace]})

    def _children(self, request: httpx.Request, folder_id: uuid.UUID) -> httpx.Response:
        if self._user(request) is None:
            return _error(401, "UNAUTHENTICATED", "Sign in to continue.")
        if folder_id in self.files:  # as the server: a file has no children
            return _error(
                400,
                "VALIDATION_ERROR",
                "Only folders have children.",
                [
                    {
                        "loc": ["path", "node_id"],
                        "msg": "Only folders have children.",
                        "type": "not_a_folder",
                    }
                ],
            )
        items = self.children.get(folder_id)
        if items is None:
            return _error(404, "NOT_FOUND", "Item not found.")
        try:
            query = PageQuery.model_validate(dict(request.url.params))
        except ValidationError:
            return _error(400, "VALIDATION_ERROR", "Invalid paging.")
        # As the server orders them: folders first, then by normalized name, then by ID.
        ordered = sorted(
            items,
            key=lambda n: (n.kind != NodeKind.FOLDER, normalize_name(n.name or ""), str(n.id)),
        )
        chunk = ordered[(query.page - 1) * query.limit : query.page * query.limit]
        body = ListResponse[NodeOut](
            data=chunk, meta=Meta(page=query.page, limit=query.limit, total=len(items))
        )
        return httpx.Response(200, content=body.model_dump_json())

    def node(self, node_id: uuid.UUID) -> NodeOut | None:
        if node_id in self.extra_nodes:
            return self.extra_nodes[node_id]
        if node_id == self.root_id:
            now = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
            return NodeOut(
                id=self.root_id,
                namespace_id=self.namespace_id,
                parent_id=None,
                name=None,
                kind=NodeKind.FOLDER,
                size_bytes=0,
                content_hash=None,
                revision=1,
                created_at=now,
                modified_at=now,
                modified_by_user_id=uuid.UUID(int=1),
            )
        return next((n for nodes in self.children.values() for n in nodes if n.id == node_id), None)

    def _sharing_route(
        self, method: str, path: str
    ) -> Callable[[httpx.Request], httpx.Response] | None:
        parts = path.split("/")[3:]  # after /api/v1/
        if method == "GET" and parts in (["users", "search"], ["groups", "search"]):
            return lambda request: self._search(request, parts[0])
        if method == "GET" and parts == ["shared-with-me"]:
            return self._shared_with_me
        if method == "POST" and parts == ["mounts"]:
            return self._mount
        if method == "DELETE" and len(parts) == 2 and parts[0] == "mounts":
            return lambda request: self._unmount(uuid.UUID(parts[1]))
        if len(parts) == 2 and parts[0] == "nodes" and method == "GET":
            return lambda request: self._node(uuid.UUID(parts[1]))
        if len(parts) == 3 and parts[0] == "nodes" and parts[2] == "members":
            node_id = uuid.UUID(parts[1])
            if method == "GET":
                return lambda request: self._sharing(node_id)
            if method == "POST":
                return lambda request: self._add_member(request, node_id)
        if len(parts) == 2 and parts[0] == "members":
            member_id = uuid.UUID(parts[1])
            if method == "PATCH":
                return lambda request: self._change_member(request, member_id)
            if method == "DELETE":
                return lambda request: self._remove_member(member_id)
        return None

    def _node(self, node_id: uuid.UUID) -> httpx.Response:
        node = self.node(node_id)
        if node is None:
            return _error(404, "NOT_FOUND", "Item not found.")
        return httpx.Response(200, content=DataResponse[NodeOut](data=node).model_dump_json())

    def _search(self, request: httpx.Request, what: str) -> httpx.Response:
        text = request.url.params.get("q", "").strip().casefold()
        if len(text) < 2:
            message = "Type at least 2 characters to search."
            issue = {"loc": ["q"], "msg": message, "type": "string_too_short"}
            return _error(400, "VALIDATION_ERROR", message, [issue])
        if what == "users":
            found: list[UserHit] | list[GroupHit] = [
                hit
                for hit in self.people
                if hit.display_name.casefold().startswith(text)
                or hit.email.casefold().startswith(text)
            ]
        else:
            found = [hit for hit in self.groups if hit.name.casefold().startswith(text)]
        return httpx.Response(200, json={"data": [hit.model_dump(mode="json") for hit in found]})

    def _sharing(self, node_id: uuid.UUID) -> httpx.Response:
        owner = UserHit(id=uuid.UUID(int=1), display_name="Alice", email="alice@example.com")
        body = SharingOut(
            node_id=node_id,
            owner=owner,
            members=self.members.get(node_id, []),
            can_manage=self.can_manage,
        )
        return httpx.Response(200, json={"data": body.model_dump(mode="json")})

    def _add_member(self, request: httpx.Request, node_id: uuid.UUID) -> httpx.Response:
        body = MemberIn.model_validate_json(request.content)
        members = self.members.setdefault(node_id, [])
        if any(m.user_id == body.user_id and m.group_id == body.group_id for m in members):
            return _error(409, "ALREADY_SHARED", "They already have access.")
        person = next((p for p in self.people if p.id == body.user_id), None)
        group = next((g for g in self.groups if g.id == body.group_id), None)
        member = MemberOut(
            id=uuid.uuid4(),
            kind="user" if person else "group",
            user_id=body.user_id,
            group_id=body.group_id,
            name=person.display_name if person else group.name if group else "?",
            email=person.email if person else None,
            role=body.role,
        )
        members.append(member)
        return httpx.Response(201, json={"data": member.model_dump(mode="json")})

    def _find_member(self, member_id: uuid.UUID) -> tuple[list[MemberOut], int] | None:
        for members in self.members.values():
            for index, member in enumerate(members):
                if member.id == member_id:
                    return members, index
        return None

    def _change_member(self, request: httpx.Request, member_id: uuid.UUID) -> httpx.Response:
        found = self._find_member(member_id)
        if found is None:
            return _error(404, "NOT_FOUND", "Member not found.")
        members, index = found
        role = MemberUpdateIn.model_validate_json(request.content).role
        members[index] = members[index].model_copy(update={"role": role})
        return httpx.Response(200, json={"data": members[index].model_dump(mode="json")})

    def _remove_member(self, member_id: uuid.UUID) -> httpx.Response:
        found = self._find_member(member_id)
        if found is None:
            return _error(404, "NOT_FOUND", "Member not found.")
        members, index = found
        del members[index]
        return httpx.Response(204)

    def _shared_with_me(self, request: httpx.Request) -> httpx.Response:
        query = PageQuery.model_validate(dict(request.url.params))  # paged, as the server
        start = (query.page - 1) * query.limit
        body = ListResponse[SharedItemOut](
            data=self.shared[start : start + query.limit],
            meta=Meta(page=query.page, limit=query.limit, total=len(self.shared)),
        )
        return httpx.Response(200, content=body.model_dump_json())

    def _mount(self, request: httpx.Request) -> httpx.Response:
        namespace_id = MountIn.model_validate_json(request.content).namespace_id
        for index, item in enumerate(self.shared):
            if item.namespace_id == namespace_id:
                mount = item.mount_node_id  # already added: the same mount, as the server
                if mount is None:
                    mount = self.add(self.root_id, item.name)
                    self.shared[index] = item.model_copy(update={"mount_node_id": mount})
                    top = self.children[self.root_id]
                    top[-1] = top[-1].model_copy(update={"mount_namespace_id": namespace_id})
                node = self.node(mount)
                assert node is not None
                return httpx.Response(
                    201, content=DataResponse[NodeOut](data=node).model_dump_json()
                )
        return _error(404, "NOT_FOUND", "Shared folder not found.")

    def _unmount(self, namespace_id: uuid.UUID) -> httpx.Response:
        for index, item in enumerate(self.shared):
            if item.namespace_id == namespace_id and item.mount_node_id is not None:
                self.shared[index] = item.model_copy(update={"mount_node_id": None})
                return httpx.Response(204)
        return _error(404, "NOT_FOUND", "Shared folder not found.")

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
                "quota_bytes": self.accounts[username].quota_bytes,
                "used_bytes": self.accounts[username].used_bytes,
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


class FakeEngine:
    """Stands in for the sync engine in UI tests: reports states like the real one, and owns
    its index like the real one (closed once stopped)."""

    def __init__(self, root: Path, index: Index, listener: SyncListener, display_name: str) -> None:
        self.root = root
        self.index = index
        self.listener = listener
        self.display_name = display_name
        self.calls: list[str] = []
        self.paused = False

    def start(self) -> None:
        self.calls.append("start")
        self.listener.state_changed(SyncState.PAUSED if self.paused else SyncState.UP_TO_DATE, None)

    def stop(self, timeout: float = 15.0) -> bool:
        self.calls.append("stop")
        self.listener.state_changed(SyncState.STOPPED, None)
        self.index.close()
        return True

    def retry_now(self, key: str) -> None:
        self.calls.append(f"retry {key}")

    def local_changed(self, path: str) -> None:
        self.calls.append(f"changed {path}")

    def pause(self) -> None:
        self.calls.append("pause")
        self.paused = True
        self.listener.state_changed(SyncState.PAUSED, None)

    def resume(self) -> None:
        self.calls.append("resume")
        self.paused = False
        self.listener.state_changed(SyncState.UP_TO_DATE, None)

    def confirm_deletes(self, approved: bool) -> None:
        self.calls.append(f"confirm_deletes({approved})")
        if not approved:
            self.pause()


class FakeEngines:
    """The engine factory for SyncController, keeping every engine it made. The folder's
    ownership check is the real one (sync.binding), as in app.main."""

    def __init__(self, index_path: Path) -> None:
        self.index_path = index_path
        self.made: list[FakeEngine] = []
        self.gate: threading.Event | None = None  # hold starting here until set
        self.fail_with: Exception | None = None

    def __call__(
        self,
        root: Path,
        user_id: uuid.UUID,
        display_name: str,
        server_url: str,
        listener: SyncListener,
    ) -> FakeEngine:
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.fail_with is not None:
            raise self.fail_with
        index = binding.open_index(self.index_path, root, server_url, user_id, MacFileAttributes())
        engine = FakeEngine(root, index, listener, display_name)
        self.made.append(engine)
        return engine

    def forget(self) -> None:
        binding.forget(self.index_path)

    @property
    def last(self) -> FakeEngine:
        return self.made[-1]


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
    sync: SyncController
    transfers: TransfersController
    browser: BrowserController
    file_actions: FileActionsController
    uploads: UploadController
    share: ShareController
    shared: SharedController
    revealed: list[Path]  # what "Show in folder" asked the system to show
    engines: FakeEngines
    trash: Path
    app_data: Path
