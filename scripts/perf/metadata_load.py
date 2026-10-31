"""NFR-PERF-01: metadata API calls stay under 300 ms at the 95th percentile with 100 active
users at once.

Each simulated user signs in, then for `--seconds` repeats what a running client asks most:
who it is, its namespaces, a folder's children, its change journal, and its trash. Every call
is timed from the client's side, so the numbers include the network and JSON parsing.

    uv run python scripts/perf/metadata_load.py --server http://127.0.0.1:8000 --users 100
"""

import argparse
import threading
import time
from collections.abc import Callable

from perf_common import (
    add_target_arguments,
    check_target,
    ensure_users,
    percentile,
    record,
    say,
    server_label,
    sign_in,
    summary,
)

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import ApiError, OfflineError
from dropshare_shared.schemas.envelope import DataResponse, ListResponse
from dropshare_shared.schemas.nodes import FolderCreateIn, NamespaceOut, NodeOut
from dropshare_shared.schemas.sync import ChangesResponse
from dropshare_shared.schemas.trash import TrashItemOut
from dropshare_shared.schemas.users import UserOut

_TARGET_MS = 300.0
_FOLDERS_PER_USER = 25  # a little content to list
_LOCK = threading.Lock()  # around what the user threads report


class _Run:
    def __init__(self) -> None:
        self.timings: dict[str, list[float]] = {}
        self.errors: list[str] = []
        self.ran: list[str] = []

    def failed(self, text: str) -> None:
        with _LOCK:
            self.errors.append(text)


def _prepare(api: ApiClient) -> NamespaceOut:
    """The user's namespace, with a few folders to list."""
    namespace = api.envelope("GET", "/api/v1/namespaces", DataResponse[list[NamespaceOut]]).data[0]
    existing = api.envelope(
        "GET", f"/api/v1/nodes/{namespace.root_node_id}/children", ListResponse[NodeOut]
    ).meta.total
    for n in range(existing, _FOLDERS_PER_USER):
        body = FolderCreateIn(parent_id=namespace.root_node_id, name=f"Folder {n:02d}")
        api.data("POST", "/api/v1/nodes", NodeOut, json=body.model_dump(mode="json"))
    return namespace


def _user(server: str, name: str, until: float, run: _Run) -> None:
    """One user: get ready, then call until `until`. A user who can't sign in or get ready is
    counted as failed, so a run never reports "met" with fewer users than asked for."""
    try:
        signed = sign_in(server, name)
    except (ApiError, OfflineError) as exc:
        run.failed(f"{name} couldn't sign in: {exc}")
        return
    api = signed.api
    try:
        namespace = _prepare(api)
        calls: dict[str, Callable[[], object]] = {
            "GET /me": lambda: api.data("GET", "/api/v1/me", UserOut),
            "GET /namespaces": lambda: api.envelope(
                "GET", "/api/v1/namespaces", DataResponse[list[NamespaceOut]]
            ),
            "GET /nodes/{id}/children": lambda: api.envelope(
                "GET", f"/api/v1/nodes/{namespace.root_node_id}/children", ListResponse[NodeOut]
            ),
            "GET /namespaces/{id}/changes": lambda: api.envelope(
                "GET",
                f"/api/v1/namespaces/{namespace.id}/changes",
                ChangesResponse,
                params={"cursor": 0},
            ),
            "GET /trash": lambda: api.envelope("GET", "/api/v1/trash", ListResponse[TrashItemOut]),
        }
        mine: dict[str, list[float]] = {label: [] for label in calls}
        while time.monotonic() < until:
            for label, call in calls.items():
                started = time.perf_counter()
                try:
                    call()
                except (ApiError, OfflineError) as exc:  # counted; the user carries on
                    run.failed(f"{label}: {exc}")
                    continue
                mine[label].append((time.perf_counter() - started) * 1000)
    except (ApiError, OfflineError) as exc:
        run.failed(f"{name} couldn't get ready: {exc}")
        return
    finally:
        api.close()
    with _LOCK:
        for label, values in mine.items():
            run.timings.setdefault(label, []).extend(values)
        run.ran.append(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args()
    check_target(args)

    say(f"Preparing {args.users} test users…")
    names = ensure_users(args.server, args.users)
    run = _Run()
    until = time.monotonic() + args.seconds
    threads = [
        threading.Thread(target=_user, args=(args.server, name, until, run)) for name in names
    ]
    say(f"Running {len(threads)} users for {args.seconds:.0f} s…")
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    every = [value for values in run.timings.values() for value in values]
    p95 = percentile(every, 95)
    record(
        "NFR-PERF-01 metadata API",
        f"p95 < {_TARGET_MS:.0f} ms at {args.users} concurrent active users",
        f"p95 {p95:.0f} ms over {len(every)} calls ({len(every) / args.seconds:.0f} calls/s) "
        f"from {len(run.ran)} of {args.users} users, {len(run.errors)} failed",
        p95 < _TARGET_MS and not run.errors and len(run.ran) == args.users,
        [
            f"{args.users} users for {args.seconds:.0f} s against {server_label(args.server)}",
            "measured from one Python process with a thread per user: its own scheduling and "
            "JSON parsing are in the numbers, so they are on the high side",
            *(f"failed: {error}" for error in run.errors[:5]),
            *(f"`{label}`: {summary(values)} ms" for label, values in sorted(run.timings.items())),
        ],
    )


if __name__ == "__main__":
    main()
