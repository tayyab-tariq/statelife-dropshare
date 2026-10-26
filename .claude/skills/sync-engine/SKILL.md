---
name: sync-engine
description: Build or change the client sync engine and file transfers of this Dropbox-style project — local index (SQLite), file watcher and scanner, three-way reconciliation, conflict copies, move/rename detection, mass-delete protection, Recycle Bin deletes, resumable chunked uploads and ranged downloads, long-poll change notifications, retries and idempotency, and Windows file-system edge cases (locked files, long paths, reserved names, read-only viewers). Use this for ANY code in client/sync, client/store, or client transfers, and for server-side changes to the change journal, revisions, uploads, or trash that the client depends on — even small fixes. The rule above all: never lose a user's data.
---

# Sync engine

This code can destroy users' files, so it is held to a higher bar than anything else.

Before writing code, read `references/algorithm.md` (index, reconciliation, conflicts, safety
rules). For uploads, downloads, long-poll, or retries also read `references/transfers.md`. For
anything that touches the local disk, read `references/windows-fs.md`.

Requirements: §6.4–6.7 and §10 of `docs/requirements.md`.

## Non-negotiable rules

1. **No data loss in any interleaving.** When in doubt, keep both copies.
2. **Disk or server first, index second.** Update the local index only after the operation it records has succeeded.
3. **Journal before acting.** Write intent to the operation journal before a multi-step operation, so a crash can resume or roll back.
4. **Atomic local writes.** Download to a temp file in the sync root's hidden temp folder, verify SHA-256, then `os.replace`.
5. **Remote deletes go to the Recycle Bin**, never `os.remove`/`shutil.rmtree` on user files.
6. **Mass-delete protection** (FR-SYNC-08) is checked before _any_ deletion in a batch is applied.
7. **Every server write carries the base revision.** `409 REVISION_CONFLICT` means "resolve as conflict", never "overwrite".
8. **Retries verify end state.** A retried write that returns a conflict first checks whether the server already reflects the intended change.
9. `sync/` and `store/` import no Qt. Progress goes out through the engine's callback interface.

## Workflow

1. **Plan the cases first.** Before coding, list the local × remote scenarios the change affects (use the matrix in `references/algorithm.md`) and what the outcome must be for each, including crash points.
2. **Write the tests first** for those scenarios (table-driven, fake API, temp sync folder, simulated crashes).
3. Implement in small steps; keep reconciliation decisions pure functions of (local, remote, base) so they're easy to test.
4. Run the `sync-reviewer` agent on the diff before considering it done.
5. **Verify:** `uv run ruff check . && uv run mypy . && uv run pytest client/tests/sync` on the Mac. The reconciliation logic is OS-independent and fully testable there; anything relying on Windows file-system behaviour (`references/windows-fs.md`) also needs `windows_only` tests and a Windows check list for the Windows machine.

## Checklist before reporting done

- Every affected scenario in the matrix has a test, including crash-and-restart and retry-after-timeout
- No index write precedes the operation it records
- No user file is deleted except through the Recycle Bin helper
- Locked/in-use files are retried, not failed or skipped silently
- Names and paths validated with `dropshare_shared.naming`; unsyncable items appear in "Can't sync" with a reason
- Engine can be stopped at any point and restarted without manual repair
