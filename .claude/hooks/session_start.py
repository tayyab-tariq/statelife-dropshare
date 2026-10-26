#!/usr/bin/env python3
"""SessionStart hook: snapshot the repo so /audit can diff exactly this session's work.

`git stash create` captures uncommitted tracked changes as a commit WITHOUT touching the
working tree, so pre-existing edits aren't blamed on this session. Never blocks startup.
Stdlib only.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True, timeout=20
    )
    return result.stdout.strip()


def main() -> None:
    try:
        root = Path(git("rev-parse", "--show-toplevel"))
    except (OSError, subprocess.SubprocessError):
        return  # not a git repo yet: the audit falls back gracefully

    session_dir = root / ".claude" / ".session"
    session_dir.mkdir(parents=True, exist_ok=True)

    base = ""
    try:
        base = git("stash", "create") or git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        pass  # no commits yet

    untracked = ""
    try:
        untracked = git("ls-files", "--others", "--exclude-standard")
    except (OSError, subprocess.SubprocessError):
        pass

    (session_dir / "base-sha").write_text(base, encoding="utf-8")
    (session_dir / "started-at").write_text(
        datetime.now(timezone.utc).isoformat(), encoding="utf-8"
    )
    (session_dir / "untracked-at-start").write_text(untracked, encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - a hook must never block session start
        pass
    sys.exit(0)
