"""NFR-PERF-05: an initial sync of many files completes without user intervention and survives
restarts midway.

A fresh test user gets `--files` files totalling `--gb` GB: mostly small, a few large, in
folders of 500. One computer uploads them all, the other downloads them all. Each client runs
in its own process and is killed (SIGKILL, no clean shutdown) once `--kill-at` of the files
have moved, then started again. Every file is then checked by size and SHA-256.

    uv run python scripts/perf/initial_sync.py --server http://127.0.0.1:8000 \\
        --files 5000 --gb 5

Full scale (requirements): --files 50000 --gb 50. Needs about three times the size in free
disk: the two folders and the server's storage.
"""

import argparse
import hashlib
import os
import random
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from perf_common import (
    PASSWORD,
    PASSWORD_ENV,
    add_target_arguments,
    check_target,
    ensure_user,
    make_engine,
    record,
    say,
    server_label,
    sign_in,
)

from dropshare_shared.schemas.envelope import PAGE_LIMIT_MAX, DataResponse, ListResponse
from dropshare_shared.schemas.nodes import NamespaceOut, NodeOut

_PER_FOLDER = 500
_SMALL_SHARE = 0.9  # nine files in ten are small (up to 64 KB)
_POLL_SECONDS = 2.0
_TIMEOUT_SECONDS = 6 * 3600.0


def _generate(root: Path, files: int, total: int) -> dict[str, str]:
    """The files, and the SHA-256 of each by relative path."""
    rng = random.Random(2026)  # noqa: S311 - test data, not security
    small = int(files * _SMALL_SHARE)
    sizes = [rng.randint(1, 64 * 1024) for _ in range(small)]
    large = files - small
    left = max(0, total - sum(sizes))
    sizes += [left // large + (1 if n < left % large else 0) for n in range(large)] if large else []
    rng.shuffle(sizes)
    hashes: dict[str, str] = {}
    for n, size in enumerate(sizes):
        relative = f"Batch {n // _PER_FOLDER:03d}/file-{n:06d}.bin"
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        with path.open("wb") as out:
            remaining = size
            while remaining:
                block = os.urandom(min(8 * 1024 * 1024, remaining))
                out.write(block)
                digest.update(block)
                remaining -= len(block)
        hashes[relative] = digest.hexdigest()
    return hashes


def _bind(server: str, user: str, root: Path, index: Path) -> None:
    """Make `root` this account's sync folder while it is empty, as choosing it in the app
    does: a folder already full of files nobody can vouch for is refused (ForeignFolderError)."""
    from dropshare_client import platform as client_platform
    from dropshare_client.sync import binding

    signed = sign_in(server, user)
    me = signed.auth.current_user()
    root.mkdir(parents=True, exist_ok=True)
    attributes = client_platform.current(profile="perf").file_attributes
    binding.open_index(index, root, server, me.id, attributes).close()
    signed.api.close()


def _child(server: str, user: str, root: Path, index: Path) -> None:
    """One computer: sync until killed (no clean shutdown) or its stdin closes (the parent
    is done: stop cleanly). Works the same on Windows, which has no SIGTERM handler."""
    engine, _ = make_engine(sign_in(server, user), server, root, index)
    engine.start()
    sys.stdin.read()  # returns once the parent closes it
    engine.stop()


def _spawn(args: argparse.Namespace, user: str, root: Path, index: Path) -> subprocess.Popen[bytes]:
    command = [sys.executable, __file__, "--child", "--server", args.server, "--user", user]
    environment = {**os.environ, PASSWORD_ENV: PASSWORD}  # never on the command line
    return subprocess.Popen(  # noqa: S603 - this script, with its own arguments
        [*command, "--root", str(root), "--index", str(index)],
        stdin=subprocess.PIPE,
        env=environment,
    )


def _stop(process: subprocess.Popen[bytes]) -> None:
    """A clean stop: the child sees its stdin close, stops its engine, and exits."""
    if process.stdin is not None:
        process.stdin.close()
    process.wait(timeout=120)


class _ServerCount:
    """How many files the server has, by one signed-in client reused for every poll."""

    def __init__(self, server: str, user: str) -> None:
        self._api = sign_in(server, user).api

    def __call__(self) -> int:
        root = self._api.envelope(
            "GET", "/api/v1/namespaces", DataResponse[list[NamespaceOut]]
        ).data[0]
        count = 0
        for folder in self._all(root.root_node_id):
            count += self._api.envelope(
                "GET",
                f"/api/v1/nodes/{folder.id}/children",
                ListResponse[NodeOut],
                params={"limit": 1},
            ).meta.total
        return count

    def _all(self, folder_id: object) -> list[NodeOut]:
        found: list[NodeOut] = []
        page = 1
        while True:
            response = self._api.envelope(
                "GET",
                f"/api/v1/nodes/{folder_id}/children",
                ListResponse[NodeOut],
                params={"page": page, "limit": PAGE_LIMIT_MAX},
            )
            found.extend(response.data)
            if not response.data or len(found) >= response.meta.total:
                return found
            page += 1

    def close(self) -> None:
        self._api.close()


def _local_count(root: Path) -> int:
    return sum(1 for path in root.glob("Batch */*.bin") if path.is_file())


def _phase(
    name: str,
    args: argparse.Namespace,
    user: str,
    root: Path,
    index: Path,
    moved: Callable[[], int],
) -> tuple[float, int]:
    """Run one computer until every file has moved, killing and restarting it once midway.
    Returns the seconds it took and how many restarts it needed."""
    started = time.monotonic()
    process = _spawn(args, user, root, index)
    killed = 0
    deadline = started + _TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{name}: the client stopped on its own (exit {process.returncode})")
        done = moved()
        say(f"{name}: {done}/{args.files} files")
        if killed == 0 and done >= args.files * args.kill_at:
            if done >= args.files:  # it finished between two looks: not a restart midway
                raise RuntimeError(f"{name}: done before it could be killed; lower --kill-at")
            say(f"{name}: killing the client (no clean shutdown), then starting it again")
            process.kill()
            process.wait()
            killed += 1
            process = _spawn(args, user, root, index)
        elif done >= args.files:
            break
        time.sleep(_POLL_SECONDS)
    elapsed = time.monotonic() - started
    _stop(process)
    return elapsed, killed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--gb", type=float, default=5.0)
    parser.add_argument("--kill-at", type=float, default=0.5)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--user", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--index", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        _child(args.server, args.user, args.root, args.index)
        return
    check_target(args)

    user = ensure_user(args.server, f"perf-sync-{datetime.now(UTC):%Y%m%d%H%M%S}")
    total = int(args.gb * 1000**3)
    with tempfile.TemporaryDirectory(prefix="dropshare-perf-") as temp:
        base = Path(temp)
        _bind(args.server, user, base / "A", base / "a.db")  # chosen before it fills up
        say(f"Writing {args.files} files, {args.gb} GB…")
        hashes = _generate(base / "A", args.files, total)
        on_server = _ServerCount(args.server, user)
        try:
            up, up_kills = _phase("upload", args, user, base / "A", base / "a.db", on_server)
        finally:
            on_server.close()
        down, down_kills = _phase(
            "download", args, user, base / "B", base / "b.db", lambda: _local_count(base / "B")
        )
        wrong = [
            relative
            for relative, sha in hashes.items()
            if not (base / "B" / relative).is_file()
            or hashlib.sha256((base / "B" / relative).read_bytes()).hexdigest() != sha
        ]

    gb = total / 1000**3
    record(
        "NFR-PERF-05 initial sync",
        "an initial sync completes without intervention and survives a restart midway",
        f"{args.files} files / {gb:.1f} GB up in {up / 60:.1f} min and down in {down / 60:.1f} "
        f"min, each killed and restarted once; {len(wrong)} files wrong or missing",
        not wrong and up_kills == 1 and down_kills == 1,
        [
            f"against {server_label(args.server)}; full scale is 50,000 files / 50 GB",
            f"upload {args.files / up:.0f} files/s, {gb * 1000 / up:.0f} MB/s; "
            f"download {args.files / down:.0f} files/s, {gb * 1000 / down:.0f} MB/s",
            f"killed (no clean shutdown) at {args.kill_at:.0%} of the files moved",
            *(f"wrong: {relative}" for relative in wrong[:10]),
        ],
    )


if __name__ == "__main__":
    main()
