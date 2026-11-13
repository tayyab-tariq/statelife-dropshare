# StateLife DropShare — Requirements

> **Status:** Draft v0.3 · **Last updated:** 2026-11-05
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

| #   | Topic                                            | Decision                                                                                                                                                                                                                                 |
| --- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | Architecture                                     | Desktop client ↔ **FastAPI backend** ↔ Oracle + file storage. The client never connects to the database.                                                                                                                                 |
| D2  | Backend                                          | **Python 3.12, FastAPI, SQLAlchemy 2.0 (ORM), Alembic (migrations)**.                                                                                                                                                                    |
| D3  | Database                                         | **Oracle Database 19c** via the `python-oracledb` driver. All access through SQLAlchemy so the DB can change later. Local dev and test may use PostgreSQL for now, selected by `DB_DIALECT` (OQ-25). Production also runs PostgreSQL for now (OQ-50). |
| D4  | Desktop framework                                | **PySide6 (Qt 6)**. Chosen over PyQt6 because it is LGPL-licensed (PyQt6 is GPL or paid commercial); the APIs are nearly identical.                                                                                                      |
| D5  | Client OS                                        | **Windows 10/11 (64-bit)** in v1. macOS planned later; OS-specific code is isolated behind a platform layer.                                                                                                                             |
| D6  | File storage                                     | **Local disk in development.** Production backend is pluggable (filesystem on NAS/SAN, or S3-compatible such as MinIO). Final choice **deferred to deployment** (OQ-2).                                                                  |
| D7  | App modes                                        | **Both** a background two-way sync folder and an in-app file browser.                                                                                                                                                                    |
| D8  | Conflicts                                        | **Keep both** versions; one is saved as a "conflicted copy".                                                                                                                                                                             |
| D9  | Sharing                                          | **Yes**: share with users and groups, with permission levels. Share links are **internal only** (OQ-1).                                                                                                                                  |
| D10 | Trash                                            | **Yes** in v1. **Version history** later, but designed for now (G6).                                                                                                                                                                     |
| D11 | Selective sync, offline extras, bandwidth limits | **Phase 2.**                                                                                                                                                                                                                             |
| D12 | Accounts                                         | **Stored in Oracle** (local accounts). 50–100 users initially; must scale beyond. In v1, accounts are managed with the server CLI and, by admins, from the Administration section of the desktop app (FR-ADMIN-02/06, OQ-60). Groups, storage overview and the audit viewer in the app stay **Phase 2**.                                |
| D13 | Network                                          | Clients connect **over the public internet**.                                                                                                                                                                                            |
| D14 | Audit logs                                       | **Required** in v1.                                                                                                                                                                                                                      |
| D15 | Distribution                                     | **Installer, code signing, and auto-update** all required in v1.                                                                                                                                                                         |
| D16 | Tooling                                          | **uv** (dependencies/workspace), **ruff** (lint/format), **mypy** (types), **pytest**. Monorepo with `client/`, `server/`, `shared/`.                                                                                                    |
| D17 | Planning                                         | Requirements split into **v1** and **later phases**.                                                                                                                                                                                     |
| D18 | Limits                                           | 2,000 MiB max file (10 GB until 2026-11-05, then 3 GB until 2026-11-07: ClamAV can't scan more than 2 GiB of a file, OQ-54, OQ-58 (4)), 50 GB default quota, 5 TB initial storage, shared content counts against the owner, 30-day trash, 1-year audit retention (OQ-4).                                                                                         |
| D19 | Platforms                                        | Server: **Linux in production**; developed on **macOS**. Desktop client: developed on **macOS**, tested and built on a **Windows** machine. All server code and the client's non-OS-specific code must run on macOS, Windows, and Linux. |
| D20 | Product name                                     | **StateLife DropShare**.                                                                                                                                                                                                                 |

---

## 3. Users and roles

| Role                | Who                              | Can do                                                                                                                                                                                                                                                         |
| ------------------- | -------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **User**            | Any employee with an account     | Sync and manage their own files, share folders and files they own, use trash, manage their devices.                                                                                                                                                            |
| **Admin**           | IT / system administrators       | In v1: manage users (create, disable/enable, reset password, change quota and role) from the **Administration section of the desktop app** (FR-ADMIN-02/06) or the server CLI; manage groups, view storage usage, and export audit logs **via the server CLI** (requires server access). In Phase 2: groups, storage, and audit from the app too. Admins do **not** get access to other users' file content. |
| **Share recipient** | A User who has been given access | Access shared items according to their role (Viewer or Editor).                                                                                                                                                                                                |

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

| Phase        | Theme                                   | Contents                                                                                                                                     |
| ------------ | --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| **v1 (MVP)** | Reliable sync and sharing for the pilot | Everything tagged `v1` in sections 6–7.                                                                                                      |
| **Phase 2**  | Power features and hardening            | Version history, selective sync, files-on-demand, bandwidth limits, MFA, file search, previews, macOS client, Explorer integration. See §13. |
| **Later**    | Scale and integrations                  | LAN sync, delta/block sync, SSO/LDAP, web client, mobile. See §13.                                                                           |

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

IT staff with access to the server run a CLI that writes directly to the database through the same
services the API uses, so passwords are hashed correctly and every action is validated and audited.
(Plain SQL inserts can't be used for accounts because passwords must be stored as Argon2id hashes.)
In v1, admins can also manage user accounts from an Administration section in the desktop app
(FR-ADMIN-02/06, roadmap M16, OQ-60), which calls the same services through an admin API. Groups,
storage overview, and the audit viewer in the app are Phase 2.

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

**FR-ADMIN-02 — Manage users from the app** `v1` (OQ-60)
Admins manage user accounts from the Administration section (FR-ADMIN-06) through an admin API that calls the same services as the CLI.
Acceptance:

- Only users whose role is `admin` can use `/admin/*`. The role is read from the database on every request, not trusted from the token, so a demoted or disabled admin loses access at their next request. Anyone else gets `403 FORBIDDEN`, audited as access denied.
- The user list shows every account, enabled and disabled: username, email, display name, role, status, quota, and storage used. The app filters it by the start of username, display name, or email (case-insensitive). No paging.
- **Create user:** username, email, display name, role (user/admin), quota (default 50 GB). Same validation and uniqueness rules as FR-ADMIN-01; a conflict says which field is taken.
- **Reset password:** sets a new temporary password, signs the user out everywhere, and clears any lockout (as the CLI does).
- Create and reset generate a one-time temporary password the user must change at first sign-in (FR-AUTH-05). It is shown to the admin **once**, in the response to that action; it can't be retrieved later and is never logged or audited. It is also emailed to the user (FR-ADMIN-07). If the email fails or isn't configured, the action still stands and the admin is told the email wasn't sent, so they pass the password on themselves.
- **Disable** signs the user out everywhere immediately; files and shares are kept. **Enable** lets them sign in again.
- **Change quota** follows OQ-46: never below what the user already stores, trash included; the refusal says how much they store.
- **Change role** between user and admin.
- Guard rails: an admin can't change their own status or role (`409 SELF_CHANGE_NOT_ALLOWED`). Since only an enabled admin can disable or demote another, never themselves, an enabled admin always remains. Before disabling or demoting an admin, the caller's own role is re-checked under a lock on every admin row, so when two admins demote each other at once the second is refused (`403`).
- Every action is audited with the admin as actor (device, source IP), with the same actions as the CLI (`user.listed`, `user.created`, `user.disabled`, `user.enabled`, `user.quota_changed`, `auth.password_reset`) plus `user.role_changed` (old and new role). Whether the email was sent is recorded as `user.password_emailed` with outcome success or failure. Refused actions are audited as failure or denied.
- Admin write actions are rate-limited per admin (NFR-SEC-13).
- The CLI (FR-ADMIN-01) is unchanged and still audits as `cli`.

**FR-ADMIN-03 — Manage groups from the app** `P2`
**FR-ADMIN-04 — Quotas and storage overview in the app** `P2`
**FR-ADMIN-05 — Audit log viewer in the app (filter, paginate, export)** `P2`
**FR-ADMIN-06 — Administration section in the desktop app, visible only to admins** `v1` (users only, OQ-60)
Acceptance:

- The section appears in the main window only when `GET /me` reports role `admin`. Hiding it is a convenience; the server decides (FR-ADMIN-02).
- It holds the user list and the create, disable/enable, reset password, change quota, and change role actions. The list has loading, error (with retry), empty, and populated states (NFR-UX-01); network work runs off the UI thread; everything is keyboard-operable with accessible names (NFR-UX-02).
- Disable, reset password, and change role ask for confirmation, naming the user.
- After create or reset, a dialog shows the temporary password once with a Copy button and says whether the email was sent. The client never logs or stores it and drops it when the dialog closes.
- An admin who loses the role while the section is open sees "You're no longer an administrator" on their next action, and the section is hidden.
- Groups, storage overview, and the audit viewer aren't in the section in v1 (FR-ADMIN-03/04/05 stay `P2`).

**FR-ADMIN-07 — Email the temporary password** `v1` (OQ-60, OQ-61)
Acceptance:

- The server sends through the organization's email API: `POST` to `EMAIL_API_URL` with JSON `{ "subject", "reciever", "text" }` (field spelled as the API spells it) and header `X-API-Key: <EMAIL_API_KEY>`. `text` is HTML.
- The email holds the username and the temporary password and says the password must be changed at first sign-in. Every inserted value is HTML-escaped; no other account data.
- The email is sent only after the account change is committed; a failed email never undoes it.
- Sending fails on a network error, a timeout, a non-2xx status, a response that isn't JSON or has no `http_status`, or an `http_status` containing "fail" or "error" (case-insensitive).
- `EMAIL_API_URL` and `EMAIL_API_KEY` are optional: with neither set the server starts and reports email as not configured; with only one set it refuses to start (NFR-OPS-03). The key never appears in logs, errors, or audit events; the password never appears in logs.

### 6.3 Files and folders in the file browser (FILE)

**FR-FILE-01 — Browse** `v1`
Acceptance:

- The browser shows the user's own files, folders shared with them, and a "Shared with me" view.
- Each item shows name, size, modified date, modified by, and sharing state.
- Large folders load page by page without freezing the UI.

**FR-FILE-02 — Upload** `v1`
Acceptance:

- Upload via an Upload button (files, or a folder) and by dragging files/folders from Explorer onto the browser or onto a folder in it.
- The selected items are **copied into the matching folder inside the sync folder**; the sync engine uploads them (OQ-23). The copy runs off the UI thread, shows progress, and can be cancelled.
- If a name already exists: ask **Keep both** (default; adds ` (1)`, ` (2)` …), **Replace**, or **Skip**, with "Apply to all" for multiple items.
- Before copying, check free disk space and remaining quota. If either is insufficient, nothing is copied, and the message says how much space is needed.

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
- Items that cannot be synced (invalid name for Windows, path too long, over quota, file type not allowed or malware detected per FR-SAFE-01/03) are listed in a "Can't sync" view with the reason and a suggested fix.

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

**FR-SHARE-08 — Find people and groups** `v1`
Acceptance:

- `GET /users/search?q=` returns active users matching the start of their display name, username, or email (case-insensitive), with only ID, display name, and email. Requires at least 2 characters and returns at most 20 results. Disabled users are never returned, and neither is the signed-in user (you can't share with yourself; OQ-53).
- `GET /groups/search?q=` does the same for groups (ID, name, member count).
- Both endpoints require authentication and are rate-limited like other API calls.

### 6.9 Audit log (AUDIT)

**FR-AUDIT-01 — Record security-relevant events** `v1`
Acceptance — at minimum these are recorded:

- Authentication: sign-in success/failure, sign-out, token refresh reuse detected, lockout/unlock, password change/reset.
- Files: upload complete, download, create folder, rename, move, delete to trash, restore, permanent delete, upload or rename refused for file type or malware (FR-SAFE-01/02).
- Sharing: share created/changed/removed, shared folder accepted/left.
- Admin: user/group created/changed/disabled/enabled, role changed, password reset by an admin, quota changed, audit export.
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

- The production server URL is built into the installer, and installer builds sign in only to it (OQ-51). Running from source (development) may sign in to another server.

**FR-CLIENT-06 — Diagnostics** `v1`
Acceptance:

- Logs are written to `%LOCALAPPDATA%\StateLife\DropShare\logs` with rotation. "Export diagnostics" creates a zip of logs and config without tokens or file content.

**FR-CLIENT-07 — Single instance** `v1`
Acceptance:

- Starting the app while it is already running brings the existing window to the front.

**FR-CLIENT-08 — Transfers view and progress** `v1`
Acceptance:

- A Transfers view lists active uploads and downloads, each with name, direction, a progress bar, bytes done/total, and percent. It also shows queued and waiting items, and the 50 most recent completed or failed transfers.
- An overall summary is shown at the top of the view and in the tray tooltip and menu (for example "Syncing 12 items · 45%").
- Progress updates reach the UI at most about 4 times per second per item, and the view stays responsive with 1,000 queued items.
- Failed transfers show the reason and a Retry action.

### 6.11 Installation and updates (UPD)

**FR-UPD-01 — Windows installer** `v1`
Acceptance:

- A signed installer installs per-user (no admin rights required) into `%LOCALAPPDATA%\Programs\StateLife DropShare`, creates Start-menu shortcuts, and registers the `statelife-dropshare://` link scheme (FR-SHARE-07). The start-at-sign-in entry is registered by the app, so the user's choice is kept (OQ-44); the installer only removes it on uninstall (OQ-49).
- Uninstall removes the app but keeps the sync folder and asks whether to remove local app data; removing it also deletes the saved sign-in from Credential Manager (OQ-49).

**FR-UPD-02 — Code signing** `v1`
Acceptance:

- The application executable and installer are Authenticode-signed with the organization's certificate (OQ-6). Builds without a valid signature are not published: the release CLI checks the installer's signature and publisher and refuses anything else (OQ-49).

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
- Admins can list releases and withdraw one, so clients are no longer offered it.
- Each user is on the stable or beta channel, set via the CLI (default stable). Beta users are offered the newer of the latest beta and the latest stable release (OQ-49).

### 6.12 Content safety (SAFE)

The server is internet-facing, and a shared folder passes every file in it to everyone with access.
So the server checks file content before anyone else can see it. The server's check is the one that
counts; the client checks too, but only so users find out early and files aren't uploaded just to be
refused. Planned in roadmap milestone M15; assumptions confirmed in OQ-57.

**FR-SAFE-01 — Malware scan before a file becomes visible** `v1` (NFR-SEC-11; OQ-54, OQ-55)
Every uploaded file version is scanned for malware on the server before it becomes visible to anyone: the uploader's other devices, share recipients, or the uploader in the file browser.
Acceptance:

- A version becomes visible only after it scans clean (extends NFR-REL-03). An infected version is never visible or downloadable; its staged data is deleted, and the previous version (if any) stays current.
- Every file is scanned in full: the 2,000 MiB file limit (NFR-SCALE-01) is within the scanner's limits (ClamAV scans at most 2 GiB of a file), and the scanner is configured so no file is skipped for size.
- The uploading client receives `MALWARE_DETECTED` with the detection name. The item appears in "Can't sync" (FR-SYNC-07) with a plain-language reason, and the user is notified. DropShare doesn't change, move, or delete the local file (OQ-57).
- Scanning applies to every upload path: the sync engine, in-app upload (FR-FILE-02), and new versions of individually shared files (FR-SHARE-03, OQ-32), in personal and shared folders alike.
- Detections are audited as `file.malware_detected` with actor, device, source IP, file name, folder, and detection name, never file content (FR-AUDIT-02).
- When the scanner is down, uploads wait (fail closed, OQ-55): nothing becomes visible unscanned, the client shows the item as waiting for the scan and checks again with backoff, and the upload completes once the scanner is back. Nothing is lost, and a waiting upload survives an app restart (FR-XFER-01).
- `/health/ready` reports whether the scanner is reachable and how old its signatures are (NFR-OPS-02).
- Scanning streams the content: the app server never loads a whole file into memory (§11). A small file still reaches other devices within NFR-PERF-02.

**FR-SAFE-02 — Banned file types on the server** `v1` (OQ-56, OQ-57)
The server refuses banned file types, so they are never stored and never reach anyone through a share.
Acceptance:

- The banned extensions are one list for everyone in server configuration, validated at startup (NFR-OPS-03). Changing it doesn't need a client release.
- The list is enforced on every write that creates or names a file, in personal and shared folders, for owners and editors alike: upload start (before any content is sent), upload completion, rename, and move. A refused request returns `FILE_TYPE_NOT_ALLOWED`, naming the extension.
- Extensions are matched case-insensitively on the last extension of the normalized name (NFR-COMPAT-02), so `invoice.pdf.EXE` counts as `.exe`.
- Before a version becomes visible, the server checks its first bytes. Executable content (Windows PE, ELF, Mach-O) is refused as `FILE_TYPE_NOT_ALLOWED` whatever the extension, so a renamed `.exe` is still caught.
- Banned files never reach the server, so a shared folder holds only allowed types and sharing needs no separate check.
- Refusals are audited as `file.type_blocked` with actor, device, source IP, file name, and extension.
- Files already stored when a type is banned are handled as set by OQ-57.

**FR-SAFE-03 — Banned files on the client** `v1` (OQ-56, OQ-57)
Acceptance:

- The client gets the current banned list from the server and checks names before uploading, so a banned file is never uploaded just to be refused. The server's answer still decides (FR-SAFE-02).
- Banned files in the sync folder stay where they are, untouched. DropShare never deletes, moves, renames, or quarantines them (OQ-57). They are listed in "Can't sync" with the reason (for example "This file type isn't allowed: .exe") and a suggested fix, and the "can't sync" notification is shown (FR-CLIENT-02, OQ-43). DropShare can't stop other programs saving files into the sync folder; it only declines to sync them.
- A refused upload (type or malware) is retried only after the local file changes, so there is no upload loop. The server keeps its last good version, and the client doesn't overwrite the local file with it.
- If a local rename gives a synced file a banned extension, the server item is never deleted. It keeps its old name, the local file is listed in "Can't sync", and renaming it back resumes syncing (NFR-REL-01).
- In-app upload (FR-FILE-02) checks the selection before copying. Banned files aren't copied into the sync folder, the rest are, and the summary lists what was skipped and why.
- The same rules apply to editors in shared folders. Viewers can't upload (FR-SHARE-05).

---

## 7. Non-functional requirements

### 7.1 Security (SEC)

| ID         | Requirement                                                                                                                                                                     | Phase     |
| ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| NFR-SEC-01 | All traffic uses HTTPS with TLS 1.2+ (1.3 preferred) and HSTS. No plain-HTTP endpoints exposed.                                                                                 | v1        |
| NFR-SEC-02 | Every endpoint except `/health` and sign-in requires a valid access token. Authorization (ownership and share roles) is checked in the server's service layer on every request. | v1        |
| NFR-SEC-03 | Passwords hashed with **Argon2id**. Tokens signed with a server secret held outside the repo; refresh tokens stored only as hashes.                                             | v1        |
| NFR-SEC-04 | Password policy: minimum 12 characters, maximum 128, rejected if on a bundled common-password list. No forced composition rules (per NIST SP 800-63B).                          | v1        |
| NFR-SEC-05 | Inputs validated by Pydantic models; unknown fields rejected. File names validated server-side. Path traversal impossible because storage keys are IDs, not paths.              | v1        |
| NFR-SEC-06 | Rate limiting at the reverse proxy (global and stricter on auth endpoints) plus account lockout (FR-AUTH-06) and an in-app limit on admin actions (NFR-SEC-13).                                                                   | v1        |
| NFR-SEC-07 | Errors never expose stack traces, SQL, internal IDs of other users, or storage paths.                                                                                           | v1        |
| NFR-SEC-08 | Encryption at rest via encrypted storage volumes (NAS/SAN or object store). Oracle TDE only if licensed (OQ-10).                                                                | v1        |
| NFR-SEC-09 | Secrets (DB password, token secret, storage keys, email API key) come from environment/secret store, validated at startup. Never committed.                                                    | v1        |
| NFR-SEC-10 | Dependencies pinned via `uv.lock`; vulnerability scan (`pip-audit` or equivalent, needs approval) before each release.                                                          | v1        |
| NFR-SEC-11 | Antivirus / malware scanning of uploads (FR-SAFE-01; moved from P2, OQ-54).                                                                                                     | v1        |
| NFR-SEC-13 | Admin write actions (FR-ADMIN-02) are limited per admin inside the app (default 30 per minute, `ADMIN_RATE_LIMIT_PER_MINUTE`); over the limit answers `429 RATE_LIMITED` with `Retry-After`. Kept in memory per server process (standard library): it resets on restart and isn't shared between workers (OQ-60). | v1 |
| NFR-SEC-12 | MFA (TOTP).                                                                                                                                                                     | P2 (OQ-9) |

### 7.2 Performance and scale (PERF, SCALE)

Values confirmed (OQ-4). All limits are configuration, not code constants.

| ID           | Requirement                                                                                                                                                                           | Phase       |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| NFR-PERF-01  | Metadata API calls: p95 < 300 ms at 100 concurrent active users.                                                                                                                      | v1          |
| NFR-PERF-02  | A small file saved on one device is visible on another online device within 10 s (p95).                                                                                               | v1          |
| NFR-PERF-03  | The UI thread is never blocked by network, disk, or hashing work; the window stays responsive with 200,000 files in the sync folder.                                                  | v1          |
| NFR-PERF-04  | Large transfers use at least 80% of available bandwidth on a stable connection.                                                                                                       | v1          |
| NFR-PERF-05  | Initial sync of 50,000 files / 50 GB completes without user intervention and survives restarts mid-way.                                                                               | v1          |
| NFR-SCALE-01 | Maximum file size: **2,000 MiB** (configurable, but never above 2 GiB; was 10 GB, then 3 GB, lowered so every file fits the malware scanner, OQ-54, OQ-58 (4)). No limit on folder size.                                              | v1          |
| NFR-SCALE-02 | Up to 200,000 files per user (own + mounted shared folders).                                                                                                                          | v1          |
| NFR-SCALE-03 | Default quota: **50 GB per user**, adjustable per user by admins.                                                                                                                     | v1          |
| NFR-SCALE-04 | Initial storage provisioning: **5 TB usable**, expandable, alert at 75% full. (100 users × 50 GB is the worst case; typical use is 30–50% of quota, and trash/versions add overhead.) | v1          |
| NFR-SCALE-05 | Design supports growth to 1,000+ users by adding app servers, without data model changes.                                                                                             | v1 (design) |

### 7.3 Reliability and data integrity (REL)

| ID         | Requirement                                                                                                                                                                       | Phase |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| NFR-REL-01 | No data loss: sync engine has automated tests covering every local/remote change combination (create/modify/delete/move × both sides), including crash and restart mid-operation. | v1    |
| NFR-REL-02 | Every stored blob is verified by SHA-256 on upload; downloads are verified before being moved into place.                                                                         | v1    |
| NFR-REL-03 | Server operations that change metadata are transactional; a new file version becomes visible only after its content is fully stored.                                              | v1    |
| NFR-REL-04 | Idempotency: retrying any client request (including after timeouts) never duplicates files or corrupts state.                                                                     | v1    |
| NFR-REL-05 | Scheduled jobs clean up expired uploads, orphaned blobs (no DB row after 24 h), and purge trash.                                                                                  | v1    |
| NFR-REL-06 | Backups: Oracle (RMAN or DBA-standard) and storage snapshots on a schedule agreed with infra; documented restore procedure tested before go-live.                                 | v1    |

### 7.4 Compatibility and portability (COMPAT)

| ID            | Requirement                                                                                                                                                                                                                                                                                                                           | Phase       |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| NFR-COMPAT-01 | Database access only via SQLAlchemy ORM; no Oracle-specific SQL outside the migration layer. Schema changes only via Alembic.                                                                                                                                                                                                         | v1          |
| NFR-COMPAT-02 | Server enforces cross-platform-safe names: no `< > : " / \ \| ? *`, no control characters, no trailing dot or space, no Windows reserved names (`CON`, `PRN`, `AUX`, `NUL`, `COM1–9`, `LPT1–9`), max 255 characters. Names unique per folder case-insensitively (Unicode NFC + casefold).                                             | v1          |
| NFR-COMPAT-03 | Client supports paths longer than 260 characters (long-path aware), up to the server limit.                                                                                                                                                                                                                                           | v1          |
| NFR-COMPAT-04 | OS-specific client code (credential store, autostart, recycle bin, file IDs, notifications) lives behind a platform interface so a macOS implementation can be added. A **development-grade macOS implementation** exists so the client runs on developers' Macs; it is not a supported or shipped macOS client (that remains BL-08). | v1 (design) |
| NFR-COMPAT-05 | Storage access only via a `StorageBackend` interface; switching local disk ↔ NAS ↔ S3 is configuration.                                                                                                                                                                                                                               | v1          |
| NFR-COMPAT-06 | The server runs on **macOS (development)** and **Linux (production)**, and also on Windows: `pathlib` for all paths, no OS-specific calls outside clearly isolated modules, no reliance on case-sensitive file systems. CI runs the server test suite on Linux.                                                                       | v1          |
| NFR-COMPAT-07 | Windows-specific client behaviour (Credential Manager, Recycle Bin, NTFS file IDs, locked files, long paths, reserved names, link scheme, installer, auto-update) is verified on a real Windows machine before a change counts as done; automated Windows-only tests run there and in CI on a Windows runner.                         | v1          |

### 7.5 Usability and accessibility (UX)

| ID        | Requirement                                                                                                                                                              | Phase |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- |
| NFR-UX-01 | Every data-driven view shows loading, error (with retry), empty (with next action), and populated states.                                                                | v1    |
| NFR-UX-02 | Fully keyboard-operable; visible focus; screen-reader names on all controls; follows Windows light/dark and high-contrast settings; correct at 100–200% display scaling. | v1    |
| NFR-UX-03 | Error messages say what happened and what to do, in plain language.                                                                                                      | v1    |

### 7.6 Observability and operations (OPS)

| ID         | Requirement                                                                                                  | Phase |
| ---------- | ------------------------------------------------------------------------------------------------------------ | ----- |
| NFR-OPS-01 | Structured (JSON) server logs with request ID, user ID, and timing; no secrets or file content.              | v1    |
| NFR-OPS-02 | `/health` (liveness) and `/health/ready` (DB + storage reachable) endpoints.                                 | v1    |
| NFR-OPS-03 | Configuration via environment variables validated at startup; the server refuses to start on invalid config. | v1    |
| NFR-OPS-04 | Metrics endpoint (Prometheus format) and dashboards.                                                         | P2    |

### 7.7 Maintainability (MAINT)

| ID           | Requirement                                                                                                                     | Phase |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------- | ----- |
| NFR-MAINT-01 | `ruff` and `mypy --strict` pass; tests run in CI on every change.                                                               | v1    |
| NFR-MAINT-02 | Server layering: routers → services → repositories; only repositories use SQLAlchemy; only storage backends touch file content. | v1    |
| NFR-MAINT-03 | Client layering: UI → controllers → services/sync engine; UI code never makes HTTP calls or touches the local index directly.   | v1    |

---

## 8. Data model (conceptual)

This is the conceptual model that implementation must satisfy. Exact columns are decided in code
and Alembic migrations. All tables use UUID primary keys (except append-only logs, which use an
increasing integer) and UTC timestamps.

| Entity                      | Purpose                                                                                     | Key fields                                                                                                                                                        |
| --------------------------- | ------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **User**                    | Account                                                                                     | username, email (both unique, case-insensitive), display name, password hash, role, status, quota bytes, failed-login count, locked-until, must-change-password   |
| **Group** / **GroupMember** | Groups for sharing                                                                          | name; user ↔ group                                                                                                                                                |
| **Device**                  | A signed-in client install                                                                  | user, name, OS, client version, last seen, revoked at                                                                                                             |
| **RefreshToken**            | Session tokens                                                                              | user, device, token hash, family ID, expires, revoked, replaced by                                                                                                |
| **Namespace**               | A sync root with its own change history: each user's personal space, and each shared folder | type (personal/shared), owner                                                                                                                                     |
| **NamespaceMember**         | Who can access a shared namespace                                                           | namespace, user or group, role (viewer/editor)                                                                                                                    |
| **Mount**                   | A recipient's accepted shared folder in their tree                                          | user, namespace, mount name, accepted at                                                                                                                          |
| **Node**                    | A file or folder                                                                            | namespace, parent, name, normalized name, kind, size, content hash, current version, revision (for optimistic concurrency), created/modified by/at, trashed at/by |
| **FileVersion**             | One stored version of a file's content                                                      | node, blob key, size, SHA-256, created by/at, scanned at and scanner signature version (FR-SAFE-01)                                                               |
| **FileShare**               | A single file shared with users/groups (FR-SHARE-03)                                        | node, user or group, role                                                                                                                                         |
| **UploadSession**           | In-progress resumable upload                                                                | user, target (parent + name, or node + base revision), size, chunk size, received chunks, expected hash, status, expires                                          |
| **Change**                  | Append-only journal of node changes per namespace; its increasing ID is the sync cursor     | namespace, node, change type, snapshot of node fields, actor, time                                                                                                |
| **AuditEvent**              | Append-only audit log                                                                       | see FR-AUDIT-02                                                                                                                                                   |
| **ClientRelease**           | Auto-update releases                                                                        | version, platform, channel, min supported version, installer blob, SHA-256, notes, published at                                                                   |

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

| Area               | Endpoints (v1)                                                                                                                                                                              |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Health             | `GET /health`, `GET /health/ready` (outside `/api/v1`)                                                                                                                                      |
| Auth               | `POST /auth/login`, `POST /auth/refresh`, `POST /auth/logout`                                                                                                                               |
| Me                 | `GET /me`, `POST /me/password`, `GET /me/devices`, `DELETE /me/devices/{id}`                                                                                                                |
| Namespaces & nodes | `GET /namespaces`, `GET /nodes/{id}`, `GET /nodes/{id}/children`, `POST /nodes` (create folder), `PATCH /nodes/{id}` (rename/move, requires `if_revision`), `DELETE /nodes/{id}` (to trash) |
| Uploads            | `POST /uploads`, `GET /uploads/{id}`, `PUT /uploads/{id}/chunks/{index}`, `POST /uploads/{id}/complete` (200 with the file once scanned clean; 202 with the upload, status `scanning`, while the malware scan is still to come, with `Retry-After`; FR-SAFE-01), `DELETE /uploads/{id}` (409 `UPLOAD_SCANNING` while it is being scanned)                                                             |
| Download           | `GET /nodes/{id}/content` (supports `Range`)                                                                                                                                                |
| Sync               | `GET /namespaces/{id}/changes?cursor=&limit=`, `POST /changes/wait` (long-poll over several namespace cursors)                                                                              |
| Trash              | `GET /trash`, `POST /trash/{node_id}/restore`, `DELETE /trash/{node_id}`, `DELETE /trash`                                                                                                   |
| Sharing            | `GET /nodes/{id}/members`, `POST /nodes/{id}/members`, `PATCH /members/{id}`, `DELETE /members/{id}`, `GET /shared-with-me`, `POST /mounts`, `DELETE /mounts/{namespace_id}`, `GET /users/search?q=`, `GET /groups/search?q=` |
| File policy        | `GET /file-policy`: the banned extensions and the maximum file size, for checks before upload (FR-SAFE-03) |
| Client updates     | `GET /client/releases/latest?platform=` (channel from the user, OQ-49), `GET /client/releases/{id}/installer`                                                                                                     |
| Admin              | v1 (FR-ADMIN-02, admins only): `GET /admin/users`, `POST /admin/users`, `PATCH /admin/users/{id}` (status, role, quota), `POST /admin/users/{id}/password-reset`. Phase 2: `/admin/groups`, `/admin/storage`, `/admin/audit-events`, `/admin/releases`.                                        |

Error codes are stable SCREAMING_SNAKE_CASE strings defined in `shared/`, for example
`VALIDATION_ERROR`, `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `NAME_CONFLICT`,
`REVISION_CONFLICT`, `QUOTA_EXCEEDED`, `FILE_TOO_LARGE`, `CHUNK_HASH_MISMATCH`, `FILE_HASH_MISMATCH`,
`ACCOUNT_LOCKED`, `UPGRADE_REQUIRED`, `RATE_LIMITED`, `SELF_CHANGE_NOT_ALLOWED` (`409`, FR-ADMIN-02), `FILE_TYPE_NOT_ALLOWED`, `MALWARE_DETECTED` (FR-SAFE-01/02). `FILE_TYPE_NOT_ALLOWED` is `422`, with the reason, the extension, and for a folder up to 10 of the files it holds. `MALWARE_DETECTED` is `422`, with the scanner's detection name. `UPLOAD_SCANNING` is `409`: the upload is being scanned right now and may still complete.

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

| Local vs index | Server vs index       | Action                                 |
| -------------- | --------------------- | -------------------------------------- |
| unchanged      | unchanged             | nothing                                |
| changed        | unchanged             | upload / apply local change to server  |
| unchanged      | changed               | download / apply server change locally |
| changed        | changed, same content | update index only                      |
| changed        | changed, different    | **conflict** → FR-CONF-01/02/03        |

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

| Environment          | Database                                                   | Storage               | Notes                                                                                                                                  |
| -------------------- | ---------------------------------------------------------- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| Local dev (macOS)    | `DROPSHARE_DEV` schema on the existing Oracle 19c instance, or `dropshare_dev` on a local PostgreSQL (OQ-25) | Local disk            | Server via `uvicorn --reload`; client run from source using the development-grade macOS platform layer. Setup: `docs/oracle-setup.md` or `docs/postgres-setup.md`. |
| Windows test machine | Points at the dev server                                   | —                     | Client run from source (`uv run`) to verify Windows behaviour; Windows-only tests; local installer builds.                             |
| Test (automated)     | `DROPSHARE_TEST` schema on the same instance, or `dropshare_test` on the same PostgreSQL | Temp directory        | Migrations applied before tests. Runs on macOS locally, Linux in CI (server), and Windows (client Windows-only tests).                 |
| Staging              | Oracle 19c                                                 | Same type as prod     | Pre-release verification, signed beta builds.                                                                                          |
| Production (Linux)   | Oracle 19c (separate schema owner and app user); for now PostgreSQL in Docker on Windows (OQ-50) | NAS / SAN / S3 (OQ-2); for now a folder on the server (OQ-50) | Behind reverse proxy, internet-facing.                                                                                                 |

**Production topology (proposed, OQ-8):** internet → firewall → reverse proxy on Linux (TLS, rate limits)
→ FastAPI app server(s) running under a process manager → Oracle 19c and storage on the internal
network. Scheduled jobs (trash purge, upload cleanup, orphan GC, audit retention) run via a server
CLI invoked by the OS scheduler.

**Client build pipeline** (must run on Windows; PyInstaller and Inno Setup can't build Windows apps from macOS): freeze the app (PyInstaller, one-folder mode) → sign executables →
build installer (Inno Setup) → sign installer → publish release to the server (FR-UPD-05).

---

## 13. Phase 2 and later backlog

| ID    | Item                                                                                                | Phase     |
| ----- | --------------------------------------------------------------------------------------------------- | --------- |
| BL-01 | Version history: list, preview, restore previous versions; retention rules                          | P2        |
| BL-02 | Selective sync (choose folders to sync per device)                                                  | P2        |
| BL-03 | Files on demand (Windows Cloud Files API placeholders)                                              | P2        |
| BL-04 | Bandwidth limits (upload/download caps)                                                             | P2        |
| BL-05 | MFA (TOTP)                                                                                          | P2 (OQ-9) |
| BL-06 | Search by file name (then content)                                                                  | P2        |
| BL-07 | File previews (images, PDF, Office thumbnails)                                                      | P2        |
| BL-08 | macOS client                                                                                        | P2        |
| BL-09 | Windows Explorer integration (status overlay icons, right-click "Share")                            | P2        |
| BL-10 | Malware scanning of uploads: moved to v1 as FR-SAFE-01 (OQ-54, roadmap M15)                         | v1        |
| BL-11 | Proxy configuration in the client                                                                   | P2        |
| BL-12 | Metrics and dashboards                                                                              | P2        |
| BL-13 | Editors can re-share; share expiry dates                                                            | P2        |
| BL-14 | LAN sync between clients on the same network                                                        | Later     |
| BL-15 | Delta/block-level sync and deduplication                                                            | Later     |
| BL-16 | SSO / LDAP / Active Directory sign-in                                                               | Later     |
| BL-17 | Web client                                                                                          | Later     |
| BL-18 | Mobile apps                                                                                         | Later     |
| BL-19 | In-app administration of groups, storage overview, and audit viewer (FR-ADMIN-03..05) and their admin API; user management moved to v1 (FR-ADMIN-02/06/07, OQ-60) | P2        |

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

| ID        | Question                                                                                                                                                                                               | Recommendation                                                         | Blocks                                                                                      |
| --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| **OQ-2**  | Production storage backend: filesystem on NAS/SAN, or S3-compatible (MinIO)? **Deferred to deployment.**                                                                                               | Start on NAS; keep S3 backend ready (§11).                             | Production deploy only                                                                      |
| **OQ-6**  | Code-signing certificate: which certificate (OV/EV or a cloud signing service), and who holds the key? **To be provided later.**                                                                       | Cloud/HSM-backed signing so the key never sits on a developer machine. | Publishing signed releases (FR-UPD-02); development can continue with unsigned local builds |
| **OQ-7**  | Oracle connection details and schema creation. See `docs/oracle-setup.md` for the checklist (port, PDB service name, an account that can create users, tablespace, character set, network encryption). | Create `DROPSHARE_DEV` and `DROPSHARE_TEST` with the provided script.  | Server development                                                                          |
| **OQ-8**  | Hosting: Docker is allowed and the proxy is an existing nginx on another machine, run by its owners (OQ-50). Still open: public DNS name and TLS certificate (**to be provided later**), and the proxy's internal IP (firewall rule and `TRUSTED_PROXIES`). | Docker Compose; infra team owns proxy and certificates.                | Staging/production                                                                          |
| **OQ-11** | Which CI/source-control platform do you use (GitHub, GitLab, Azure DevOps, other)? We need a **Windows runner** for Windows-only tests and installer builds, plus a Linux runner for server tests.     | Any platform with hosted Windows and Linux runners.                    | Automated Windows builds (manual builds on the Windows machine work until then)             |
| **OQ-9**  | MFA is planned for P2. Given the server is internet-facing, do you want to pull it into v1?                                                                                                            | Pull into v1 if the pilot includes users outside the office.           | —                                                                                           |
| **OQ-61** | Assumptions in M16, proceeding on request with these until confirmed: (1) the email is sent once with a 10 s timeout, no retry; (2) the admin rate limit is 30 write actions per minute per admin, and reading the list isn't counted; (3) admins can disable, reset, re-quota, and demote other admins, subject to the last-admin rule; (4) a temporary password doesn't expire (same as the CLI); (5) quota is entered in whole GB, default 50, at least 1. | As listed | — |
| **OQ-10** | Encryption at rest: is Oracle Advanced Security (TDE) licensed, or do we rely on encrypted storage volumes only?                                                                                       | Encrypted volumes for v1; TDE if already licensed.                     | —                                                                                           |

### Resolved

| ID    | Resolution                                                                                                                                                                                                                                                                                                                                                                                                                        | Date       |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| OQ-1  | Share links are **internal only** (FR-SHARE-07). Public links out of scope.                                                                                                                                                                                                                                                                                                                                                       | 2026-10-26 |
| OQ-3  | v1: accounts created directly against the database via the server CLI (FR-ADMIN-01). Admin UI is Phase 2 (BL-19); self-registration is out of scope. **Replaced by OQ-60** for user management.                                                                                                                                                                                                                                                                              | 2026-10-26 |
| OQ-4  | Proposed limits confirmed (D18).                                                                                                                                                                                                                                                                                                                                                                                                  | 2026-10-26 |
| OQ-5  | Product name: **StateLife DropShare** (D20).                                                                                                                                                                                                                                                                                                                                                                                      | 2026-10-26 |
| OQ-12 | Local development: the client accepts `http://` only for `localhost`/`127.0.0.1`; every other server URL must be HTTPS (NFR-SEC-01 unchanged for real deployments).                                                                                                                                                                                                                                                               | 2026-10-26 |
| OQ-13 | Development-only `--profile <name>` client flag: separate app-data folder, sync root, credential entry and single-instance name, so two clients can run on one Mac for sync testing. Not exposed in shipped builds.                                                                                                                                                                                                               | 2026-10-26 |
| OQ-14 | NFR-SEC-04: length rules (12–128) apply, and a new password is refused if it is on the bundled common-password list: SecLists `Pwdb_top-1000000.txt` (MIT, commit `43cbd03`), keeping only entries of 12–128 characters (about 30,000; shorter ones are already refused by length), compared case-insensitively (NFC + casefold). The list ships with the server (`core/common_passwords.txt`, licence beside it) and is rebuilt with `scripts/dev/build_common_passwords.py`. A refused password is a 400 field error (`password_common`), not audited, like a reused one. | 2026-11-02 |
| OQ-15 | Interim behaviour until roadmap F1/F2/F5 land: a local rename/move is sent as trash + re-upload (no data loss; FR-SYNC-10 pending); conflict notifications appear only as tray messages until the Activity view exists; uploads resume within a session but not across app restarts.                                                                                                                                              | 2026-10-26 |
| OQ-16 | Refresh tokens are strictly single-use (FR-AUTH-03): re-presenting a rotated token always revokes the session, with no grace window. The client must never resend a refresh token it already used (e.g. after a timeout); it signs in again instead. Revisit if this signs users out too often. A sign-in lockout also applies to logins that match no account, so a 423 never reveals whether an account exists (FR-AUTH-01/06). | 2026-10-26 |
| OQ-17 | Client versions are `MAJOR.MINOR.PATCH` only; pre-release suffixes (`1.4.0-beta.1`) are rejected. Beta builds are distinguished by release channel (FR-UPD-05), not by version string.                                                                                                                                                                                                                                            | 2026-10-26 |
| OQ-18 | FR-SYNC-08 mass-delete protection: the 500-file limit always applies; the 25% rule applies only when more than 10 files would be deleted in one cycle, so deleting 1 of 3 files doesn't ask. | 2026-10-27 |
| OQ-19 | A client deleting a folder sends its namespace cursor (`DELETE /nodes/{id}?if_revision=&if_cursor=`); the server refuses with `409 REVISION_CONFLICT` if anything inside changed after that cursor, so another device's new file is never taken to the trash with the folder (FR-CONF-03). | 2026-10-27 |
| OQ-20 | A sync folder that already holds files is adopted only when the local index or an owner marker inside the folder (`.dropshare-tmp/owner.json`) says it belongs to the signed-in account on that server; otherwise the user chooses a new folder. The index is removed at sign-out only once the folder itself was moved to the Recycle Bin. | 2026-10-27 |
| OQ-21 | NFR-COMPAT-03 interim: until long-path support lands (roadmap F2), paths beyond Windows' MAX_PATH are listed in "Can't sync" with a reason, never treated as deleted. | 2026-10-27 |
| OQ-22 | Sync progress is shown in a **Transfers view** in the app (a progress bar per file) plus an **overall summary in the tray** (FR-CLIENT-08). No progress on Explorer icons (that's BL-09, Phase 2). | 2026-10-28 |
| OQ-23 | Uploading from the app **copies the files/folders into the sync folder**, and the sync engine uploads them (FR-FILE-02). More generally, browser actions on synced items (upload, new folder, delete, rename, move) act on the local sync folder, so there is one write path. The exception is individually shared files, which aren't in the sync folder (FR-SHARE-03); those use the API directly. | 2026-10-28 |
| OQ-24 | The share dialog finds people by **searching all active users by name or email**, and groups by name (FR-SHARE-08). | 2026-10-28 |
| OQ-25 | Local development and tests may run on **PostgreSQL** (Docker) instead of Oracle for now, selected with `DB_DIALECT=postgresql` (driver: psycopg 3). Oracle 19c stays the default and the staging/production target (D3); the Oracle code path and its workarounds (IN-list chunking, no partial indexes, `''` is NULL) are kept, so reverting is a settings change. Server tests should be re-run on Oracle before staging. | 2026-10-28 |
| OQ-26 | FR-FILE-02 "Replace" on a folder merges: files with matching names are replaced, everything else in the existing folder is kept. | 2026-10-28 |
| OQ-27 | FR-CONF-03: if the same item was moved or renamed differently on both sides, the server's location wins and the local item is moved to match. No content changes, so nothing is lost. | 2026-10-28 |
| OQ-28 | FR-FILE-02 cancel removes everything that upload created (files, subfolders, and the top folder if the upload made it); files replaced by the upload are put back. Items that existed before the upload are never touched; anything that already reached the server goes to the server trash through normal sync. Browser actions on items without a local copy yet are disabled with a reason (OQ-23 keeps one write path). | 2026-10-28 |
| OQ-29 | Interim for clients from before rename/move support (FR-SYNC-10): they don't know the `moved` change type, so they fetch the node and apply another device's rename as Recycle Bin + download (a large folder rename can show their mass-delete prompt; nothing is lost). `MIN_CLIENT_VERSION` is not raised for this; the release notes say so. Release order: the server is always upgraded before clients, since a new client sends renames as `PATCH /nodes/{id}`. | 2026-10-28 |
| OQ-30 | Sharing a folder (FR-SHARE-01) keeps it where it is in the owner's tree: it becomes a shared namespace (node IDs kept) mounted at its old place. When the owner later moves or renames it, their mount moves with it. | 2026-10-28 |
| OQ-31 | Recipients may rename and move their shared folder within their own tree; this changes only their own mount, never the folder's name for others (FR-SHARE-02). A mount is a folder node in the user's own namespace that points at the shared namespace, so it is renamed and moved with `PATCH /nodes/{id}` like any folder, shares the folder's name rules, and leaving or losing access trashes it. | 2026-10-28 |
| OQ-32 | The editor of an individually shared file (FR-SHARE-03) can save a copy and upload a new version from the app; renaming and deleting stay with the owner. | 2026-10-28 |
| OQ-33 | When a recipient loses access to a shared folder (removed, removed from the group, or leaving) while it holds local changes not yet uploaded: changed and new files are moved into `<folder> (access removed)` in the user's own root and sync to their own account (counting against their quota); the rest of the folder goes to the Recycle Bin; the user is told. Without unsynced changes, the folder goes to the Recycle Bin (FR-SHARE-04, NFR-REL-01). | 2026-10-28 |
| OQ-34 | `MIN_CLIENT_VERSION` is raised to 0.2.0 with sharing (M8): an older client doesn't know mounts or the new `removed` change type, and would send the owner's newly shared folder to the Recycle Bin. | 2026-10-28 |
| OQ-35 | Only a shared folder's owner may move items out of it (into another namespace); an editor is refused (403) and copies them instead. When moved items change owner, single-file shares on them end (audited as `share.removed`): the new owner manages their sharing (FR-SHARE-01/04). | 2026-10-28 |
| OQ-36 | Deleting your own shared folder (after removing everyone's access) sends that folder's change cursor (`DELETE /nodes/{id}?if_mount_cursor=`); an edit inside it the deleting device hasn't seen refuses the delete, as `if_cursor` does for ordinary folders (FR-CONF-03, OQ-19). A folder holding your shared folder isn't deleted with it: the shared folder is deleted on its own first. | 2026-10-28 |
| OQ-37 | Trash (FR-TRASH-01…03): the owner of a namespace sees its whole trash; an editor of a shared folder sees what they deleted from it and may restore it while still an editor; only the owner deletes permanently or empties. A recipient's mount of someone else's shared folder (left, or access ended) isn't shown in their trash: they add the folder again from Shared with me, and purge-trash removes the mount later. An item deleted from a shared folder that has itself been deleted since can't be restored on its own (409): the shared folder is restored first. Permanently deleting a folder keeps items deleted earlier from inside it in the trash, moved up to the nearest remaining folder. A missing parent is recreated by name (an existing live folder of that name is reused); a taken name gets "(restored)", then "(restored 2)"... Every restored item gets a new revision and a CREATED row. A purged shared folder's namespace row, root and journal stay. `DELETE /trash` empties up to 200 items per call and answers `more` (the client calls again); `removed` counts only items the trash listed. Restoring or deleting permanently something that changed meanwhile (restored, its folder deleted, an upload finishing into it) is 409 `REVISION_CONFLICT`: try again. | 2026-10-29 |
| OQ-38 | Cleanup jobs (NFR-REL-05): gc-blobs removes versions superseded more than `BLOB_GRACE_HOURS` (24) ago and blobs older than that with no version row, so running downloads finish. purge-audit (FR-AUDIT-03, `AUDIT_RETENTION_DAYS`=365) deletes through `AUDIT_PURGE_DB_USER`/`_PASSWORD` when set (production: the only account with DELETE on audit_events), else the app account (dev/test), and records `audit.purged`. purge-trash uses `TRASH_RETENTION_DAYS` (30). The change journal no longer has a foreign key to nodes and stores each row's `mount_namespace_id`, so purged items' rows stay and are still served. gc-blobs refuses (deleting no unreferenced blob) when more than 100 blobs and more than 10% of storage, or every blob, look unreferenced (wrong database or older backup), unless run with `--force`. purge-audit logs itself even when it fails partway. | 2026-10-29 |
| OQ-39 | Transfers (FR-XFER-01/03): up to 4 files move at once (`MAX_TRANSFERS`, a setting in M11), smallest first. Transfer threads only move bytes; everything the engine records (index, remote tree, journal) stays on its own thread as each transfer finishes, so crash safety is unchanged. Each transfer starts only when a slot is free, so a pause sends nothing more. Once only files of 4 MiB or more are moving, the cycle ends without them and the next one sends what changed meanwhile beside them. An over-quota answer while other uploads are moving is retried, not listed as can't sync. Started uploads are kept in the local index (`upload_sessions`, one per target with the content it is for; a session for other content is cancelled) and resume after the app restarts; one found completed by recovery is forgotten, and a replay whose file has moved since is uploaded again; partial downloads resume from their `.part` file. Chunks of one file are not sent in parallel unless a measurement over a real network shows one stream below NFR-PERF-04. Ends the OQ-15 interim rule that uploads resume only within one run. | 2026-10-30 |
| OQ-40 | Performance tests (NFR-PERF-01/02/04/05) are scripts in `scripts/perf/` run against a running server, writing their numbers to `docs/perf-results.md`; not part of pytest. On the development Mac they run at a smaller scale (100 users; 5,000 files / 5 GB); the full scale (50,000 files / 50 GB) and NFR-PERF-04 over a real network are run on the Windows machine or staging. | 2026-10-30 |
| OQ-41 | Settings (FR-CLIENT-04) shows the version in About; "Check for updates" and the "update available" notification (FR-CLIENT-02) arrive with the updater in M13. | 2026-10-30 |
| OQ-42 | Moving the sync folder (FR-CLIENT-04): the new place must pass the first-run rules (new or empty, NTFS, on this computer) and be neither inside the folder nor holding it. Syncing stops first and is held until the move is done (it only proceeds if the engine really stopped). The old folder is first renamed aside (`<name>.dropshare-moving`), so a file open in another program refuses the move and nothing goes on writing under the old path. On the same volume each item is then renamed into the new place; on another, everything is copied and each copy checked by SHA-256 (a file edited meanwhile is copied again), and links refuse the move. The index keeps its entries (paths are relative), then the old folder goes to the Recycle Bin, or is left and reported if it can't. The new place is checked again at move time, and must hold everything (each copied file, at its size) before the switch and again when a crash recovery finishes it. A journal in the app data folder lets a crash finish a verified move or undo an unverified one (on a worker at start, before syncing); undoing removes only identical copies and DropShare's own part files. The new drive is flushed before the move counts as verified. A verified move whose new place no longer holds everything (drive gone, a copy deleted or quarantined) goes back to the old folder. If something can't be put back (a file stays locked), the journal stays and syncing (and sign-out) stays paused until a later start succeeds, so missing items are never taken as deletes; nothing new starts syncing until an engine that was slow to stop has stopped. OS litter already in the new place (desktop.ini) is never overwritten. Signing out waits for a move to finish. | 2026-10-30 |
| OQ-43 | Quota notifications (FR-CLIENT-02): the client reads `GET /me` at sign-in and after uploads, at most once a minute; crossing 90% and 100% each notify once per run, and again only after use drops below 90%. "Can't sync" notifies only for items newly unable to sync, not for those an earlier run left. Notifications of one type are gathered for 3 s and shown at most once every 10 s. | 2026-10-30 |
| OQ-44 | Start at Windows sign-in (FR-CLIENT-04, default on) is the per-user Run key, registered at the first sign-in of an installed (frozen) build; from source and on the Mac it isn't offered. The user's choice is kept, and registered again at each sign-in in case the app moved. | 2026-10-30 |
| OQ-45 | Devices (FR-AUTH-07): the list shows devices not revoked and seen within the refresh-token inactivity limit (30 days), most recent first, with this computer marked; "last seen" is updated at sign-in and each token refresh (about every 15 minutes). This computer is signed out with Sign out, not from the list (the server still accepts it). Revoking is audited as `auth.device_revoked`. | 2026-10-30 |
| OQ-46 | Quotas (FR-ADMIN-01): `users set-quota` never sets a quota below what the user already stores, trash included (`used_bytes`); there is no override. It is checked under the same user-row lock uploads take, so a finishing upload can't slip past it; uploads still in progress aren't counted and are refused at completion if the new quota leaves no room. The storage report lists quota, usage, % used and trash per user, plus file-version and blob totals; byte totals come from the database (the storage interface doesn't report sizes), and only the blob count from storage. | 2026-11-02 |
| OQ-47 | Reverse proxy (NFR-SEC-01/06): reference config and runbook in `docs/deploy/` (nginx). TLS 1.2+; one HSTS header `max-age=31536000` without `includeSubDomains` (other hosts on the organization's domain aren't ours); the app sends the same header as a fallback when reached over HTTPS directly or via a `TRUSTED_PROXIES` peer with `X-Forwarded-Proto: https`. Rate limits per source address, IPv6 grouped by /64: a global flood guard (100/s, burst 500, set above an initial sync's rate) and a stricter limit on sign-in and password change (20/min, burst 10); token refresh only falls under the global limit (refresh tokens can't be guessed and every device refreshes about every 15 minutes). Office egress ranges may be exempted from the global limit only. The proxy's 429 is in the API error envelope with `Retry-After` and `X-Request-ID`. | 2026-11-02 |
| OQ-48 | Interim hosting: the server runs on **Windows** for now. D19 and NFR-COMPAT-06 stand (Linux in production, OS-neutral server code); Linux-specific work (nginx reference, cron jobs) waits until hosting moves. Windows-only server code, such as the Authenticode check, lives in one isolated module behind a protocol. | 2026-11-02 |
| OQ-49 | Distribution (FR-UPD-01 … 05): the installer doesn't create the start-at-sign-in entry (OQ-44) but removes it on uninstall; removing app data at uninstall also deletes the Credential Manager entry. `releases publish` refuses an installer whose Authenticode signature or publisher doesn't verify (it runs on the Windows server, OQ-48); `releases list` and `releases withdraw` exist. The channel is per user (`users set-channel`, default stable) and reported by `GET /me`; the server picks it, the client doesn't pass it. `/auth/refresh` and `/client/releases/*` skip the minimum-version check (still signed in), so a client that is too old can fetch its update. The effective minimum version is the higher of `MIN_CLIENT_VERSION` and the minimum of the latest published stable release. Installers must be signed by `RELEASE_PUBLISHER` with a certificate whose SHA-256 is pinned (`RELEASE_CERT_SHA256` on the server, `cert_sha256` in the client build), and their version resource must equal the release version (audit 2026-11-02-0544). Clients from before the updater (0.2.0) are upgraded by hand; `--min-supported` stays at 0.2.0 until they are. | 2026-11-02 |
| OQ-50 | Production deployment (interim, OQ-48): Docker Compose (`docker-compose.yml`) on a Windows server with Docker Desktop, deployed by GitHub Actions on every push to `main` after ruff, mypy and the shared/server tests pass on Linux (self-hosted runner labelled `dropshare`). The database is **PostgreSQL 16 in a container** for now, a change to D3: the Oracle code path stays, but moving back needs a data migration as well as the settings change, and server tests must pass on Oracle again first. Postgres is published on the host's loopback only, for the host's admin CLI (`releases publish` needs Windows). File storage is the local filesystem backend on a server folder (`E:\DropShareData`), never created by the deploy. The API listens on port 5009 for the existing nginx on another machine: on loopback until the Windows firewall allows that port from the proxy only, then on the server's address (`DROPSHARE_API_BIND`, NFR-SEC-01). Only `main` deploys; the `dropshare-production` environment is limited to `main`, because the runner can control the host. A `jobs` container runs the NFR-REL-05 jobs at each deploy and then daily. Known gaps, accepted for now: **no backups** (NFR-REL-06 not met; backups and a tested restore are needed before go-live); **the app connects as the PostgreSQL superuser**, so DB grants don't enforce FR-AUDIT-03 and §12's separate schema owner and app user don't exist yet; **nginx → app is plain HTTP** across the internal network (NFR-SEC-01 relies on that network being trusted until TLS is added on the hop). Runbook: `docs/deploy/windows-docker.md`. | 2026-11-03 |
| OQ-51 | Server address (FR-CLIENT-05): installer builds, test builds included, don't offer "Use a different server" and refuse to sign in anywhere but the built-in address; only a run from source can choose the server. An installed client still keeps the address it first signed in with, so a later build with another built-in address doesn't move it off the server its sync folder belongs to; moving such a client to another server means a clean reinstall. Replaces "can be overridden in an advanced setting for testing". | 2026-11-04 |
| OQ-52 | "Active users" in the share dialog means **enabled accounts** (everyone who can sign in). There is no online or recently-active status. | 2026-11-05 |
| OQ-53 | The share dialog keeps type-to-search (FR-SHARE-08: at least 2 characters, at most 20 results); it doesn't list everyone before you type. The signed-in user is never among the results. | 2026-11-05 |
| OQ-54 | Malware scanning moves from P2 to **v1** (FR-SAFE-01, NFR-SEC-11, BL-10) as its own milestone (roadmap M15) with rigorous testing. The maximum file size is lowered from 10 GB to **3 GB** (D18, NFR-SCALE-01) so every file fits the scanner; folders have no size limit. Files saved straight into the sync folder are checked per file. | 2026-11-05 |
| OQ-55 | When the malware scanner is down, uploads **wait** (fail closed): nothing becomes visible unscanned, the client shows the item as waiting for the scan, and the upload completes once the scanner is back (FR-SAFE-01). | 2026-11-05 |
| OQ-56 | File-type policy: one **banned list** of extensions for everyone, in server configuration (not an allowlist, not per user or folder). The server also checks content, so executables (Windows PE, ELF, Mach-O) are refused whatever their extension and a renamed `.exe` is caught (FR-SAFE-02/03). | 2026-11-05 |
| OQ-57 | Content safety (FR-SAFE-01 … 03), confirmed: (1) DropShare never deletes, moves, or quarantines a local file because of a ban or a detection; it only declines to sync it. (2) Files already on the server when a type is banned, or larger than the size limit, stay and can still be downloaded; new versions of them, and renames or moves of them, must pass the checks. (3) For v1 the ban list doesn't look inside archives, though ClamAV scans their contents; password-protected archives can't be scanned and are accepted. Revisit after the pilot. (4) Stored files aren't rescanned when signatures update, and downloads aren't scanned again. (5) The scan runs after `POST /uploads/{id}/complete`, with the upload in a "scanning" state that the client checks; this changes the upload API and the client's upload flow. (6) The starting ban list is `.exe .dll .msi .bat .cmd .com .scr .ps1 .vbs .js .jse .wsf .hta .jar .lnk .reg .iso`; macro-enabled Office types (`.docm .xlsm .pptm`) are allowed. (7) Engine: ClamAV (the official image, `clamav/clamav-debian` since it also runs on the development Macs, `clamd` + `freshclam`) as its own container in `docker-compose.yml` on the private network, no published ports, about 4 GB RAM, outbound HTTPS to `database.clamav.net` for signatures; the server talks to it over the `clamd` socket protocol with the standard library only. Approved as new infrastructure. (8) Moving or renaming a folder is refused while it holds a file with a banned extension. (9) Restoring a file with a banned extension from the trash is refused. Restoring a folder brings back everything else and leaves its banned files in the trash, each as its own item that can be deleted for good (assumption, proceeding on request: refusing the whole folder would leave its allowed files unrecoverable, audit 2026-11-05-0304). (10) Sharing a file with a banned extension, or adding a member to a folder that holds one, is refused like a move into a shared folder (assumption, proceeding on request; audit 2026-11-05-0304). | 2026-11-05 |
| OQ-58 | Malware scanning details (FR-SAFE-01, roadmap M15b): (1) there is no setting that turns scanning off, in development either; developers run the `clamav` container and tests use a fake scanner. (2) ClamAV's size limits are set to their maximum with `AlertExceedsMax`, so a file it can't scan in full (an archive bomb, very deep nesting) is refused as `MALWARE_DETECTED` (`Heuristics.Limits.Exceeded`); password-protected archives stay accepted (OQ-57 (3)). (3) `/health/ready` fails only when the scanner can't be reached; it reports the signature age and logs a warning when signatures are older than 48 hours. Files up to 64 MiB are scanned within `POST /uploads/{id}/complete` (200); larger ones, or any while the scanner is down, answer 202 with the upload `scanning` and a worker finishes them. (4) The maximum file size is **2,000 MiB** (2,097,152,000 bytes), decided 2026-11-07: testing showed ClamAV 1.4 scans at most 2 GiB − 1 byte of a file, whatever its settings, so the 3 GB limit of OQ-54 would have refused files between 2 and 3 GB as unscannable. (5) Interim for clients from before the client part of M15 (0.2.x; assumption, proceeding on request, audit 2026-11-07-0309): they don't know the 202 answer and retry it as an unexpected response until the scan is done, without sending the file again; a refusal after the scan reaches their "Can't sync" list. Uploading a new version of an individually shared file (OQ-32) that is scanned by the worker shows them a "response this app doesn't understand" error although the upload goes on, and trying again after it finished can store the same content as one more version. Whether `MIN_CLIENT_VERSION` is raised with the client part (FR-UPD-04) is decided there. (6) Archives are scanned up to 4000M unpacked in total, 100,000 files and clamd's default nesting of 17 levels; anything beyond is refused as in (2). (7) A file the scanner keeps failing on (5 tries) is refused as unscannable (`MALWARE_DETECTED`, `DropShare.Unscannable`), never published. An upload whose uploader is disabled, or whose device is revoked, while it waits is not published. | 2026-11-05 |
| OQ-59 | Client side of content safety (FR-SAFE-01/03, roadmap M15c): (1) A synced item renamed locally to a name DropShare can't sync (a banned type, or a path too long for Windows) is held: the server copy keeps its old name and is never trashed, and renaming it back resumes syncing. This also closes the same gap for too-long paths that existed before M15. (2) The first client with the client part of M15 is **0.2.3**; it is published with a minimum supported version of 0.2.3 (OQ-49, FR-UPD-04), so older clients are asked to update and the 0.2.x interim of OQ-58 (5) ends. (3) A malware detection is announced through the existing "Can't sync" notification (OQ-43), worded for a virus; there is no separate notification type. (4) Files synced before their type was banned keep syncing while unchanged here (OQ-57 (2)); edited here, they aren't sent. (5) While the server scans an upload, the client asks again with growing pauses (up to 5 minutes). Once published, the upload is recorded as synced, so a delete, rename or edit made here meanwhile is applied as such; a new file renamed during its scan is uploaded once more under its new name, and the published original then goes to the trash. A new version of an individually shared file (OQ-32) is asked about in the background until it is published or refused, while the app runs, and the user is told either way. (Audit 2026-11-07-0712.) | 2026-11-07 |
| OQ-60 | In-app user administration moves to **v1** (roadmap M16), changing D12 and replacing OQ-3 for users. "Super user" is the existing `admin` role; no new role. Admins create users (username, email, display name, role, quota) and disable/enable, reset passwords, change quotas (OQ-46 rule), and change roles from an Administration section in the desktop app. The one-time temporary password is always shown once to the admin **and** emailed through the organization's email API (FR-ADMIN-07); if the email fails, the action still stands. Admins can't change their own status or role, and at least one enabled admin always remains. Actions are audited with the admin as actor, under the CLI's action names. Rate limiting is in the app, per admin (NFR-SEC-13). The CLI stays unchanged. `httpx` approved as a server dependency (the client already uses it). | 2026-11-11 |

## 16. Glossary

| Term                        | Meaning                                                                                           |
| --------------------------- | ------------------------------------------------------------------------------------------------- |
| **Sync folder / sync root** | The local folder the client keeps in two-way sync with the server.                                |
| **Namespace**               | A separately-synced tree with its own change journal: a user's personal space or a shared folder. |
| **Mount**                   | A shared folder a recipient has accepted, appearing in their sync folder.                         |
| **Node**                    | A file or folder record in the database.                                                          |
| **Revision**                | A counter on each node that increases on every change; used to detect stale writes.               |
| **Cursor**                  | The position in a namespace's change journal the client has synced up to.                         |
| **Blob**                    | The stored bytes of one file version, addressed by an opaque key.                                 |
| **Conflicted copy**         | The extra file created when the same file changed in two places.                                  |
| **Long-poll**               | An HTTP request the server holds open until there is news or a timeout.                           |
