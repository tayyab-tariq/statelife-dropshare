# StateLife DropShare — Roadmap

> **Approved:** 2026-10-26 · Source of truth for scope: `docs/requirements.md`.
> Goal for the first build day: a working end-to-end slice. A user signs in on the desktop app,
> files sync both ways between the app and the server, and the basics are audited. Scope is cut
> before data-safety rules are (conflict copies, Recycle Bin, index-after-success, mass-delete
> protection, journaled crash recovery).

Every milestone ends with `/audit` and `/commit`.

## Agreed decisions for this slice

Recorded in requirements §15 (OQ-12 … OQ-15).

| # | Decision |
|---|---|
| OQ-7 | `DROPSHARE_DEV` and `DROPSHARE_TEST` don't exist yet; they are created per `docs/oracle-setup.md` §4 as the first step of M1. |
| OQ-12 | The client accepts `http://` only for `localhost`/`127.0.0.1` (local dev); HTTPS everywhere else. |
| OQ-13 | Dev-only `--profile <name>` flag: separate app-data folder, sync root, credential entry, and single-instance name, so two clients can run on one Mac. |
| OQ-14 | Password policy enforces the 12–128 length rules now; the common-password list is added when a source is supplied. |
| OQ-15 | Interim behaviour until follow-ups land: a local rename/move is sent as trash old + upload new (no data loss; FR-SYNC-10 deferred); conflict notifications use the tray only (Activity view later); uploads resume within a session but not across app restarts. |

Dependencies stay within the approved baseline: `argparse` for the CLI, a basic email shape
check (no `email-validator`), and `ctypes` for the Recycle Bin (no `send2trash`).

## Today's milestones

### M1 — Scaffolding · medium

**Requirements:** D16, D19, NFR-MAINT-01/02/03, NFR-COMPAT-01/02/05/06, NFR-OPS-01/02/03, NFR-SEC-07/09, OQ-7

**Scope:**
- Oracle dev and test schemas created and verified (oracle-setup.md §4–5).
- Fix `.gitignore` (stop ignoring `docs/`, add key/cert/cache/db patterns) and add `.gitattributes`.
- uv workspace (`shared`, `server`, `client`) with ruff, mypy, and pytest config.
- `shared`: response envelope, `ErrorCode`, `naming.py`.
- `server`: app factory, settings, `AppError` and handlers, request ID, JSON logs, `/health` and `/health/ready`, SQLAlchemy engine, Alembic `env.py` with the naming convention, `StorageBackend` and the local backend.
- `client`: an app that starts and quits, with logging and the platform-layer protocols.

**Done when:**
- `uv sync`, `ruff check`, `ruff format --check`, and `mypy --strict` pass.
- Table-driven `naming` tests pass (reserved names, bad characters, trailing dot/space, 255 limit, NFC + casefold).
- `/health/ready` returns 200 against `DROPSHARE_DEV`.
- `alembic upgrade head` runs on `DROPSHARE_TEST`.
- The client launches and exits cleanly on the Mac.

### M2 — Accounts, auth, and audit (server + CLI) · large

**Requirements:** FR-AUTH-01, FR-AUTH-02 (server side), FR-AUTH-03, FR-AUTH-04 (server side), FR-AUTH-05, FR-AUTH-06 (lockout), FR-ADMIN-01 (subset), FR-AUDIT-01 (auth, admin, denied), FR-AUDIT-02, FR-AUDIT-03 (insert-only in code), FR-UPD-04, NFR-SEC-02/03/04 (length rules)/05

**Scope:**
- Models: users, devices, refresh tokens, audit events, personal namespaces.
- Endpoints: `POST /auth/login`, `/auth/refresh`, `/auth/logout`; `GET /me`; `POST /me/password`.
- CLI: create user, reset password, disable/enable, list users, export audit to CSV.
- Lockout after 5 failures, refresh-token rotation with family reuse detection, `X-Client-Version` check returning 426.

**Done when:**
- TestClient tests per endpoint cover the happy path, 400, 401, 423 lockout, 426, disabled account, a reused rotated token (revokes the family), and a password change (revokes other sessions).
- Each test asserts the matching audit row, and no secrets appear in responses or audit details.
- A CLI-created user must change their password before getting a usable session.

### M3 — Files, transfers, and change journal (server) · large

**Requirements:** FR-XFER-01 (server), FR-XFER-02 (server), FR-SYNC-03 (server), FR-TRASH-01 (own namespace), FR-FILE-04 (create folder, delete), FR-AUDIT-01 (file events), NFR-REL-02/03/04, NFR-SCALE-01/03

**Scope:**
- Models: nodes (with `live_key`), file versions, upload sessions, changes.
- `GET /namespaces`, `GET /nodes/{id}`, `GET /nodes/{id}/children`, `POST /nodes`, `DELETE /nodes/{id}`.
- Upload endpoints: `POST /uploads`, `GET /uploads/{id}`, `PUT /uploads/{id}/chunks/{index}`, `POST /uploads/{id}/complete`, `DELETE /uploads/{id}`.
- `GET /nodes/{id}/content` with `Range`.
- `GET /namespaces/{id}/changes` and `POST /changes/wait`.

**Done when** tests cover:
- chunk and file hash mismatches
- a repeated `complete` returning the same node
- `409 REVISION_CONFLICT` and `NAME_CONFLICT` (case-insensitive)
- another user's node returning 404
- `QUOTA_EXCEEDED` and `FILE_TOO_LARGE`
- Range responses (206 and 416)
- long-poll returning on a new change and timing out otherwise
- a change row and an audit row for every write

### M4 — Desktop sign-in and shell · medium

**Requirements:** FR-AUTH-01, FR-AUTH-02, FR-AUTH-04, FR-AUTH-05 (forced change), FR-CLIENT-01 (states, Pause, Quit), FR-CLIENT-05, FR-CLIENT-07, FR-SYNC-01 (default path and chooser), NFR-COMPAT-04, NFR-UX-01/02

**Scope:**
- `ApiClient` with a single locked token refresh.
- Platform layer (credential store, paths, Recycle Bin, file ID) with macOS dev and Windows implementations.
- Sign-in dialog, change-password dialog, sync-folder chooser, tray.
- Sign-out with a keep/remove local files choice.
- Dev `--profile` flag (OQ-13).

**Done when:**
- pytest-qt tests (widgets found by accessible name) cover the generic invalid-credentials message, the disabled-account message, the forced password change, and sign-out clearing the credential.
- Quitting and relaunching resumes without asking for the password.
- `windows_only` tests exist for the credential store and Recycle Bin, and a Windows check list is provided.

### M5 — Sync engine · large

**Requirements:** FR-SYNC-02 (create/edit/delete for files and folders), FR-SYNC-03, FR-SYNC-04, FR-SYNC-05, FR-SYNC-06, FR-SYNC-07 (ignore list), FR-SYNC-08, FR-SYNC-09, FR-CONF-01, FR-CONF-02, FR-CONF-03 (folder deleted while files added), FR-XFER-01 (client, within a session), NFR-REL-01 (excluding rename/move)

**Scope:**
- SQLite index: entries, cursors, journal, activity.
- Startup scan, watcher with debounce, 2 s stability check, ignored names.
- Pure reconciler.
- Executor: folder create, chunked upload, temp-file download verified by hash then `os.replace`, local delete to server trash, remote delete to the Recycle Bin, conflict copies, mass-delete pause.
- Journal-based crash recovery, long-poll thread with offline backoff, pause/resume.

**Done when:**
- Table-driven tests cover {none, create, modify, delete} × {none, create, modify, delete} for files and folders, plus a crash at every executor step, a retry after a timeout, and the mass-delete thresholds.
- The `sync-reviewer` reports no unresolved blockers.
- Manually, two profiles see each other's changes in under 10 s, conflicts produce copies, and remote deletes land in the Trash.
- A Windows check list is provided.

## Follow-up milestones (rest of v1)

- **F1 Renames and moves:** `PATCH /nodes`, FR-SYNC-10 identity detection, applying remote moves, case-only renames, FR-CONF-03 rename collisions.
- **F2 Transfer robustness:** upload/download resume across restarts, FR-XFER-03 concurrency and prioritisation, locked files shown as "waiting", full NFR-REL-01 matrix, NFR-PERF tests.
- **F3 Trash and jobs:** FR-TRASH-02/03/04, `expire-uploads`, `gc-blobs`, `purge-audit`, FR-AUDIT-03 retention path.
- **F4 Sharing:** groups (CLI), FR-SHARE-01…07, mounts, viewer read-only, quota for shared content, link scheme.
- **F5 App completeness:** file browser (FR-FILE-01…05), Activity and Can't-sync views, Settings, devices (FR-AUTH-07), notifications (FR-CLIENT-02), diagnostics export, full FR-SYNC-01 rules, accessibility/HiDPI pass on Windows.
- **F6 Admin and security:** remaining CLI commands (groups, quota, storage report, audit export filters), common-password list (OQ-14), proxy rate limits and HSTS (NFR-SEC-06).
- **F7 Distribution:** FR-UPD-01…05 (installer, signing per OQ-6, auto-update, releases).
- **F8 Ops and go-live:** CI on Linux and Windows (OQ-11), `pip-audit` (needs approval), backups and a tested restore (NFR-REL-06), staging/production (OQ-2, OQ-8), S3 backend if chosen.
