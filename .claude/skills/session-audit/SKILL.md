---
name: session-audit
description: Audit every change made during the current coding session before it is wrapped up or committed. Collects the session diff, runs ruff/mypy/pytest, dispatches specialist reviewer agents in parallel (code, security, API contract, sync safety, UI), verifies their findings, checks the changes against requirements.md and the Definition of Done, and writes a dated audit report. Use this whenever the user says audit, review my changes, review the session, wrap up, end of session, before I commit, is this ready, or runs /audit — even if they don't say "audit" explicitly.
---

# Session audit

Goal: an honest, evidence-based review of _this session's_ changes, so problems are caught before they are committed. The audit reports; it does not fix anything until the user approves.

## Step 1: Collect the changes

Run:

```bash
uv run --no-project python .claude/skills/session-audit/scripts/collect_changes.py
```

It diffs against the snapshot taken by the SessionStart hook (falls back to `HEAD` if no snapshot exists), prints a summary (files, line counts, commits made this session, new dependencies, new or modified migrations, env/key files touched, risky-pattern hits), and writes the full diff to `.claude/.session/session.diff`. Pass `--base <ref>` to audit against a different ref (for example `--base main`).

If there are no changes, say so and stop.

Read the summary carefully. Risky-pattern hits are leads, not findings: confirm each one in the code before reporting it. Modified existing migrations, env files, and key files are always worth a direct look.

## Step 2: Run the checks

Run each and record pass/fail with the first relevant error lines. Do not fix failures yet.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest            # or only the affected packages if the full suite needs services that aren't available
```

If server tests can't run because the Oracle test schema isn't reachable, record that as "not run (reason)", never as passed.

## Step 3: Requirements trace

From the conversation and commits, list the requirement IDs this session worked on. For each, check the acceptance criteria in `docs/requirements.md` against the code and tests: met, partially met (what's missing), or not addressed. Flag any work that goes beyond the requirements (P2/Later features, unapproved assumptions, decisions that contradict §2).

## Step 4: Review

**Small diff (under ~150 changed lines):** review it yourself against CLAUDE.md and the relevant skill references. Spawning agents costs more than it saves at this size. Still apply the `sync-reviewer` lens yourself if sync code changed.

**Larger diff:** dispatch these subagents **in parallel** (one message, multiple Task calls), each pointed at `.claude/.session/session.diff` plus the untracked files listed in the summary:

| Agent                   | Dispatch when the session touched                                                                                                                    |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `code-reviewer`         | anything (always)                                                                                                                                    |
| `security-reviewer`     | `server/` (auth, services, routers, storage), `shared/`, or client credential/updater code                                                           |
| `api-contract-reviewer` | `server/src/.../api/`, `shared/`, or `client/src/.../services/`                                                                                      |
| `sync-reviewer`         | `client/src/.../sync/`, `store/`, transfers, or server uploads/changes/trash/revisions — **always** if any of these changed, regardless of diff size |
| `ui-reviewer`           | `client/src/.../ui/` or `controllers/`                                                                                                               |

Tell each agent the session goal and the requirement IDs so it can judge intent, not just style.

## Step 5: Verify and consolidate

Reviewers can be wrong. For every finding:

1. Open the cited file and line and confirm the problem is real and was introduced this session. Drop it otherwise.
2. Merge duplicates reported by more than one agent.
3. Assign one severity:
   - **Blocker**: bug, possible data loss, security issue, data leak, broken contract, failing check, missing audit event for a required action, unmet acceptance criterion the session claimed to implement.
   - **Should fix**: convention violation, missing test for new behavior, accessibility gap, Oracle portability risk, maintainability problem.
   - **Nit**: naming, small readability improvements.

Also list **test gaps**: new or changed behavior (or acceptance criteria) with no test covering it.

## Step 6: Definition of Done

Go through every item in the Definition of Done section of CLAUDE.md. Mark each ✅ / ❌ / N/A with one line of evidence (a check result, a file reference, or "no UI changed"). Never mark ✅ without evidence.

## Step 7: Write the report

Save to `docs/session-audits/YYYY-MM-DD-HHmm.md` using this template:

```markdown
# Session audit: <date time>

**Goal of the session:** <one line>
**Requirements:** <IDs>
**Verdict:** ✅ Ready to commit | ⚠️ Ready after should-fixes | ❌ Blockers found
**Scope:** <N files, +X/−Y lines, base <sha>>

## Checks

| Check | Result | Notes |

## Requirements trace

| ID | Status | Evidence / gap |

## Findings

### Blockers

- **[file:line]** Problem. Why it matters. Suggested fix.

### Should fix

### Nits

## Test gaps

## Definition of Done

- [x]/[ ] item — evidence

## What changed (summary)

<3–6 bullets a reviewer could read in 30 seconds>

## Suggested commit(s)

<conventional commit message(s), split by logical change, with Refs: requirement IDs>

## Lessons for CLAUDE.md

<only if a mistake pattern showed up that a one-line rule would prevent; otherwise "None">
```

## Step 8: Hand off

In chat, give the verdict, the blockers and should-fixes (one line each), and the report path. Then ask whether to fix them now. After fixes, re-run Step 2 and update the report's verdict.

Never edit CLAUDE.md or docs/requirements.md, commit, or push as part of the audit; propose and wait.
