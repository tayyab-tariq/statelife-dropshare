# StateLife DropShare — Requirements

> **Status:** Draft v0.3 · **Last updated:** 2026-10-26
> **Product name:** StateLife DropShare ("DropShare" for short in the UI). Python packages use the `dropshare_` prefix.
>
> This document is the **source of truth** for what we are building. Every task, plan, and
> commit should trace back to a requirement ID in this file (for example `FR-SYNC-03`).
> If code and this document disagree, raise it; do not silently pick one.

---

## Contents

1. [Purpose and goals](#1-purpose-and-goals)
2. [Confirmed decisions](#2-confirmed-decisions)
3. [Users and roles](#3-users-and-roles)
4. [System overview](#4-system-overview)
5. [Release phases](#5-release-phases)
6. [Functional requirements](#6-functional-requirements)
7. [Non-functional requirements](#7-non-functional-requirements)
8. [Data model (conceptual)](#8-data-model-conceptual)
9. [API overview](#9-api-overview)
10. [Sync design](#10-sync-design)
11. [File storage design](#11-file-storage-design)
12. [Environments and deployment](#12-environments-and-deployment)
13. [Phase 2 and later backlog](#13-phase-2-and-later-backlog)
14. [Out of scope](#14-out-of-scope)
15. [Open questions](#15-open-questions)
16. [Glossary](#16-glossary)

---

## 1. Purpose and goals

We are building a self-hosted, Dropbox-style file sync and sharing system for our organization.
Files live on our own on-prem infrastructure, not a public cloud. Users install a Windows
desktop app that keeps a local folder in two-way sync with the server and also offers a file
browser for working with files directly.

### Goals

- **G1. Own our data.** All file content and metadata stay on on-prem servers we control.
- **G2. Feel like Dropbox.** Drop a file in a folder and it appears on your other devices and for the people you share it with, without thinking about it.
- **G3. Never lose a file.** Sync must favour keeping data over being tidy: conflicts produce copies, deletions go to trash, and nothing is overwritten silently.
- **G4. Secure over the internet.** The server is reachable over the public internet, so every request is authenticated, encrypted in transit, authorized on the server, and audited.
- **G5. Stay portable.** Oracle 19c today, but the database must be swappable via the ORM. Windows today, but macOS must be addable without rewriting the core.
- **G6. Be ready for version history.** Version history is not in v1, but the data model and storage must support adding it without migration pain.

### Success criteria for v1

- 50–100 users can install the app, sign in, and sync files reliably across at least two Windows machines each.
- A file saved on machine A appears on machine B within 10 seconds on a normal connection (see NFR-PERF-02).
- Folder sharing with users and groups works with viewer and editor roles.
- Zero confirmed data-loss incidents during the pilot.
- Every security-relevant action is recorded in the audit log and can be exported by admins.

---

## 2. Confirmed decisions

Decisions agreed with the project owner. Changing any of these requires updating this table.

| # | Topic | Decision |
|---|---|---|
| D1 | Architecture | Desktop client ↔ **FastAPI backend** ↔ Oracle + file storage. The client never connects to the database. |
| D2 | Backend | **Python 3.12, FastAPI, SQLAlchemy 2.0 (ORM), Alembic (migrations)**. |
| D3 | Database | **Oracle Database 19c** via the `python-oracledb` driver. All access through SQLAlchemy so the DB can change later. |
| D4 | Desktop framework | **PySide6 (Qt 6)**. Chosen over PyQt6 because it is LGPL-licensed (PyQt6 is GPL or paid commercial); the APIs are nearly identical. |
| D5 | Client OS | **Windows 10/11 (64-bit)** in v1. macOS planned later; OS-specific code is isolated behind a platform layer. |
| D6 | File storage | **Local disk in development.** Production backend is pluggable (filesystem on NAS/SAN, or S3-compatible such as MinIO). Final choice **deferred to deployment** (OQ-2). |
| D7 | App modes | **Both** a background two-way sync folder and an in-app file browser. |
| D8 | Conflicts | **Keep both** versions; one is saved as a "conflicted copy". |
| D9 | Sharing | **Yes**: share with users and groups, with permission levels. Share links are **internal only** (OQ-1). |
| D10 | Trash | **Yes** in v1. **Version history** later, but designed for now (G6). |
| D11 | Selective sync, offline extras, bandwidth limits | **Phase 2.** |
| D12 | Accounts | **Stored in Oracle** (local accounts). 50–100 users initially; must scale beyond. In v1, accounts are created directly against the database with a server CLI; the in-app admin UI is **Phase 2** (OQ-3). |
| D13 | Network | Clients connect **over the public internet**. |
| D14 | Audit logs | **Required** in v1. |
| D15 | Distribution | **Installer, code signing, and auto-update** all required in v1. |
| D16 | Tooling | **uv** (dependencies/workspace), **ruff** (lint/format), **mypy** (types), **pytest**. Monorepo with `client/`, `server/`, `shared/`. |
| D17 | Planning | Requirements split into **v1** and **later phases**. |
| D18 | Limits | 10 GB max file, 50 GB default quota, 5 TB initial storage, shared content counts against the owner, 30-day trash, 1-year audit retention (OQ-4). |
| D19 | Platforms | Server: **Linux in production**; developed on **macOS**. Desktop client: developed on **macOS**, tested and built on a **Windows** machine. All server code and the client's non-OS-specific code must run on macOS, Windows, and Linux. |
| D20 | Product name | **StateLife DropShare**. |

---

## 3. Users and roles

| Role | Who | Can do |
|---|---|---|
| **User** | Any employee with an account | Sync and manage their own files, share folders and files they own, use trash, manage their devices. |
| **Admin** | IT / system administrators | In v1: manage users, groups, and quotas, view storage usage, and export audit logs **via the server CLI** (requires server access). In Phase 2: the same from an Administration section in the app. Admins do **not** get access to other users' file content. |
| **Share recipient** | A User who has been given access | Access shared items according to their role (Viewer or Editor). |

Roles are stored on the user account. Authorization is always enforced on the server.

---

## 4. System overview

```
 ┌──────────────────────────────┐                ┌───────────────────────────────────────────┐
 │  Windows desktop client      │                │  On-prem                                  │
 │  (PySide6)                   │    HTTPS       │                                           │
 │                              │   (TLS 1.2+)   │  Reverse proxy (TLS termination,          │
 │  • Tray icon + status        │ ─────────────► │  rate limits)                             │
 │  • File browser UI           │   internet     │        │                                  │
 │  • Sync engine (background)  │                │        ▼                                  │
 │  • Local index (SQLite)      │                │  FastAPI app server(s)                    │
 │  • Credential store          │                │   • REST API /api/v1                      │
 │    (Windows Credential Mgr)  │                │   • Long-poll change notifications        │
 │  • Auto-updater              │                │   • Background jobs (CLI, scheduled)      │
 └──────────────────────────────┘                │        │                    │             │
                                                 │        ▼                    ▼             │
                                                 │  Oracle 19c            File storage       │
                                                 │  (metadata, users,     (local disk dev /  │
                                                 │   shares, audit)        NAS · SAN · S3)   │
                                                 └───────────────────────────────────────────┘
```

**Key principles**

- The database holds **metadata** (names, folders, owners, permissions, versions, audit). The storage holds **file content only**, as opaque blobs keyed by ID. Paths and names never appear in storage keys, so renames and moves are metadata-only operations.
- Request and response models are defined once in `shared/` (Pydantic) and used by both client and server, so the contract cannot drift.
- The server is the source of truth. The client keeps a local index to know what it last synced.

---

## 5. Release phases

| Phase | Theme | Contents |
|---|---|---|
| **v1 (MVP)** | Reliable sync and sharing for the pilot | Everything tagged `v1` in sections 6–7. |
| **Phase 2** | Power features and hardening | Version history, selective sync, files-on-demand, bandwidth limits, MFA, file search, previews, macOS client, Explorer integration. See §13. |
| **Later** | Scale and integrations | LAN sync, delta/block sync, SSO/LDAP, web client, mobile. See §13. |

Each requirement below carries a phase tag: `v1`, `P2`, or `Later`.

---

## 6. Functional requirements

Format: **ID — Title** `phase`, a short description, then **Acceptance** criteria. Every
acceptance criterion must be testable.

### 6.1 Authentication and sessions (AUTH)

**FR-AUTH-01 — Sign in with username/email and password** `v1`
Users sign in from the desktop app with their username or email and password.
Acceptance:
- Valid credentials return an access token and refresh token; the app shows the main window.
- Invalid credentials return a generic "Invalid username or password" (no hint whether the user exists).
- Disabled accounts cannot sign in and get a clear "Account disabled, contact your administrator" message.

**FR-AUTH-02 — Stay signed in across restarts** `v1`
Acceptance:
- The refresh token is stored in Windows Credential Manager, never in plain files.
- After a reboot the app resumes syncing without asking for the password, until the refresh token expires or is revoked.

**FR-AUTH-03 — Token lifecycle** `v1`
Acceptance:
- Access tokens expire after 15 minutes; refresh tokens after 30 days of inactivity and 90 days absolute.
- Refresh tokens rotate on every use. Re-use of an already-rotated token revokes the whole token family and is audited.

**FR-AUTH-04 — Sign out** `v1`
Acceptance:
- Sign out revokes the device's refresh token on the server and removes it from Credential Manager.
- The user chooses whether to keep or remove the local sync folder contents.

**FR-AUTH-05 — Change password** `v1`
Acceptance:
- Requires the current password. New password must meet NFR-SEC-04.
- All other sessions of the user are revoked after a password change.

**FR-AUTH-06 — Brute-force protection** `v1`
Acceptance:
- After 5 consecutive failed attempts an account is locked for 15 minutes. Lock and unlock are audited.
- Per-IP rate limiting on the login endpoint (see NFR-SEC-06).

**FR-AUTH-07 — Device management** `v1`
Acceptance:
- Users can list their signed-in devices (name, last seen, client version) and revoke any of them.
- A revoked device stops syncing on its next request and returns to the sign-in screen.

**FR-AUTH-08 — Multi-factor authentication (TOTP)** `P2` (see OQ-9)

### 6.2 Administration (ADMIN)

In v1 there is no admin UI. IT staff with access to the server run a CLI that writes directly to the
database through the same services the API uses, so passwords are hashed correctly and every action
is validated and audited. (Plain SQL inserts can't be used for accounts because passwords must be
stored as Argon2id hashes.) The in-app Administration section is Phase 2.

**FR-ADMIN-01 — Server administration CLI** `v1`
Commands (exact names decided at implementation): create user, reset password, disable/enable user, set quota, list users; create/delete group, add/remove group member, list groups; storage report; export audit events to CSV; publish client release (FR-UPD-05).
Acceptance:
- Creating a user sets username, email, display name, role (user/admin), and quota (default 50 GB). It prints a one-time temporary password, and the user must change it on first sign-in (FR-AUTH-05).
- Usernames and emails are unique, case-insensitive; violations print a clear error.
- Disabling a user revokes all their sessions immediately; their files and shares are kept.
- Removing a user from a group removes access granted via that group on the user's next sync cycle.
- Audit export supports filters for date range, user, action, and outcome.
- Every CLI action is audited with actor `cli` plus the operating-system user who ran it.
- The CLI refuses to run if the database configuration is missing or invalid.

**FR-ADMIN-02 — Manage users from the app** `P2`
**FR-ADMIN-03 — Manage groups from the app** `P2`
**FR-ADMIN-04 — Quotas and storage overview in the app** `P2`
**FR-ADMIN-05 — Audit log viewer in the app (filter, paginate, export)** `P2`
**FR-ADMIN-06 — Administration section in the desktop app, visible only to admins** `P2`

### 6.3 Files and folders in the file browser (FILE)

**FR-FILE-01 — Browse** `v1`
Acceptance:
- The browser shows the user's own files, folders shared with them, and a "Shared with me" view.
- Each item shows name, size, modified date, modified by, and sharing state.
- Large folders load page by page without freezing the UI.

**FR-FILE-02 — Upload** `v1`
Acceptance:
- Upload via button and drag-and-drop, for files and whole folders.
- Uploads show progress, can be cancelled, and resume after network loss or app restart (FR-XFER-01).

**FR-FILE-03 — Download** `v1`
Acceptance:
- Download files and folders to a chosen location, with progress and resume.

**FR-FILE-04 — Create, rename, move, delete** `v1`
Acceptance:
- Create folder, rename, move (including drag-and-drop), and delete (to trash) work for files and folders.
- Names are validated against NFR-COMPAT-02 before sending; invalid names show the reason.
- Name collisions in the same folder are rejected (case-insensitive) with a clear message.
- Concurrent edits are detected: an operation based on stale data returns a conflict and the UI refreshes.

**FR-FILE-05 — Open in sync folder** `v1`
Acceptance:
- "Show in folder" opens Windows Explorer at the synced local copy, when it exists.

### 6.4 Sync folder (SYNC)

**FR-SYNC-01 — Choose sync folder on first run** `v1`
Acceptance:
- Default is `%USERPROFILE%\StateLife DropShare`. The user can choose another local NTFS folder.
- Network drives, removable drives, and folders inside another sync tool's folder are rejected with a reason.

**FR-SYNC-02 — Two-way sync** `v1`
Acceptance:
- Creating, editing, renaming, moving, or deleting files or folders locally is reflected on the server, and vice versa.
- Remote changes are applied without user action while the app is running.

**FR-SYNC-03 — Near-real-time propagation** `v1`
Acceptance:
- Clients learn about remote changes via long-poll notifications (see §10) and apply them within NFR-PERF-02 targets.

**FR-SYNC-04 — Offline changes** `v1`
Acceptance:
- Changes made while offline or while the app is closed are detected (full scan on startup and reconnect) and synced when connectivity returns.
- Note: in v1 the sync folder holds full local copies, so synced files are always available offline. "Files on demand" is P2 (FR-SYNC-20).

**FR-SYNC-05 — Pause and resume** `v1`
Acceptance:
- Users can pause sync from the tray menu; status shows "Paused". Resume continues where it stopped.

**FR-SYNC-06 — Safe file handling** `v1`
Acceptance:
- Files still being written are not uploaded until they have been stable (unchanged size and modified time) for 2 seconds.
- Files locked by another program (for example open in Office) are retried with backoff and shown as "waiting" rather than failing.
- Downloads are written to a temporary file, verified by SHA-256, then moved into place atomically. A partially downloaded file never appears under its real name.

**FR-SYNC-07 — Ignored files** `v1`
Acceptance:
- These are never synced: `desktop.ini`, `Thumbs.db`, `~$*`, `*.tmp`, `.~lock.*`, `.DS_Store`, and the client's own temp folder.
- Items that cannot be synced (invalid name for Windows, path too long, over quota) are listed in a "Can't sync" view with the reason and a suggested fix.

**FR-SYNC-08 — Mass-delete protection** `v1`
Acceptance:
- If one sync cycle would delete more than 500 files or more than 25% of the synced files (either direction), the client pauses and asks the user to confirm.

**FR-SYNC-09 — Remote deletes go to the Recycle Bin** `v1`
Acceptance:
- When a file is deleted on the server, the local copy is moved to the Windows Recycle Bin, not permanently deleted.

**FR-SYNC-10 — Move and rename detection** `v1`
Acceptance:
- A local rename or move is sent as a move, not as delete + re-upload, when the file identity can be determined (NTFS file ID plus content hash).

**FR-SYNC-20 — Selective sync** `P2`
**FR-SYNC-21 — Files on demand (placeholders via Windows Cloud Files API)** `P2`
**FR-SYNC-22 — Bandwidth limits** `P2`
**FR-SYNC-23 — LAN sync** `Later`
**FR-SYNC-24 — Delta / block-level sync** `Later`

### 6.5 Conflicts (CONF)

**FR-CONF-01 — Keep both on conflict** `v1`
A conflict occurs when the same file changes locally and on the server since the last sync.
Acceptance:
- The server version keeps the original name. The local version is saved alongside as
  `Name (conflicted copy – Display Name – YYYY-MM-DD HH-MM).ext` and uploaded.
- The user gets a notification and the conflict appears in the activity view.
- No content is lost in any interleaving of edits (verified by automated tests, NFR-REL-01).

**FR-CONF-02 — Edit vs delete** `v1`
Acceptance:
- If one side edits and the other deletes, the edit wins: the edited file is kept (restored on the deleting side).

**FR-CONF-03 — Folder-level conflicts** `v1`
Acceptance:
- Rename/move collisions and "folder deleted while files added inside" resolve without losing files; any orphaned files are restored into the nearest existing parent.

### 6.6 Transfers (XFER)

**FR-XFER-01 — Resumable chunked upload** `v1`
Acceptance:
- Files are uploaded in 8 MiB chunks. Each chunk carries its SHA-256; the server rejects corrupt chunks.
- After interruption, the client asks the server which chunks it has and sends only the missing ones.
- On completion the server verifies the whole-file SHA-256 before the new version becomes visible.
- Unfinished upload sessions expire after 24 hours and their partial data is cleaned up.

**FR-XFER-02 — Resumable download** `v1`
Acceptance:
- Downloads support HTTP Range requests and resume from the last byte received.

**FR-XFER-03 — Concurrency** `v1`
Acceptance:
- The client runs a bounded number of parallel transfers (default 4 files) and prioritizes small files so many small changes do not wait behind one large file.

### 6.7 Trash (TRASH)

**FR-TRASH-01 — Deleted items go to trash** `v1`
Acceptance:
- Deleting a file or folder moves it to the owner's trash, with who deleted it and when.
- Items deleted inside a shared folder go to the trash of the shared folder's owner and are visible to the deleting editor too.

**FR-TRASH-02 — Restore** `v1`
Acceptance:
- Restoring puts the item back at its original path. If the parent no longer exists it is recreated; if the name is taken the restored item gets a "(restored)" suffix.

**FR-TRASH-03 — Permanent delete and empty trash** `v1`
Acceptance:
- Owners can permanently delete individual items or empty their trash. Permanent deletion is audited and frees quota.

**FR-TRASH-04 — Automatic purge** `v1`
Acceptance:
- Items older than the retention period (default 30 days, configurable) are permanently deleted by a scheduled job.
- Trashed items count against quota until purged.

### 6.8 Sharing (SHARE)

**FR-SHARE-01 — Share folders with users and groups** `v1`
Acceptance:
- Owners share a folder with users and/or groups as **Viewer** (read/download) or **Editor** (also create, edit, rename, move, delete inside).
- Recipients are notified in the app and see the folder in "Shared with me".

**FR-SHARE-02 — Shared folders in the sync folder** `v1`
Acceptance:
- When a recipient adds a shared folder (accept), it appears at the top level of their sync folder and syncs like their own files.
- Recipients can remove a shared folder from their account ("leave") without affecting others.

**FR-SHARE-03 — Share individual files** `v1`
Acceptance:
- Owners share single files with users/groups as Viewer or Editor. Shared files appear in the recipient's "Shared with me" view in the file browser (not in the sync folder in v1).

**FR-SHARE-04 — Manage access** `v1`
Acceptance:
- Owners see everyone with access, change roles, and remove access. Changes take effect on the server immediately and on clients at their next sync cycle.
- In v1 only the owner can manage sharing; editors cannot re-share.

**FR-SHARE-05 — Viewer protection on disk** `v1`
Acceptance:
- Files in view-only shared folders are marked read-only on disk.
- If a viewer modifies one anyway, the change is not uploaded; the modified file is moved to the user's own root as a conflicted copy, the original is restored, and the user is notified.

**FR-SHARE-06 — Quota for shared content** `v1`
Acceptance:
- Content in a shared folder counts against the **owner's** quota.

**FR-SHARE-07 — Internal links** `v1`
Users can copy a link to any file or folder they can access and paste it into email or chat for colleagues.
Acceptance:
- "Copy link" puts a link of the form `statelife-dropshare://open?node=<id>` on the clipboard. The link contains only the item ID: no token, no name, no path.
- The installer registers the `statelife-dropshare://` scheme for the current user, so clicking the link opens DropShare (or brings the running instance to the front) at that item.
- A link never grants access. A recipient without access sees "You don't have access to this item. Ask the owner to share it with you."; a trashed or deleted item shows "This item is no longer available."
- The app validates the link strictly (scheme, `open` action, UUID) and ignores anything else.
- Public links for people without an account are out of scope (§14).

### 6.9 Audit log (AUDIT)

**FR-AUDIT-01 — Record security-relevant events** `v1`
Acceptance — at minimum these are recorded:
- Authentication: sign-in success/failure, sign-out, token refresh reuse detected, lockout/unlock, password change/reset.
- Files: upload complete, download, create folder, rename, move, delete to trash, restore, permanent delete.
- Sharing: share created/changed/removed, shared folder accepted/left.
- Admin: user/group created/changed/disabled, quota changed, audit export.
- Authorization failures (access denied).

**FR-AUDIT-02 — Event content** `v1`
Acceptance:
- Each event stores: timestamp (UTC), actor user, device, source IP, action, target type and ID, target name at the time, outcome (success/failure/denied), request ID, and small structured details.
- Events never contain file content, passwords, or tokens.

**FR-AUDIT-03 — Tamper resistance** `v1`
Acceptance:
- The application can only insert audit rows, never update or delete them (enforced in code and, where possible, by DB grants on the app user).
- Retention is configurable (default 1 year); purging old events is done by a scheduled job using a separate privileged path, and is itself logged.

### 6.10 Desktop app shell (CLIENT)

**FR-CLIENT-01 — System tray** `v1`
Acceptance:
- Tray icon shows state: Up to date, Syncing (n items), Paused, Offline, Error, Signed out.
- Tray menu: Open sync folder, Open DropShare, Recent activity, Pause/Resume, Settings, Quit.

**FR-CLIENT-02 — Notifications** `v1`
Acceptance:
- Windows notifications for: new share received, conflicts, items that can't sync, quota at 90% and 100%, update available. Each type can be turned off in settings.

**FR-CLIENT-03 — Activity view** `v1`
Acceptance:
- Shows recent sync events (uploaded, downloaded, deleted, conflicts, errors) with time and path, and lets the user open the item.

**FR-CLIENT-04 — Settings** `v1`
Acceptance:
- Account (signed-in user, sign out, change password), devices, sync folder location (with safe move), start at Windows sign-in (default on), notifications, about/version, check for updates.

**FR-CLIENT-05 — Server address** `v1`
Acceptance:
- The production server URL is built into the installer. It can be overridden in an advanced setting for testing.

**FR-CLIENT-06 — Diagnostics** `v1`
Acceptance:
- Logs are written to `%LOCALAPPDATA%\StateLife\DropShare\logs` with rotation. "Export diagnostics" creates a zip of logs and config without tokens or file content.

**FR-CLIENT-07 — Single instance** `v1`
Acceptance:
- Starting the app while it is already running brings the existing window to the front.

### 6.11 Installation and updates (UPD)

**FR-UPD-01 — Windows installer** `v1`
Acceptance:
- A signed installer installs per-user (no admin rights required) into `%LOCALAPPDATA%\Programs\StateLife DropShare`, creates Start-menu shortcuts and the start-at-sign-in entry, and registers the `statelife-dropshare://` link scheme (FR-SHARE-07).
- Uninstall removes the app but keeps the sync folder and asks whether to remove local app data.

**FR-UPD-02 — Code signing** `v1`
Acceptance:
- The application executable and installer are Authenticode-signed with the organization's certificate (OQ-6). Builds without a valid signature are not published.

**FR-UPD-03 — Auto-update** `v1`
Acceptance:
- The client checks the server for updates at startup and every 6 hours.
- Updates download in the background; before installing, the client verifies the SHA-256 from the release manifest **and** the Authenticode signature/publisher.
- The user can install now or on next restart. Sync state survives the update.

**FR-UPD-04 — Minimum supported version** `v1`
Acceptance:
- Every request carries the client version. If it is below the server's minimum supported version, the server returns `426 UPGRADE_REQUIRED`, sync stops safely, and the client offers the update.

**FR-UPD-05 — Release management** `v1`
Acceptance:
- Admins publish a release via the server CLI (FR-ADMIN-01), or a CI step calls the same command, with version, channel (stable/beta), minimum supported version, release notes, installer, and checksum.

---

## 7. Non-functional requirements

### 7.1 Security (SEC)

| ID | Requirement | Phase |
|---|---|---|
| NFR-SEC-01 | All traffic uses HTTPS with TLS 1.2+ (1.3 preferred) and HSTS. No plain-HTTP endpoints exposed. | v1 |
| NFR-SEC-02 | Every endpoint except `/health` and sign-in requires a valid access token. Authorization (ownership and share roles) is checked in the server's service layer on every request. | v1 |
| NFR-SEC-03 | Passwords hashed with **Argon2id**. Tokens signed with a server secret held outside the repo; refresh tokens stored only as hashes. | v1 |
| NFR-SEC-04 | Password policy: minimum 12 characters, maximum 128, rejected if on a bundled common-password list. No forced composition rules (per NIST SP 800-63B). | v1 |
| NFR-SEC-05 | Inputs validated by Pydantic models; unknown fields rejected. File names validated server-side. Path traversal impossible because storage keys are IDs, not paths. | v1 |
| NFR-SEC-06 | Rate limiting at the reverse proxy (global and stricter on auth endpoints) plus account lockout (FR-AUTH-06). | v1 |
| NFR-SEC-07 | Errors never expose stack traces, SQL, internal IDs of other users, or storage paths. | v1 |
| NFR-SEC-08 | Encryption at rest via encrypted storage volumes (NAS/SAN or object store). Oracle TDE only if licensed (OQ-10). | v1 |
| NFR-SEC-09 | Secrets (DB password, token secret, storage keys) come from environment/secret store, validated at startup. Never committed. | v1 |
| NFR-SEC-10 | Dependencies pinned via `uv.lock`; vulnerability scan (`pip-audit` or equivalent, needs approval) before each release. | v1 |
| NFR-SEC-11 | Antivirus / malware scanning of uploads. | P2 |
| NFR-SEC-12 | MFA (TOTP). | P2 (OQ-9) |

### 7.2 Performance and scale (PERF, SCALE)

Values confirmed (OQ-4). All limits are configuration, not code constants.

| ID | Requirement | Phase |
|---|---|---|
| NFR-PERF-01 | Metadata API calls: p95 < 300 ms at 100 concurrent active users. | v1 |
| NFR-PERF-02 | A small file saved on one device is visible on another online device within 10 s (p95). | v1 |
| NFR-PERF-03 | The UI thread is never blocked by network, disk, or hashing work; the window stays responsive with 200,000 files in the sync folder. | v1 |
| NFR-PERF-04 | Large transfers use at least 80% of available bandwidth on a stable connection. | v1 |
| NFR-PERF-05 | Initial sync of 50,000 files / 50 GB completes without user intervention and survives restarts mid-way. | v1 |
| NFR-SCALE-01 | Maximum file size: **10 GB** (configurable). | v1 |
| NFR-SCALE-02 | Up to 200,000 files per user (own + mounted shared folders). | v1 |
| NFR-SCALE-03 | Default quota: **50 GB per user**, adjustable per user by admins. | v1 |
| NFR-SCALE-04 | Initial storage provisioning: **5 TB usable**, expandable, alert at 75% full. (100 users × 50 GB is the worst case; typical use is 30–50% of quota, and trash/versions add overhead.) | v1 |
| NFR-SCALE-05 | Design supports growth to 1,000+ users by adding app servers, without data model changes. | v1 (design) |

### 7.3 Reliability and data integrity (REL)

| ID | Requirement | Phase |
|---|---|---|
| NFR-REL-01 | No data loss: sync engine has automated tests covering every local/remote change combination (create/modify/delete/move × both sides), including crash and restart mid-operation. | v1 |
| NFR-REL-02 | Every stored blob is verified by SHA-256 on upload; downloads are verified before being moved into place. | v1 |
| NFR-REL-03 | Server operations that change metadata are transactional; a new file version becomes visible only after its content is fully stored. | v1 |
| NFR-REL-04 | Idempotency: retrying any client request (including after timeouts) never duplicates files or corrupts state. | v1 |
| NFR-REL-05 | Scheduled jobs clean up expired uploads, orphaned blobs (no DB row after 24 h), and purge trash. | v1 |
| NFR-REL-06 | Backups: Oracle (RMAN or DBA-standard) and storage snapshots on a schedule agreed with infra; documented restore procedure tested before go-live. | v1 |

### 7.4 Compatibility and portability (COMPAT)

| ID | Requirement | Phase |
|---|---|---|
| NFR-COMPAT-01 | Database access only via SQLAlchemy ORM; no Oracle-specific SQL outside the migration layer. Schema changes only via Alembic. | v1 |
| NFR-COMPAT-02 | Server enforces cross-platform-safe names: no `< > : " / \ \| ? *`, no control characters, no trailing dot or space, no Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1–9`, `LPT1–9`), max 255 characters. Names unique per folder case-insensitively (Unicode NFC + casefold). | v1 |
| NFR-COMPAT-03 | Client supports paths longer than 260 characters (long-path aware), up to the server limit. | v1 |
| NFR-COMPAT-04 | OS-specific client code (credential store, autostart, recycle bin, file IDs, notifications) lives behind a platform interface so a macOS implementation can be added. A **development-grade macOS implementation** exists so the client runs on developers' Macs; it is not a supported or shipped macOS client (that remains BL-08). | v1 (design) |
| NFR-COMPAT-05 | Storage access only via a `StorageBackend` interface; switching local disk ↔ NAS ↔ S3 is configuration. | v1 |
| NFR-COMPAT-06 | The server runs on **macOS (development)** and **Linux (production)**, and also on Windows: `pathlib` for all paths, no OS-specific calls outside clearly isolated modules, no reliance on case-sensitive file systems. CI runs the server test suite on Linux. | v1 |
| NFR-COMPAT-07 | Windows-specific client behaviour (Credential Manager, Recycle Bin, NTFS file IDs, locked files, long paths, reserved names, link scheme, installer, auto-update) is verified on a real Windows machine before a change counts as done; automated Windows-only tests run there and in CI on a Windows runner. | v1 |

### 7.5 Usability and accessibility (UX)

| ID | Requirement | Phase |
|---|---|---|
| NFR-UX-01 | Every data-driven view shows loading, error (with retry), empty (with next action), and populated states. | v1 |
| NFR-UX-02 | Fully keyboard-operable; visible focus; screen-reader names on all controls; follows Windows light/dark and high-contrast settings; correct at 100–200% display scaling. | v1 |
| NFR-UX-03 | Error messages say what happened and what to do, in plain language. | v1 |

### 7.6 Observability and operations (OPS)

| ID | Requirement | Phase |
|---|---|---|
| NFR-OPS-01 | Structured (JSON) server logs with request ID, user ID, and timing; no secrets or file content. | v1 |
| NFR-OPS-02 | `/health` (liveness) and `/health/ready` (DB + storage reachable) endpoints. | v1 |
| NFR-OPS-03 | Configuration via environment variables validated at startup; the server refuses to start on invalid config. | v1 |
| NFR-OPS-04 | Metrics endpoint (Prometheus format) and dashboards. | P2 |

### 7.7 Maintainability (MAINT)

| ID | Requirement | Phase |
|---|---|---|
| NFR-MAINT-01 | `ruff` and `mypy --strict` pass; tests run in CI on every change. | v1 |
| NFR-MAINT-02 | Server layering: routers → services → repositories; only repositories use SQLAlchemy; only storage backends touch file content. | v1 |
| NFR-MAINT-03 | Client layering: UI → controllers → services/sync engine; UI code never makes HTTP calls or touches the local index directly. | v1 |

---

## 8. Data model (conceptual)

This is the conceptual model that implementation must satisfy. Exact columns are decided in code
and Alembic migrations. All tables use UUID primary keys (except append-only logs, which use an
increasing integer) and UTC timestamps.

| Entity | Purpose | Key fields |
|---|---|---|
| **User** | Account | username, email (both unique, case-insensitive), display name, password hash, role, status, quota bytes, failed-login count, locked-until, must-change-password |
| **Group** / **GroupMember** | Groups for sharing | name; user ↔ group |
| **Device** | A signed-in client install | user, name, OS, client version, last seen, revoked at |
| **RefreshToken** | Session tokens | user, device, token hash, family ID, expires, revoked, replaced by |
| **Namespace** | A sync root with its own change history: each user's personal space, and each shared folder | type (personal/shared), owner |
| **NamespaceMember** | Who can access a shared namespace | namespace, user or group, role (viewer/editor) |
| **Mount** | A recipient's accepted shared folder in their tree | user, namespace, mount name, accepted at |
| **Node** | A file or folder | namespace, parent, name, normalized name, kind, size, content hash, current version, revision (for optimistic concurrency), created/modified by/at, trashed at/by |
| **FileVersion** | One stored version of a file's content | node, blob key, size, SHA-256, created by/at |
| **FileShare** | A single file shared with users/groups (FR-SHARE-03) | node, user or group, role |
| **UploadSession** | In-progress resumable upload | user, target (parent + name, or node + base revision), size, chunk size, received chunks, expected hash, status, expires |
| **Change** | Append-only journal of node changes per namespace; its increasing ID is the sync cursor | namespace, node, change type, snapshot of node fields, actor, time |
| **AuditEvent** | Append-only audit log | see FR-AUDIT-02 |
| **ClientRelease** | Auto-update releases | version, platform, channel, min supported version, installer blob, SHA-256, notes, published at |

**Designed for version history (G6):** file content is never overwritten. Each save creates a
new `FileVersion` with its own blob, and `Node.current_version` points to the latest. In v1, a
retention policy keeps only the current version (older versions are garbage-collected). Adding
version history in Phase 2 means changing the retention policy and adding endpoints and UI,
not changing the schema or storage layout.

**Trash** is a state of `Node` (trashed at/by), not a separate table, so restore keeps IDs, shares, and history.

---

## 9. API overview

Base path `/api/v1`. JSON. All responses use one envelope:

```json
{ "data": { } }
{ "data": [ ], "meta": { "page": 1, "limit": 50, "total": 134 } }
{ "error": { "code": "REVISION_CONFLICT", "message": "Human-readable summary", "details": { } } }
```

Every request sends `X-Client-Version`; every response includes `X-Request-ID`.

| Area | Endpoints (v1) |
|---|---|
| Health | `GET /health`, `GET /health/ready` (outside `/api/v1`) |
| Auth | `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout` |
| Me | `GET /me`, `POST /me/password`, `GET /me/devices`, `DELETE /me/devices/{id}` |
| Namespaces & nodes | `GET /namespaces`, `GET /nodes/{id}`, `GET /nodes/{id}/children`, `POST /nodes` (create folder), `PATCH /nodes/{id}` (rename/move, requires `if_revision`), `DELETE /nodes/{id}` (to trash) |
| Uploads | `POST /uploads`, `GET /uploads/{id}`, `PUT /uploads/{id}/chunks/{index}`, `POST /uploads/{id}/complete`, `DELETE /uploads/{id}` |
| Download | `GET /nodes/{id}/content` (supports `Range`) |
| Sync | `GET /namespaces/{id}/changes?cursor=&limit=`, `POST /changes/wait` (long-poll over several namespace cursors) |
| Trash | `GET /trash`, `POST /trash/{node_id}/restore`, `DELETE /trash/{node_id}`, `DELETE /trash` |
| Sharing | `GET /nodes/{id}/members`, `POST /nodes/{id}/members`, `PATCH /members/{id}`, `DELETE /members/{id}`, `GET /shared-with-me`, `POST /mounts`, `DELETE /mounts/{namespace_id}` |
| Client updates | `GET /client/releases/latest?platform=&channel=`, `GET /client/releases/{id}/installer` |
| Admin | None in v1 (administration is the server CLI). Phase 2: `/admin/users`, `/admin/groups`, `/admin/storage`, `/admin/audit-events`, `/admin/releases`. |

Error codes are stable SCREAMING_SNAKE_CASE strings defined in `shared/`, for example
`VALIDATION_ERROR`, `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `NAME_CONFLICT`,
`REVISION_CONFLICT`, `QUOTA_EXCEEDED`, `FILE_TOO_LARGE`, `CHUNK_HASH_MISMATCH`, `FILE_HASH_MISMATCH`,
`ACCOUNT_LOCKED`, `UPGRADE_REQUIRED`, `RATE_LIMITED`.

---

## 10. Sync design

This section describes the intended behaviour; the `sync-engine` skill holds implementation rules.

### 10.1 Change detection

- **Server → client:** every metadata change appends a row to the `Change` journal of its namespace. The client stores one cursor per namespace and fetches changes after it. To learn about changes quickly, the client holds a **long-poll** request (`POST /changes/wait`, ~55 s timeout) that returns as soon as any of its namespaces has new changes.
  - Long-poll was chosen over WebSockets for v1 because it is stateless and works across multiple app server processes without a message broker. WebSockets plus a pub/sub broker is an upgrade path if needed.
- **Local → client:** a file-system watcher reports local changes, and a full scan runs at startup, on reconnect, and periodically (default every 30 min) to catch missed events.

### 10.2 Reconciliation

The client keeps a **local index** (SQLite) recording, for every synced item, the server node ID,
revision, content hash, and the local size/modified time/file ID at last sync. For each path it
compares three states — **local now**, **server now**, and **last synced (index)** — and decides:

| Local vs index | Server vs index | Action |
|---|---|---|
| unchanged | unchanged | nothing |
| changed | unchanged | upload / apply local change to server |
| unchanged | changed | download / apply server change locally |
| changed | changed, same content | update index only |
| changed | changed, different | **conflict** → FR-CONF-01/02/03 |

All server writes include the base revision; the server rejects stale writes with
`409 REVISION_CONFLICT`, which the client resolves as a conflict.

### 10.3 Safety rules

- Index updates happen only after the corresponding server or disk operation succeeds.
- Operations are journaled so a crash mid-operation is resumed or rolled back on restart.
- Mass-delete protection (FR-SYNC-08) and Recycle Bin for remote deletes (FR-SYNC-09).

---

## 11. File storage design

- Storage is accessed through a `StorageBackend` interface: write chunk, assemble/commit, open range for reading, delete, exists, health check.
- **Blob keys** are generated IDs (for example `ab/cd/<uuid>`), never user paths or names.
- **Development:** local filesystem backend under a configured directory.
- **Production options (OQ-2):**
  - **A. Filesystem backend on NAS (NFS/SMB) or a SAN volume.** Simplest; no new software. A SAN volume mounted on one host limits us to one app server unless a clustered filesystem is used; NAS allows multiple app servers.
  - **B. S3-compatible object store (e.g. MinIO) on SAN-backed disks.** Scales across app servers, native multipart uploads, built-in erasure coding/replication options. Adds a service to run, plus a dependency (`boto3`). Licensing of the chosen product must be reviewed (MinIO is AGPLv3; its distribution terms have changed over time, so check the current situation).
  - **Recommendation:** start on **A (NAS)** for the pilot because it needs no extra infrastructure, with the S3 backend implemented behind the same interface so we can switch to **B** if usage grows past one storage host. Decide before the production deployment.
- File content is streamed to and from storage; the app server never loads a whole file into memory.

---

## 12. Environments and deployment

| Environment | Database | Storage | Notes |
|---|---|---|---|
| Local dev (macOS) | `DROPSHARE_DEV` schema on the existing Oracle 19c instance | Local disk | Server via `uvicorn --reload`; client run from source using the development-grade macOS platform layer. Setup: `docs/oracle-setup.md`. |
| Windows test machine | Points at the dev server | — | Client run from source (`uv run`) to verify Windows behaviour; Windows-only tests; local installer builds. |
| Test (automated) | `DROPSHARE_TEST` schema on the same instance | Temp directory | Migrations applied before tests. Runs on macOS locally, Linux in CI (server), and Windows (client Windows-only tests). |
| Staging | Oracle 19c | Same type as prod | Pre-release verification, signed beta builds. |
| Production (Linux) | Oracle 19c (separate schema owner and app user) | NAS / SAN / S3 (OQ-2) | Behind reverse proxy, internet-facing. |

**Production topology (proposed, OQ-8):** internet → firewall → reverse proxy on Linux (TLS, rate limits)
→ FastAPI app server(s) running under a process manager → Oracle 19c and storage on the internal
network. Scheduled jobs (trash purge, upload cleanup, orphan GC, audit retention) run via a server
CLI invoked by the OS scheduler.

**Client build pipeline** (must run on Windows; PyInstaller and Inno Setup can't build Windows apps from macOS): freeze the app (PyInstaller, one-folder mode) → sign executables →
build installer (Inno Setup) → sign installer → publish release to the server (FR-UPD-05).

---

## 13. Phase 2 and later backlog

| ID | Item | Phase |
|---|---|---|
| BL-01 | Version history: list, preview, restore previous versions; retention rules | P2 |
| BL-02 | Selective sync (choose folders to sync per device) | P2 |
| BL-03 | Files on demand (Windows Cloud Files API placeholders) | P2 |
| BL-04 | Bandwidth limits (upload/download caps) | P2 |
| BL-05 | MFA (TOTP) | P2 (OQ-9) |
| BL-06 | Search by file name (then content) | P2 |
| BL-07 | File previews (images, PDF, Office thumbnails) | P2 |
| BL-08 | macOS client | P2 |
| BL-09 | Windows Explorer integration (status overlay icons, right-click "Share") | P2 |
| BL-10 | Malware scanning of uploads | P2 |
| BL-11 | Proxy configuration in the client | P2 |
| BL-12 | Metrics and dashboards | P2 |
| BL-13 | Editors can re-share; share expiry dates | P2 |
| BL-14 | LAN sync between clients on the same network | Later |
| BL-15 | Delta/block-level sync and deduplication | Later |
| BL-16 | SSO / LDAP / Active Directory sign-in | Later |
| BL-17 | Web client | Later |
| BL-18 | Mobile apps | Later |
| BL-19 | In-app administration: users, groups, quotas, storage, audit viewer (FR-ADMIN-02..06) and admin API | P2 |

---

## 14. Out of scope

- Public cloud hosting of files or metadata.
- Real-time collaborative editing inside documents.
- Linux client.
- Admin access to users' file content (no "view as user" feature).
- Public share links for people without an account (decided in OQ-1: links are internal only).
- Self-registration of accounts.
- Migration tooling from other file-sharing products (unless added later).

---

## 15. Open questions

These need an answer from the project owner. Items marked **Blocks** stop the named work from starting.

| ID | Question | Recommendation | Blocks |
|---|---|---|---|
| **OQ-2** | Production storage backend: filesystem on NAS/SAN, or S3-compatible (MinIO)? **Deferred to deployment.** | Start on NAS; keep S3 backend ready (§11). | Production deploy only |
| **OQ-6** | Code-signing certificate: which certificate (OV/EV or a cloud signing service), and who holds the key? **To be provided later.** | Cloud/HSM-backed signing so the key never sits on a developer machine. | Publishing signed releases (FR-UPD-02); development can continue with unsigned local builds |
| **OQ-7** | Oracle connection details and schema creation. See `docs/oracle-setup.md` for the checklist (port, PDB service name, an account that can create users, tablespace, character set, network encryption). | Create `DROPSHARE_DEV` and `DROPSHARE_TEST` with the provided script. | Server development |
| **OQ-8** | Hosting: Linux confirmed. Still open: whether Docker is allowed, public DNS name and TLS certificate (**to be provided later**), and who manages the reverse proxy/firewall. | Docker Compose; infra team owns proxy and certificates. | Staging/production |
| **OQ-11** | Which CI/source-control platform do you use (GitHub, GitLab, Azure DevOps, other)? We need a **Windows runner** for Windows-only tests and installer builds, plus a Linux runner for server tests. | Any platform with hosted Windows and Linux runners. | Automated Windows builds (manual builds on the Windows machine work until then) |
| **OQ-9** | MFA is planned for P2. Given the server is internet-facing, do you want to pull it into v1? | Pull into v1 if the pilot includes users outside the office. | — |
| **OQ-10** | Encryption at rest: is Oracle Advanced Security (TDE) licensed, or do we rely on encrypted storage volumes only? | Encrypted volumes for v1; TDE if already licensed. | — |

### Resolved

| ID | Resolution | Date |
|---|---|---|
| OQ-1 | Share links are **internal only** (FR-SHARE-07). Public links out of scope. | 2026-10-26 |
| OQ-3 | v1: accounts created directly against the database via the server CLI (FR-ADMIN-01). Admin UI is Phase 2 (BL-19); self-registration is out of scope. | 2026-10-26 |
| OQ-4 | Proposed limits confirmed (D18). | 2026-10-26 |
| OQ-5 | Product name: **StateLife DropShare** (D20). | 2026-10-26 |

## 16. Glossary

| Term | Meaning |
|---|---|
| **Sync folder / sync root** | The local folder the client keeps in two-way sync with the server. |
| **Namespace** | A separately-synced tree with its own change journal: a user's personal space or a shared folder. |
| **Mount** | A shared folder a recipient has accepted, appearing in their sync folder. |
| **Node** | A file or folder record in the database. |
| **Revision** | A counter on each node that increases on every change; used to detect stale writes. |
| **Cursor** | The position in a namespace's change journal the client has synced up to. |
| **Blob** | The stored bytes of one file version, addressed by an opaque key. |
| **Conflicted copy** | The extra file created when the same file changed in two places. |
| **Long-poll** | An HTTP request the server holds open until there is news or a timeout. |
