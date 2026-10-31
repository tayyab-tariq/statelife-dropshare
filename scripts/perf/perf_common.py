"""What the performance scripts share: test accounts, signed-in API clients, percentiles, and
writing the numbers to docs/perf-results.md (NFR-PERF-01/02/04/05, OQ-40).

The scripts run against a server you have started. Test accounts (`perf-…`) are made, or
reset, in the database `server/.env` points at, through the same service the admin CLI uses
(audited as `cli`). So by default they refuse unless both the server and that database are on
this computer; `--allow-remote` is for a staging server you mean to use, never production.
Each run gets a fresh random password, held only in memory; `cleanup.py` disables the
accounts afterwards.
"""

import argparse
import getpass
import ipaddress
import os
import platform
import secrets
import socket
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.auth import AuthService, Device
from dropshare_client.services.tokens import TokenStore

if TYPE_CHECKING:
    from dropshare_client.sync.engine import SyncEngine

RESULTS = Path(__file__).resolve().parents[2] / "docs" / "perf-results.md"
PREFIX = "perf-"
# The variable a run hands its password to its child processes in, never on the command line.
PASSWORD_ENV = "DROPSHARE_PERF_PASSWORD"  # noqa: S105 - a variable name, not a secret
# This run's password for every test account it uses: random, in memory only.
PASSWORD = os.environ.get(PASSWORD_ENV) or secrets.token_urlsafe(24)


def _local(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="run against a server or database that isn't on this computer (staging only)",
    )


def check_target(args: argparse.Namespace) -> None:
    """Refuse a remote server or database unless asked for, and plain HTTP to a remote one
    always: test accounts are made there, and passwords travel."""
    from dropshare_server.core.config import Settings

    url = urlsplit(args.server)
    host = url.hostname or ""
    database = Settings().db_host
    if url.scheme != "https" and not _local(host):
        sys.exit(f"Refusing plain HTTP to {host}: use https:// for a remote server.")
    if not args.allow_remote and not (_local(host) and _local(database)):
        sys.exit(
            f"Refusing: the server ({host}) or the database ({database}) isn't on this "
            "computer, and test accounts would be made there. Pass --allow-remote for a "
            "staging server you mean to use; never production."
        )


class MemoryCredentials:
    """Keeps a refresh token in memory: perf clients never touch the OS credential store."""

    def __init__(self) -> None:
        self._secret: str | None = None

    def get(self) -> str | None:
        return self._secret

    def set(self, secret: str) -> None:
        self._secret = secret

    def delete(self) -> None:
        self._secret = None


@dataclass(frozen=True)
class Signed:
    api: ApiClient
    auth: AuthService


def sign_in(server_url: str, username: str, password: str = PASSWORD) -> Signed:
    tokens = TokenStore(MemoryCredentials())
    api = ApiClient(
        server_url,
        tokens,
        client_version=version("dropshare-client"),
        user_agent="DropShare-perf",
    )
    auth = AuthService(api, tokens, Device(name="perf", os=platform.system()))
    auth.sign_in(server_url, username, password)
    return Signed(api, auth)


def ensure_users(server_url: str, count: int) -> list[str]:
    """`perf-001` ... `perf-<count>`, ready to sign in with this run's PASSWORD."""
    return [ensure_user(server_url, f"{PREFIX}{n:03d}") for n in range(1, count + 1)]


def ensure_user(server_url: str, name: str, quota_gb: int = 200) -> str:
    """A test account with this run's password: made if missing, its password reset (and the
    account enabled) if an earlier run made it. Through the server's own services, as the
    admin CLI does, so it is audited as `cli`."""
    from dropshare_server.core.config import Settings
    from dropshare_server.core.db import create_db_engine, create_session_factory
    from dropshare_server.core.errors import AppError
    from dropshare_server.services import users as user_service
    from dropshare_server.services.context import cli_context
    from dropshare_shared.errors import ErrorCode
    from dropshare_shared.schemas.users import UserRole

    if not name.startswith(PREFIX):
        raise ValueError(f"Test accounts are named {PREFIX}…")
    ctx = cli_context(getpass.getuser())
    engine = create_db_engine(Settings())
    try:
        sessions = create_session_factory(engine)
        try:
            with sessions.begin() as session:
                temporary = user_service.create_user(
                    session,
                    ctx,
                    username=name,
                    email=f"{name}@perf.invalid",
                    display_name=name.replace("-", " ").title(),
                    role=UserRole.USER,
                    quota_bytes=quota_gb * 1024**3,
                ).temporary_password
        except AppError as exc:
            if exc.code != ErrorCode.NAME_CONFLICT:
                raise
            with sessions.begin() as session:  # made by an earlier run: a new password
                user_service.set_disabled(session, ctx, name, False)
                temporary = user_service.reset_password(session, ctx, name)
    finally:
        engine.dispose()
    first = sign_in(server_url, name, temporary)
    first.auth.change_password(temporary, PASSWORD)
    first.api.close()
    return name


def disable_test_users() -> list[str]:
    """Disable every `perf-…` account (they keep their files until an administrator deletes
    them); returns their names."""
    from dropshare_server.core.config import Settings
    from dropshare_server.core.db import create_db_engine, create_session_factory
    from dropshare_server.services import users as user_service
    from dropshare_server.services.context import cli_context

    ctx = cli_context(getpass.getuser())
    engine = create_db_engine(Settings())
    disabled: list[str] = []
    try:
        sessions = create_session_factory(engine)
        with sessions.begin() as session:
            names = [
                user.username
                for user, _ in user_service.list_users(session, ctx)
                if user.username.startswith(PREFIX)
            ]
        for name in names:
            with sessions.begin() as session:
                user_service.set_disabled(session, ctx, name, True)
            disabled.append(name)
    finally:
        engine.dispose()
    return disabled


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(percent / 100 * len(ordered) + 0.5) - 1))
    return ordered[rank]


def summary(values: list[float]) -> str:
    return (
        f"n={len(values)}, p50={percentile(values, 50):.1f}, p95={percentile(values, 95):.1f}, "
        f"p99={percentile(values, 99):.1f}, mean={statistics.fmean(values):.1f}"
        if values
        else "n=0"
    )


def server_label(server_url: str) -> str:
    """The server as written in results: scheme, host and port only, never credentials."""
    url = urlsplit(server_url)
    return f"{url.scheme}://{url.hostname}{f':{url.port}' if url.port else ''}"


def record(requirement: str, target: str, result: str, passed: bool, details: list[str]) -> None:
    """Append one run to docs/perf-results.md, with where it was measured."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"\n## {requirement} — {stamp}\n",
        f"- **Target:** {target}",
        f"- **Result:** {result} — {'✅ met' if passed else '❌ not met'}",
        f"- **Where:** {socket.gethostname()} ({platform.platform()}), "
        f"Python {platform.python_version()}",
        *(f"- {line}" for line in details),
        "",
    ]
    with RESULTS.open("a", encoding="utf-8") as out:
        out.write("\n".join(lines))
    say("\n".join(lines))


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class QuietListener:
    """A sync engine listener for scripts: remembers the state, ignores the rest."""

    def __init__(self) -> None:
        self.state = "starting"
        self.problem: object = None

    def state_changed(self, state: object, problem: object) -> None:
        self.state, self.problem = str(state), problem

    def conflict(self, path: str, copy_path: str) -> None:
        say(f"conflict: {path} -> {copy_path}")

    def confirm_deletes(self, here: int, on_server: int) -> None:
        say(f"mass delete waiting for confirmation ({here} here, {on_server} on the server)")

    def session_expired(self) -> None:
        say("session expired")

    def progress(self, pending: int) -> None:
        return None

    def transfer(self, event: object) -> None:
        return None


def make_engine(
    signed: Signed, server_url: str, root: Path, index_path: Path
) -> tuple["SyncEngine", QuietListener]:
    """A sync engine for `root`, wired as the app wires it (app.py), watching the folder; and
    its listener, which says when it is up to date."""
    from dropshare_client import platform as client_platform
    from dropshare_client.services.files import RemoteFiles
    from dropshare_client.sync import binding
    from dropshare_client.sync.engine import SyncEngine

    system = client_platform.current(profile="perf")
    user = signed.auth.current_user()
    root.mkdir(parents=True, exist_ok=True)
    index = binding.open_index(index_path, root, server_url, user.id, system.file_attributes)
    listener = QuietListener()
    engine = SyncEngine(
        root,
        index,
        RemoteFiles(signed.api),
        system.recycle_bin,
        system.file_identity,
        system.file_attributes,
        listener,
        display_name=user.display_name,
        max_path=client_platform.max_path_length(),
    )
    return engine, listener
