#!/usr/bin/env python3
"""Collect everything changed since the session snapshot and print a Markdown summary.

The full diff is written to .claude/.session/session.diff for reviewer agents.
Usage: uv run --no-project python .claude/skills/session-audit/scripts/collect_changes.py [--base <ref>]
Stdlib only (Python 3.11+ for tomllib).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
MIGRATIONS_DIR = "server/migrations/versions/"
MAX_HITS = 60
LARGE_FILE_LINES = 400


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout.rstrip("\n")


def try_git(*args: str) -> str:
    try:
        return git(*args)
    except subprocess.CalledProcessError:
        return ""


def read_if(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


# (pattern, label, path filter) — path filter is a regex the file path must match, or None for all .py
CHECKS: list[tuple[re.Pattern[str], str, re.Pattern[str] | None]] = [
    (re.compile(r"(:|->)\s*Any\b|\[Any\b|\bAny\]"), "`Any` in a type annotation", None),
    (re.compile(r"#\s*type:\s*ignore(?!\[)"), "`# type: ignore` without an error code", None),
    (re.compile(r"#\s*noqa(?!:)"), "Blanket `# noqa`", None),
    (re.compile(r"^\s*print\("), "`print` (use logging)", None),
    (re.compile(r"\bbreakpoint\(\)|\bimport pdb\b|pdb\.set_trace"), "Debugger left in", None),
    (re.compile(r"^\s*except\s*:"), "Bare `except:`", None),
    (re.compile(r"except\s+(Base)?Exception\s*:\s*pass"), "Swallowed exception", None),
    (re.compile(r"pytest\.mark\.(skip|xfail)\((?!.*reason)"), "Skipped/xfail test without reason", None),
    (re.compile(r"\b(text|execute|exec_driver_sql)\(\s*f[\"']"), "SQL built with an f-string", None),
    (re.compile(r"\beval\(|\bexec\(|pickle\.loads?\(|yaml\.load\((?!.*SafeLoader)"), "eval/exec/unsafe deserialization", None),
    (re.compile(r"shell\s*=\s*True"), "subprocess with shell=True", None),
    (re.compile(r"verify\s*=\s*False"), "TLS verification disabled", None),
    (re.compile(r"datetime\.utcnow\(|datetime\.now\(\s*\)"), "Naive datetime (use datetime.now(UTC))", None),
    (re.compile(r"(password|secret|api[_-]?key|token)\s*[:=]\s*[\"'][^\"']{6,}[\"']", re.I), "Possible hardcoded secret", None),
    (re.compile(r"\bTODO\b|\bFIXME\b"), "TODO/FIXME left in", None),
    # Layering and client safety
    (re.compile(r"^\s*(import httpx|from httpx|from dropshare_client\.(services|sync|store)\b)"), "UI layer importing services/sync/store/httpx", re.compile(r"^client/src/dropshare_client/ui/")),
    (re.compile(r"\btime\.sleep\("), "time.sleep on UI/controller code (use QTimer)", re.compile(r"^client/src/dropshare_client/(ui|controllers)/")),
    (re.compile(r"^\s*(from|import) (PySide6|PyQt)"), "Qt import in Qt-free layer", re.compile(r"^client/src/dropshare_client/(sync|store|services)/")),
    (re.compile(r"\bos\.(remove|unlink|rmdir)\(|shutil\.rmtree\(|\.unlink\("), "Hard delete in sync code (must use Recycle Bin helper)", re.compile(r"^client/src/dropshare_client/sync/")),
    (re.compile(r"\bwinreg\b|ctypes\.windll|ctypes\.WinDLL"), "Windows API outside platform/", re.compile(r"^client/src/dropshare_client/(?!platform/)")),
    (re.compile(r"^\s*(from|import) sqlalchemy"), "SQLAlchemy outside repositories/models", re.compile(r"^server/src/dropshare_server/(api|services|dependencies)/")),
    (re.compile(r"^\s*(from|import) (fastapi|starlette)"), "FastAPI import in service layer", re.compile(r"^server/src/dropshare_server/services/")),
]


def scan_line(path: str, line_no: int, line: str, hits: list[str]) -> None:
    if not path.endswith((".py", ".pyi")):
        return
    for pattern, label, path_filter in CHECKS:
        if path_filter is not None and not path_filter.search(path):
            continue
        if pattern.search(line):
            hits.append(f"{path}:{line_no} — {label}")


def dependency_names(pyproject_text: str) -> set[str]:
    try:
        data = tomllib.loads(pyproject_text)
    except tomllib.TOMLDecodeError:
        return set()
    specs: list[str] = list(data.get("project", {}).get("dependencies", []))
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        specs.extend(group)
    for group in data.get("dependency-groups", {}).values():
        specs.extend(s for s in group if isinstance(s, str))
    specs.extend(data.get("tool", {}).get("uv", {}).get("dev-dependencies", []))
    names: set[str] = set()
    for spec in specs:
        match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def main() -> int:
    try:
        git("rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.CalledProcessError):
        print("Not a git repository. Run `git init` and commit before auditing.", file=sys.stderr)
        return 1

    root = Path(git("rev-parse", "--show-toplevel"))
    session_dir = root / ".claude" / ".session"
    session_dir.mkdir(parents=True, exist_ok=True)

    if "--base" in sys.argv:
        base = sys.argv[sys.argv.index("--base") + 1]
        base_note = "from --base"
    else:
        base = read_if(session_dir / "base-sha")
        base_note = "session snapshot"
    if not base:
        base = try_git("rev-parse", "HEAD")
        base_note = "HEAD (no session snapshot found)"
        if not base:
            base, base_note = EMPTY_TREE, "empty tree (no commits yet)"

    head_sha = try_git("rev-parse", "HEAD")
    started_at = read_if(session_dir / "started-at")
    preexisting = set(filter(None, read_if(session_dir / "untracked-at-start").splitlines()))

    name_status = [l for l in git("diff", "--name-status", base).splitlines() if l]
    numstat = [l for l in git("diff", "--numstat", base).splitlines() if l]
    untracked = [
        f for f in git("ls-files", "--others", "--exclude-standard").splitlines()
        if f and f not in preexisting and not f.startswith(".claude/.session/")
    ]
    commits = ""
    if head_sha and base != EMPTY_TREE:
        base_commit = try_git("rev-parse", f"{base}^{{commit}}")
        if base_commit:
            commits = try_git("log", "--oneline", f"{base_commit}..HEAD")

    diff = git("diff", base)
    (session_dir / "session.diff").write_text(diff + "\n", encoding="utf-8")

    if not name_status and not untracked:
        print(f"No changes since {base[:8]} ({base_note}).")
        return 0

    added = removed = 0
    for line in numstat:
        a, r, *_ = line.split("\t")
        added += int(a) if a.isdigit() else 0
        removed += int(r) if r.isdigit() else 0
    for f in untracked:
        try:
            added += len((root / f).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeDecodeError):
            pass

    # Risky patterns: added lines in the diff + whole content of new untracked files.
    hits: list[str] = []
    current, line_no = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", line)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if line.startswith("+"):
            scan_line(current, line_no, line[1:], hits)
            line_no += 1
        elif not line.startswith("-"):
            line_no += 1
    for f in untracked:
        try:
            for n, line in enumerate((root / f).read_text(encoding="utf-8").splitlines(), start=1):
                scan_line(f, n, line, hits)
        except (OSError, UnicodeDecodeError):
            pass

    changed_paths = [l.split("\t")[-1] for l in name_status] + untracked

    large = []
    for f in changed_paths:
        p = root / f
        if f.endswith(".py") and p.exists():
            n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            if n > LARGE_FILE_LINES:
                large.append(f"{f} ({n} lines)")

    new_deps: list[str] = []
    for f in (p for p in changed_paths if p.endswith("pyproject.toml")):
        now_path = root / f
        now = dependency_names(now_path.read_text(encoding="utf-8")) if now_path.exists() else set()
        before = dependency_names(try_git("show", f"{base}:{f}"))
        new_deps.extend(f"{d} ({f})" for d in sorted(now - before))

    new_migrations = [f for f in untracked if f.startswith(MIGRATIONS_DIR)]
    new_migrations += [l.split("\t")[-1] for l in name_status if l.startswith("A") and l.split("\t")[-1].startswith(MIGRATIONS_DIR)]
    edited_migrations = [l.split("\t")[-1] for l in name_status if l[0] in "MD" and l.split("\t")[-1].startswith(MIGRATIONS_DIR)]
    env_touched = [f for f in changed_paths if re.search(r"(^|/)\.env(?!\.example)", f)]
    secrets_touched = [f for f in changed_paths if re.search(r"\.(pfx|p12|key|pem)$", f)]

    out: list[str] = ["# Session changes", ""]
    out.append(f"- **Base:** `{base[:8]}` ({base_note})" + (f", session started {started_at}" if started_at else ""))
    out.append(f"- **Scope:** {len(name_status) + len(untracked)} files, +{added} / −{removed} lines")
    out.append("- **Full diff:** `.claude/.session/session.diff` (untracked files are NOT in it; read them directly)")
    out += ["", "## Files", "```", *name_status, *(f"?\t{f}  (new, untracked)" for f in untracked), "```", ""]
    if commits:
        out += ["## Commits made this session", "```", commits, "```", ""]
    out += ["## Flags", ""]
    out.append("**New dependencies (need approval per CLAUDE.md):**\n" + "\n".join(f"- {d}" for d in new_deps) if new_deps else "- No new dependencies")
    out.append(f"- New migrations (review for Oracle): {', '.join(new_migrations)}" if new_migrations else "- No new migrations")
    out.append(f"- ⚠️ **Existing migrations modified/deleted:** {', '.join(edited_migrations)}" if edited_migrations else "- No existing migrations modified")
    out.append(f"- ⚠️ **Env files changed:** {', '.join(env_touched)}" if env_touched else "- No env files changed")
    out.append(f"- ⚠️ **Certificate/key files changed:** {', '.join(secrets_touched)}" if secrets_touched else "- No certificate/key files changed")
    out.append(f"- Python files over {LARGE_FILE_LINES} lines: {', '.join(large)}" if large else "- No oversized Python files")
    out += ["", f"## Risky-pattern hits ({len(hits)}) — verify each before reporting", ""]
    if hits:
        out += [f"- {h}" for h in hits[:MAX_HITS]]
        if len(hits) > MAX_HITS:
            out.append(f"- …and {len(hits) - MAX_HITS} more")
    else:
        out.append("- None")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
