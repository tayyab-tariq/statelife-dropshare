#!/usr/bin/env python3
"""PostToolUse hook: format the Python file Claude just edited with ruff.

Runs `ruff format` and `ruff check --select I --fix` (import sorting) via the project
environment (`uv run --no-sync`), falling back to a ruff on PATH. Silent no-op if neither
is available. Never fails the tool call. Stdlib only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys


def ruff_command() -> list[str] | None:
    if shutil.which("uv"):
        return ["uv", "run", "--no-sync", "ruff"]
    if shutil.which("ruff"):
        return ["ruff"]
    return None


def main() -> None:
    payload = json.loads(sys.stdin.read() or "{}")
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not isinstance(file_path, str):
        return
    normalized = file_path.replace("\\", "/")
    if not normalized.endswith((".py", ".pyi")) or "/.claude/.session/" in normalized:
        return

    ruff = ruff_command()
    if ruff is None:
        return

    for args in (["format", "--quiet", file_path], ["check", "--select", "I", "--fix", "--quiet", file_path]):
        subprocess.run(
            [*ruff, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook must never fail the tool call
        pass
    sys.exit(0)
