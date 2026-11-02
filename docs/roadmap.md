# StateLife DropShare — Roadmap

> **Approved:** 2026-10-26 · **Reprioritised:** 2026-10-28 · Source of truth for scope: `docs/requirements.md`.
> Scope is cut before data-safety rules are (conflict copies, Recycle Bin, index-after-success,
> mass-delete protection, journaled crash recovery).

Every milestone starts with `/plan` and ends with `/audit` and `/commit`.

## Status

| Milestone | Status |
|---|---|
| M1 Scaffolding | ✅ Done |
| M2 Accounts, auth, and audit (server + CLI) | ✅ Done |
| M3 Files, transfers, and change journal (server) | ✅ Done |
| M4 Desktop sign-in and shell | ✅ Done |
| M5 Sync engine | ✅ Done |
| M6 File browser, uploads, and transfer progress | ✅ Done (manual and Windows checks deferred, see below) |
| M7 Renames and moves | ✅ Done (manual and Windows checks deferred, see below) |
| M8 Sharing | ✅ Done (manual and Windows checks deferred, see below) |
| M9 Trash and cleanup jobs | ✅ Done (Windows check deferred, see below) |
| M10 Transfer robustness | ✅ Done (full-scale perf and Windows checks deferred, see below) |
| M11 App completeness | ✅ Done (Windows pass deferred, see below) |
| M12 Admin and security | ✅ Done (staging, Oracle and Windows checks deferred, see below) |
| M13 Distribution | In progress |
| M14 Ops and go-live | Planned |

### Deferred checks (run once the app is complete)

The automated "Done when" items of these milestones pass. The manual checks are run together at the end:

- **M11, Windows:** the accessibility and display-scaling pass at 100/150/200% in light, dark and high contrast (NFR-UX-02), and the check list in `docs/session-audits/2026-11-02-0044.md`.
- **M12, Windows:** the change-password dialog at 150/200% scaling and with large text (the hint wraps), and Narrator reading "too common" after a refused password.
- **M12, staging (needs OQ-8):** the reverse proxy in front of the real app, and the global limit driven past 100 requests/s, per the checks in `docs/deploy/reverse-proxy.md`.
- **M12, Oracle:** re-run the server suite on Oracle before staging (OQ-25).
- **M6, Mac:** drag a ~1 GB file into the app; progress shows in Transfers and the tray, and the second profile receives the file.
- **M8, two profiles:** share a folder as editor and both edit; switch to viewer; remove access (with and without unsynced changes).
- **M10, scale and network:** `scripts/perf/initial_sync.py --files 50000 --gb 50` and `throughput.py --mb 4096` from the Windows machine to the server (see `docs/perf-results.md`); the Windows check above.
- **M9, Windows:** Trash view at 100/150/200% scaling, light/dark/high contrast, keyboard only (Enter restores, Delete deletes for good, F5) and Narrator.
- **M8, Windows:** the check list at the end of `docs/session-audits/2026-10-28-1720.md` (read-only attribute, viewer edits, opening a link, scaling/contrast/Narrator, save-copy progress and cancel).
- **M6 and M7, Windows:** the check list at the end of `docs/session-audits/2026-10-28-0727.md` (Explorer drag-and-drop and Show in Explorer, junctions, a 3 GB upload, Replace and Cancel with the file open in Word, 100/150/200% scaling, light/dark/high contrast, keyboard-only and Narrator) and the M7 items in `docs/session-audits/2026-10-28-0405.md` (case-only rename on NTFS, renaming a file open in Office), plus `uv run pytest -m windows_only client/tests`.

## Decisions

Earlier decisions (OQ-7, OQ-12 … OQ-21) stay as recorded in requirements §15. New:

| # | Decision |
|---|---|
| OQ-22 | Sync progress is shown in a **Transfers view** in the app (a progress bar per file) plus an **overall summary in the tray**. No progress on Explorer icons (that's BL-09, Phase 2). |
| OQ-23 | Uploading from the app **copies the files/folders into the sync folder**, and the sync engine uploads them. More generally, browser actions on synced items (upload, new folder, delete, rename, move) act on the local sync folder, so there is one write path. The exception is individually shared files, which aren't in the sync folder (FR-SHARE-03); those use the API directly (M8). |
| OQ-24 | The share dialog finds people by **searching all active users by name or email**, and groups by name. |

### Interim behaviours from OQ-15 and where they end

| Interim behaviour | Resolved in |
|---|---|
| Local rename/move sent as trash old + upload new | ✅ M7 (clients from before M7 still apply others' renames as Recycle Bin + download: OQ-29) |
| Conflict notifications use the tray only | M11 (Activity view and notifications) |
| Uploads resume within a session, not across restarts | ✅ M10 (OQ-39) |

Decisions taken while building M6 and M7 are recorded in requirements §15: OQ-26 (Replace on a folder merges it), OQ-27 (on conflicting moves the server wins), OQ-28 (cancelling an upload takes back only what it created and restores replaced files), OQ-29 (older clients and moves; the server is upgraded before clients).

## Requirement changes to apply

✅ Applied to `docs/requirements.md` on 2026-10-28.

Apply these to `docs/requirements.md` before starting M6, keeping the existing format.

**Amend FR-FILE-02 — Upload** `v1`
Acceptance:
- Upload via an Upload button (files, or a folder) and by dragging files/folders from Explorer onto the browser or onto a folder in it.
- The selected items are **copied into the matching folder inside the sync folder**; the sync engine uploads them (OQ-23). The copy runs off the UI thread, shows progress, and can be cancelled.
- If a name already exists: ask **Keep both** (default; adds ` (1)`, ` (2)` …), **Replace**, or **Skip**, with "Apply to all" for multiple items.
- Before copying, check free disk space and remaining quota. If either is insufficient, nothing is copied, and the message says how much space is needed.

**New FR-CLIENT-08 — Transfers view and progress** `v1`
Acceptance:
- A Transfers view lists active uploads and downloads, each with name, direction, a progress bar, bytes done/total, and percent. It also shows queued and waiting items, and the 50 most recent completed or failed transfers.
- An overall summary is shown at the top of the view and in the tray tooltip and menu (for example "Syncing 12 items · 45%").
- Progress updates reach the UI at most about 4 times per second per item, and the view stays responsive with 1,000 queued items.
- Failed transfers show the reason and a Retry action.

**New FR-SHARE-08 — Find people and groups** `v1`
Acceptance:
- `GET /users/search?q=` returns active users matching the start of their display name, username, or email (case-insensitive), with only ID, display name, and email. Requires at least 2 characters and returns at most 20 results. Disabled users are never returned.
- `GET /groups/search?q=` does the same for groups (ID, name, member count).
- Both endpoints require authentication and are rate-limited like other API calls.

**API overview (§9):** add `GET /users/search` and `GET /groups/search` under Sharing.

**Decisions (§15):** add OQ-22, OQ-23, OQ-24 above to the Resolved table.

## Next milestones (in priority order)

### M6 — File browser, uploads, and transfer progress · medium–large

**Status:** ✅ Done 2026-10-28 (`b5b6b55`, `a28a11f`, `fbf50f8`, `490a999`, `90e475a`); manual Mac check and Windows checks deferred. Open follow-up: the quota pre-check can't count space reserved by uploads in progress until `GET /me` reports it (`UserOut.reserved_bytes`, a contract change awaiting approval).

**Requirements:** FR-FILE-01 (My files; Shared with me empty until M8), FR-FILE-02 (amended), FR-FILE-03 (save a copy from the local synced file), FR-FILE-04 (create folder, delete; rename/move in M7), FR-FILE-05, FR-CLIENT-01 (progress summary, Open DropShare), FR-CLIENT-08, NFR-UX-01/02, NFR-PERF-03

**Scope:**
- Main window with navigation (My files, Shared with me placeholder), breadcrumbs, and a folder list loaded page by page from `GET /nodes/{id}/children`. A sync-state column comes from the local index (synced / uploading / downloading / waiting).
- Upload button and drag-and-drop, copying into the sync folder per FR-FILE-02, with the collision dialog and the space/quota pre-check.
- New folder and Delete performed on the local sync folder (OQ-23), with confirmation for delete. Save a copy to… and Show in folder.
- Sync-engine progress callbacks (throttled) feeding a Qt Transfers model; Transfers view; tray tooltip and menu summary.

**Done when:**
- pytest-qt tests (widgets found by accessible name) cover:
  - lazy paging and the four view states;
  - uploading a file and a folder lands in the right sync-folder path and appears in Transfers;
  - Keep both produces `name (1).ext`; Replace and Skip behave correctly;
  - insufficient space or quota blocks the copy with a message;
  - progress is monotonic and finishes at 100%, at no more than ~4 updates/s per item;
  - the tray tooltip text matches the summary.
- Manually on the Mac, dragging a ~1 GB file into the app shows progress in Transfers and the tray, and the second profile receives the file.
- A Windows check list is provided (Explorer drag-and-drop, 150% scaling, keyboard-only use).

### M7 — Renames and moves · medium

**Status:** ✅ Done 2026-10-28 (`64cdcc6`, `ae668e5`, `dc86629`, rename and move in the browser in `90e475a`); Windows checks deferred.

**Requirements:** FR-SYNC-10, FR-FILE-04 (rename, move, drag-to-move, "Move to…"), FR-CONF-03 (rename/move collisions), NFR-REL-01 (rename/move cases). Resolves the OQ-15 rename interim.

**Scope:**
- `PATCH /nodes/{id}` for rename and move within a namespace (cross-namespace moves arrive with M8).
- Local move detection by file ID (falling back to size + hash), applying remote moves, case-only renames on both sides.
- Rename and move in the browser, through the sync folder.

**Done when:**
- Server tests cover:
  - rename; move;
  - `NAME_CONFLICT` (case-insensitive) and `REVISION_CONFLICT`;
  - moving a folder into its own descendant is rejected;
  - another user's node returns 404;
  - a change row and an audit row for each write.
- Engine tests extend the matrix with rename and move against every other change on both sides.
- Node IDs are preserved (no trash + upload), and renaming a folder with 1,000 files sends one `PATCH`.
- A Windows check list is provided (case-only rename on NTFS, renaming a file open in Office).

### M8 — Sharing · large

**Status:** ✅ Done 2026-10-29 (server `c15199f`, `b446dec`, `eb7a7f7`; client commits to follow). Decisions OQ-30 … OQ-36. Manual two-profile test and Windows checks deferred. Known follow-ups:
- a file shared on its own shows as shared only in Shared with me, not in My files (needs a contract field);
- one message for links to items you can't access or that are gone (the server answers 404 for both);
- the new-share notification can't be turned off until Settings (M11).

**Requirements:** FR-SHARE-01 … FR-SHARE-08, FR-FILE-01 (Shared with me), FR-ADMIN-01 (group commands), FR-AUDIT-01 (sharing events), FR-CLIENT-02 (new-share notification only)

**Scope:**
- **Server:**
  - groups model and CLI (create, delete, add/remove member, list);
  - user and group search;
  - converting a folder into a shared namespace (IDs preserved);
  - member endpoints, file shares, Shared with me, and mounts (accept/leave);
  - one effective-role authorization helper;
  - quota charged to the owner;
  - cross-namespace moves.
- **Client:**
  - Share dialog: search, role picker, member list with change/remove, Copy link;
  - Shared with me view;
  - accepting a share mounts it in the sync folder; leaving unmounts it;
  - viewer read-only handling (FR-SHARE-05) and access-removed handling;
  - link handling via the single-instance socket.
  - Scheme registration is done by the installer in M13; until then, a dev-only script registers the link scheme on the Windows test machine.

**Done when:**
- Server tests cover:
  - viewers get 403 on every write endpoint; editors succeed; non-members get 404;
  - access via a group is granted and revoked correctly;
  - only owners manage sharing; an editor re-sharing gets 403;
  - search enforces minimum length and the 20-result limit, and excludes disabled users;
  - quota is charged to the owner;
  - audit rows for every sharing action.
- Client tests cover the Share dialog (pytest-qt).
- Engine tests cover:
  - mount accept and leave;
  - a viewer's local edit becomes a conflicted copy in their own root, and the original is restored;
  - access removal with and without unsynced local changes.
- Manually, with two profiles: share a folder as editor and both edit; switch to viewer; remove access.
- A Windows check list is provided (read-only attribute, opening a link).

### M9 — Trash and cleanup jobs · medium

**Status:** ✅ Done 2026-10-30 on `feat/m9` (`26e5caf`, `ba5a020`, `b8d5c99`). Decisions OQ-37, OQ-38; audit `docs/session-audits/2026-10-29-1452.md`. Windows pass on the Trash view deferred.

**Requirements:** FR-TRASH-01 (shared folders), FR-TRASH-02/03/04, NFR-REL-05, FR-AUDIT-03 (retention path)

**Scope:**
- Trash view in the app with restore, permanent delete, and empty trash.
- Jobs: `purge-trash`, `expire-uploads`, `gc-blobs`, `purge-audit`.

**Done when:** tests cover restore to the original path, restore with a recreated parent, the `(restored)` suffix, permanent delete freeing quota, and every job being idempotent and batched; the Trash view handles all four states.

### M10 — Transfer robustness · medium

**Status:** ✅ Done 2026-10-31 on `feat/m10` (`20a54ec`, `179e26f`, `4e9241c`, `4b9dd77`, `eabea19`). Decisions OQ-39, OQ-40; audit `docs/session-audits/2026-10-30-1530.md`. Deferred to the end: full-scale NFR-PERF-05 (50,000 files / 50 GB), NFR-PERF-04 over a real network, and the Windows check (a locked file waits in Transfers; `uv run pytest -m windows_only client/tests`).

**Requirements:** FR-XFER-01/02 (resume across restarts), FR-XFER-03, FR-SYNC-06 (locked files shown as "waiting" in Transfers), NFR-REL-01 (full matrix), NFR-PERF-01/02/04/05

**Done when:**
- Killing the app mid-upload and mid-download resumes from the last chunk/byte.
- Small files overtake a large one in the queue.
- Locked files show as "waiting" and complete once released.
- Performance tests record the NFR-PERF numbers.

### M11 — App completeness · medium

**Status:** ✅ Done 2026-11-02 on `feat/m11` (`cc5e240`, `7752e16`). Decisions OQ-41 … OQ-45; audit `docs/session-audits/2026-11-02-0044.md`. The Windows accessibility and display-scaling pass is deferred.

**Requirements:** FR-CLIENT-02, FR-CLIENT-03 (Activity view), FR-CLIENT-04, FR-CLIENT-06, FR-SYNC-07 (Can't sync view), FR-AUTH-07, FR-SYNC-01 (full folder rules), NFR-UX-02 (accessibility and display-scaling pass on Windows)

**Done when:** each view handles all four states and has pytest-qt coverage, and the accessibility/display-scaling pass is completed on the Windows machine at 100/150/200% in light, dark, and high contrast.

### M12 — Admin and security · small–medium

**Status:** ✅ Done 2026-11-02 on `feat/m11`. Decisions OQ-14 (resolved), OQ-46, OQ-47; audit `docs/session-audits/2026-11-02-0207.md`. MFA stays P2 (OQ-9 still open). Release publishing via the CLI arrives with M13. Deferred: the proxy on staging, the Oracle run, and the Windows dialog check.

**Requirements:** FR-ADMIN-01 (quota, storage report, audit export filters), NFR-SEC-04 (common-password list, OQ-14), NFR-SEC-06 (proxy rate limits, HSTS); MFA only if OQ-9 is answered "v1".

### M13 — Distribution · large

**Requirements:** FR-UPD-01 … FR-UPD-05 (installer with link-scheme registration, signing per OQ-6, auto-update, releases via the CLI), FR-CLIENT-02 (update available), FR-CLIENT-04/OQ-41 (Check for updates), FR-CLIENT-05 (built-in server URL). Decisions OQ-48, OQ-49.

**Blocked for real distribution:** OQ-6 (signing certificate) and OQ-8 (production DNS name and TLS). Until then, builds are signed with a self-signed test certificate and published to the dev server.

**Scope:**
- **Server:** `client_releases` table with installers in the `StorageBackend` (gc-blobs keeps them); `releases publish|list|withdraw` and `users set-channel` CLI; `GET /client/releases/latest` and `/{id}/installer` (ranged); version-check exemptions and the effective minimum version (OQ-49).
- **Client:** build info (server URL, expected publisher); Authenticode check in `platform/windows`; updater service (resumable download, SHA-256 and signature check); update controller (startup + every 6 h); Check for updates in Settings, the update-available notification, Install now / On next restart, Update on the 426 screen.
- **Packaging:** PyInstaller spec, Inno Setup script (per-user, scheme, `AppMutex`, uninstall prompt), `scripts/build_client.ps1`. Replaces `scripts/dev/register_link_scheme.ps1`.

**Done when:**
- Server tests cover publish rules (higher version, checksum, signature refused), withdrawn releases hidden, the channel per user, ranged installer download, and a too-old client still reaching the release endpoints.
- Updater tests cover resume, a hash mismatch, a bad signature, and the check repeated just before install; pytest-qt covers the update UI states.
- On Windows: install an older test-signed build, publish a newer one, and the client updates with sync state intact; uninstall keeps the sync folder.

### M14 — Ops and go-live · medium

**Requirements:** CI on Linux and Windows (OQ-11), `pip-audit` (needs approval), NFR-REL-06 (backups and a tested restore), staging/production (OQ-2, OQ-8), S3 backend if chosen.
