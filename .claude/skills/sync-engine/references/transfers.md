# Transfers, notifications, and retries (client side)

Loaded by the `sync-engine` skill. Server side of the same protocol: `api-endpoint` → `references/storage.md`.
Requirements: FR-XFER-01..03, FR-SYNC-03, NFR-REL-02/04, NFR-PERF-02/04.

## Uploads (resumable, chunked)

1. Wait until the file is stable (FR-SYNC-06) and can be opened for reading. Open it with sharing that tolerates other readers; if it is locked, retry later.
2. Hash the whole file (SHA-256) in a streaming pass. Note size and mtime.
3. `POST /uploads` with parent/name (new file) or node ID + base revision (edit), size, and hash. Store the returned upload ID in the journal.
4. For each missing chunk (8 MiB; ask `GET /uploads/{id}` after a restart), read the slice, compute its SHA-256, `PUT /uploads/{id}/chunks/{index}` with `X-Chunk-SHA256`. Up to 2–3 chunks of the same file in flight.
5. Before completing, check the file hasn't changed since step 2 (size + mtime). If it has, discard the session and start over.
6. `POST /uploads/{id}/complete`. On success, update the index with the returned node, revision, and hash. On `REVISION_CONFLICT`, resolve as a conflict.

## Downloads (resumable, verified)

1. Target temp path: `<sync root>\.dropshare-tmp\<node id>.part` (same volume, so `os.replace` is atomic).
2. `GET /nodes/{id}/content` with `Range: bytes=<existing .part size>-` to resume.
3. Stream to the temp file while hashing; `flush` + `os.fsync` at the end.
4. Verify the SHA-256 equals the expected hash from the change/node. Mismatch → delete the `.part`, retry once, then report.
5. If the target exists and has local changes not yet synced → conflict path. If the target is locked → keep the verified temp file and retry the replace with backoff.
6. `os.replace(temp, target)`, set mtime from the server's modified time, apply read-only if the mount is view-only, then update the index.

## Scheduling (FR-XFER-03)

- Bounded concurrency: default 4 files in parallel, configurable.
- Two queues by size (small < 4 MiB first) so many small changes aren't stuck behind one large file.
- Progress callbacks are throttled (~4 updates/s per transfer) before reaching the UI.

## Remote change notifications

- A dedicated thread loops: `POST /changes/wait` with all namespace cursors (server holds ~55 s). On `changed` → fetch changes for those namespaces → reconcile. On timeout → loop again.
- On network failure → engine `offline`; retry with exponential backoff and jitter (1 s → 2 s → … capped at 60 s). On reconnect → full scan + fetch all namespaces.
- Read timeout for the long-poll is longer than the server's hold time (e.g. 70 s).

## Retries and idempotency (NFR-REL-04)

| Operation | Retry rule |
|---|---|
| Chunk PUT | Always safe to retry (server ignores duplicates). |
| Upload complete | Safe: completing a completed session returns the same node. |
| Create folder | On `NAME_CONFLICT`, fetch the existing item; if it's a folder in the right place, treat as success. |
| Move/rename (`PATCH`) | On `REVISION_CONFLICT` after a timeout, fetch the node; if it already has the intended parent/name, treat as success; otherwise conflict. |
| Delete to trash | On `NOT_FOUND`, check trash/state; if already trashed, success. |
| Any 5xx / network error | Exponential backoff with jitter, max ~5 attempts per cycle, then mark the item for the next cycle. |
| 4xx other than the above | Don't retry; mark the item `cant_sync` with the mapped reason. |
| 401 | Refresh token once (API client handles it); `SessionExpiredError` → stop engine, show sign-in. |
| 426 | Stop the engine (`upgrade_required`) and trigger the updater. |
