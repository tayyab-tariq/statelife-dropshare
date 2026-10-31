"""NFR-PERF-02: a small file saved on one device is visible on another online device within
10 seconds (95th percentile).

Two sync engines run for the same test user, each on its own folder, as two computers would.
A small file is saved in the first; the time until it appears, complete, in the second is the
measurement. Repeated `--files` times.

    uv run python scripts/perf/propagation.py --server http://127.0.0.1:8000 --files 30
"""

import argparse
import functools
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from perf_common import (
    add_target_arguments,
    check_target,
    ensure_users,
    make_engine,
    percentile,
    record,
    say,
    server_label,
    sign_in,
    summary,
)

_TARGET_SECONDS = 10.0
_TIMEOUT_SECONDS = 60.0


def _arrived(target: Path, content: bytes) -> bool:
    return target.exists() and target.read_bytes() == content


def _wait_until(check: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    add_target_arguments(parser)
    parser.add_argument("--files", type=int, default=30)
    args = parser.parse_args()
    check_target(args)

    [name] = ensure_users(args.server, 1)
    with tempfile.TemporaryDirectory(prefix="dropshare-perf-") as temp:
        base = Path(temp)
        first, second = sign_in(args.server, name), sign_in(args.server, name)
        here, here_state = make_engine(first, args.server, base / "A", base / "a.db")
        there, there_state = make_engine(second, args.server, base / "B", base / "b.db")
        here.start()
        there.start()
        try:
            ready = _wait_until(
                lambda: here_state.state == there_state.state == "up_to_date", _TIMEOUT_SECONDS
            )
            if not ready:
                raise RuntimeError("The two clients never got up to date")
            seconds: list[float] = []
            missed = 0
            for n in range(args.files):
                content = f"note {n} {uuid.uuid4()}".encode()
                name_on_disk = f"perf-{uuid.uuid4().hex[:8]}.txt"
                started = time.monotonic()
                (base / "A" / name_on_disk).write_bytes(content)
                target = base / "B" / name_on_disk
                if _wait_until(functools.partial(_arrived, target, content), _TIMEOUT_SECONDS):
                    seconds.append(time.monotonic() - started)
                    say(f"{n + 1}/{args.files}: {seconds[-1]:.2f} s")
                else:
                    missed += 1
                    say(f"{n + 1}/{args.files}: not seen within {_TIMEOUT_SECONDS:.0f} s")
        finally:
            here.stop()
            there.stop()
            first.api.close()
            second.api.close()

    p95 = percentile(seconds, 95)
    record(
        "NFR-PERF-02 change propagation",
        f"a small file visible on another device within {_TARGET_SECONDS:.0f} s (p95)",
        f"p95 {p95:.2f} s over {len(seconds)} files, {missed} not seen within "
        f"{_TIMEOUT_SECONDS:.0f} s",
        missed == 0 and p95 <= _TARGET_SECONDS,
        [
            f"two engines, one user, against {server_label(args.server)}",
            f"seconds: {summary(seconds)}",
            "includes the 2 s a new file must stay unchanged before it is uploaded (FR-SYNC-06)",
        ],
    )


if __name__ == "__main__":
    main()
