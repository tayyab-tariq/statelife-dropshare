# Sync algorithm

Loaded by the `sync-engine` skill and the `sync-reviewer` agent. Requirements §10 and FR-SYNC/CONF.

## Contents
1. Components
2. Local index
3. Inputs: local and remote changes
4. Reconciliation
5. Scenario matrix
6. Conflicts
7. Moves and renames
8. Deletes and mass-delete protection
9. Shared folders and viewers
10. Operation journal and crash recovery
11. Engine states

## 1. Components

```
watcher ──► local event queue ─┐
scanner (startup/reconnect/30 min) ┤
                                   ├─► reconciler ──► operation planner ──► executor ──► index update
long-poll ──► remote change fetch ─┘        ▲                                   │
                                            └────── local index (base) ◄────────┘
```

- **Watcher:** watchdog observer on the sync root; events are hints, debounced (~1 s) and coalesced by path.
- **Scanner:** walks the sync root and compares size, mtime, and file ID against the index; hashes only when those differ.
- **Remote fetch:** `GET /namespaces/{id}/changes?cursor=` per namespace until `has_more` is false.
- **Reconciler:** pure function deciding actions from (local, remote, base) per path.
- **Executor:** performs uploads, downloads, moves, deletes, conflict copies through the transfer layer, with bounded concurrency.

## 2. Local index (`store/`)

SQLite file at `%LOCALAPPDATA%\StateLife\DropShare\index.db`, accessed through SQLAlchemy and owned by the
engine's executor thread (one writer). WAL mode on.

| Table | Purpose |
|---|---|
| `entries` | One row per synced item: relative path, normalized path, node ID, namespace ID, kind, server revision, content hash, size, local mtime, local file ID, state (`synced`, `pending_upload`, `pending_download`, `conflict`, `cant_sync`), reason |
| `cursors` | Change cursor per namespace |
| `mounts` | Accepted shared folders and their local top-level names, role (viewer/editor) |
| `journal` | In-flight multi-step operations (section 10) |
| `activity` | Recent events for the Activity view (bounded, oldest pruned) |

Paths are stored relative to the sync root with `/` separators; comparisons use the normalized
form (NFC + casefold) because NTFS is case-insensitive.

## 3. Inputs

- **Local change** for a path = current disk state (exists?, size, mtime, file ID, hash if needed) differs from the index entry.
- **Remote change** for a path = a change from the journal newer than the index's revision for that node.
- Files still being written (size/mtime changed within the last 2 s) are deferred, not treated as changes yet (FR-SYNC-06).
- Ignored names (FR-SYNC-07) never enter reconciliation.

## 4. Reconciliation

For each affected path, with **L** = local vs base and **R** = remote vs base:

| L | R | Action |
|---|---|---|
| none | none | nothing |
| modified/created | none | upload (with base revision) |
| none | modified/created | download |
| deleted | none | delete on server (to trash) |
| none | deleted | move local copy to Recycle Bin |
| modified | modified | same hash → update index; different → conflict copy (section 6) |
| created | created (same name) | same hash → link in index; different → conflict copy |
| modified | deleted | keep local: re-upload as new file (edit wins, FR-CONF-02) |
| deleted | modified | restore: download the server version (edit wins) |
| deleted | deleted | remove index entry |

Folders: a folder created on either side is created on the other; a folder deleted on one side is
deleted on the other only if the other side has no un-synced children — otherwise the children are
kept and the folder is recreated (FR-CONF-03).

Order of execution per batch: folder creates (parents first) → moves → downloads/uploads → deletes (children first).

## 5. Scenario matrix (tests must cover)

For files **and** folders, each combination of local {none, create, modify, delete, rename, move}
× remote {none, create, modify, delete, rename, move}, plus:

- Case-only rename (`report.docx` → `Report.docx`) on either side.
- Rename on one side, modify on the other (rename applies, content merges into the renamed item).
- Move into a folder that is concurrently deleted.
- Two devices creating the same name with different content.
- Crash at each executor step (before/after server call, before/after `os.replace`, before/after index write), then restart.
- Request succeeded on server but timed out on client, then retried.
- File locked during download (target in use) and during upload (being written).
- Viewer modifying a read-only shared file.

## 6. Conflicts (FR-CONF-01)

- The **server version keeps the original name** and is downloaded into place.
- The local version is renamed to `Name (conflicted copy – Display Name – YYYY-MM-DD HH-MM).ext` (hyphens instead of colons; a numeric suffix ` (2)` if that name exists), then uploaded as a new file.
- Order: rename local file → journal → download server version → upload conflicted copy → index updates. A crash at any step leaves both contents on disk.
- Emit a `conflict_detected` event (notification + Activity entry).

## 7. Moves and renames (FR-SYNC-10)

- Local: a delete + create in the same debounce window with the same file ID (or same size + hash when the ID changed, e.g. cross-volume tools) is a move. Send `PATCH /nodes/{id}` with the new parent/name and base revision.
- Remote: a change that alters parent or name is applied with `os.replace` within the sync root (directories included); if the target name exists locally with different content, conflict rules apply.
- Never implement a move as delete + re-upload when identity is known.

## 8. Deletes and mass-delete protection

- Before applying a batch, count pending deletions (both directions). If > 500 files or > 25% of synced files, pause the engine in a `needs_confirmation` state and ask the user (FR-SYNC-08). Nothing is deleted until they confirm.
- Remote deletes: move the local item to the Recycle Bin via the platform helper (FR-SYNC-09). If that fails (e.g. Recycle Bin disabled on the drive), move it to `<sync root>\.dropshare-tmp\deleted\` and surface it in Can't sync — never hard-delete.
- Local deletes: `DELETE /nodes/{id}` (server trash).

## 9. Shared folders and viewers

- Each accepted shared folder is a separate namespace with its own cursor, mounted at a top-level folder of the sync root (FR-SHARE-02).
- Viewer mounts: set files read-only on disk. If a viewer's file changes anyway, don't upload: move the modified file to the user's own root as a conflicted copy, restore the original, notify (FR-SHARE-05).
- When access is removed (mount gone from `GET /namespaces`), stop syncing it and move its local folder to the Recycle Bin only if it has no un-synced local changes; otherwise rename it to `Name (no longer shared)` and keep it.

## 10. Operation journal and crash recovery

- Multi-step operations (conflict handling, moves with replace, download-then-replace) write a `journal` row with the intended steps **before** starting, update it after each step, and delete it when done.
- On startup, before normal sync: for each journal row, inspect disk and server state and either finish the operation or roll it back to a state where no content is lost. Then run a full scan.
- Upload sessions in progress are stored in the journal with their upload ID so they resume (`GET /uploads/{id}`) instead of restarting.

## 11. Engine states

`signed_out`, `starting`, `scanning`, `syncing`, `up_to_date`, `paused`, `offline`, `needs_confirmation`, `upgrade_required`, `error`.
These map to the tray states in FR-CLIENT-01 and are emitted through the callback interface whenever they change.
