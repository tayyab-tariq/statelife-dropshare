#!/usr/bin/env python3
"""PreToolUse hook: block edits to Alembic migrations that are already committed.

A committed migration may already be applied to a shared Oracle schema; editing it causes
schema drift that Alembic cannot detect. New, uncommitted migrations can still be edited.
Exit code 2 blocks the tool call and shows stderr to Claude. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MIGRATIONS_DIR = "server/migrations/versions/"


def main() -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not isinstance(file_path, str) or not file_path.endswith(".py"):
        return 0

    try:
        root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
        ).resolve()
        rel = Path(file_path).resolve().relative_to(root).as_posix()
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0  # outside the repo or not a git repo: nothing to protect

    if not rel.startswith(MIGRATIONS_DIR):
        return 0

    committed = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rel}"],
        capture_output=True, timeout=10, check=False,
    ).returncode == 0
    if not committed:
        return 0

    print(
        f"Blocked: {rel} is a committed Alembic migration and may already be applied. "
        "Create a new migration instead (/migration <change>). If this migration has "
        "definitely never been applied anywhere, ask the user to confirm and edit it themselves.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never break editing because the guard itself failed
        sys.exit(0)
