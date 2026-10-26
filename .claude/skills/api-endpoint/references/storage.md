# File storage (server side)

Loaded by the `api-endpoint` skill for uploads, downloads, blobs, storage backends, and cleanup jobs.
Requirements: §11, FR-XFER-01/02, NFR-REL-02/03/05, NFR-COMPAT-05.

## Interface

All file bytes go through one protocol. Nothing else opens blob files or talks to an object store.

```python
class StorageBackend(Protocol):
    def write_chunk(self, upload_id: UUID, index: int, data: bytes) -> None: ...
    def chunk_indexes(self, upload_id: UUID) -> set[int]: ...
    def commit(self, upload_id: UUID, chunk_count: int, blob_key: str) -> CommitResult: ...
    """Assemble chunks into the final blob; return size and SHA-256 computed while assembling."""
    def open_range(self, blob_key: str, start: int, end: int | None) -> Iterator[bytes]: ...
    def delete_blob(self, blob_key: str) -> None: ...
    def discard_upload(self, upload_id: UUID) -> None: ...
    def iter_blob_keys(self, older_than: datetime) -> Iterator[str]: ...
    def health_check(self) -> None: ...
```

- Implementations: `LocalFilesystemBackend` (dev and NAS/SAN mounts) and `S3Backend` (S3-compatible; only add once OQ-2 is decided — `boto3` needs approval).
- Selected by settings (`STORAGE_BACKEND=local|s3`, plus backend-specific settings). Services receive the backend via dependency injection, never construct it.

## Keys

- Blob keys are generated: `blobs/{uuid[:2]}/{uuid[2:4]}/{uuid}`. Never derived from user input, file names, or paths.
- One blob per `FileVersion`. Blobs are immutable: a save creates a new version and a new blob; nothing is overwritten (G6).

## Upload flow (FR-XFER-01)

1. `POST /uploads` — validate name, size (≤ `MAX_FILE_SIZE`), permission on the target, and reserve quota. Create an `UploadSession` (expires in 24 h) with chunk size 8 MiB. For edits of an existing file, store the base revision.
2. `PUT /uploads/{id}/chunks/{index}` — `async def`; read the body (reject if larger than the chunk size or the declared last-chunk size), verify `X-Chunk-SHA256`, then `write_chunk` in the threadpool. Re-sending an existing chunk is a no-op success (idempotent).
3. `GET /uploads/{id}` — returns received chunk indexes so a client can resume.
4. `POST /uploads/{id}/complete` — check all chunks present, `commit()` to assemble and compute the whole-file SHA-256, compare with the client's declared hash (`CHUNK_HASH_MISMATCH`/`FILE_HASH_MISMATCH` on mismatch), then in **one DB transaction**: create the `FileVersion`, create/update the `Node` (revision check against the base revision), write the `Change` row, settle quota, record audit. Completing an already-completed session returns the same node (idempotent retries).
5. `DELETE /uploads/{id}` — discard and release reserved quota.

Filesystem backend details: chunks go to `staging/{upload_id}/{index:06d}`; `commit` streams them in
order into a temp file in the blobs volume while hashing, calls `fsync`, then `os.replace` into the
final key, then deletes the staging directory. Staging and blobs must be on the same volume so the
replace is atomic.

## Download flow (FR-XFER-02)

- `GET /nodes/{id}/content` — authorize, find the current version, honour a single `Range: bytes=start-end` (return 206 with `Content-Range`; 416 if unsatisfiable), stream `open_range()` through `StreamingResponse` in 1 MiB pieces.
- Headers: `Content-Length`, `Accept-Ranges: bytes`, `ETag` = content SHA-256, `Content-Disposition: attachment` with RFC 5987 encoded filename, `X-Content-Type-Options: nosniff`.
- Never read a whole file into memory.

## Deletion and cleanup (NFR-REL-05)

- Trash doesn't touch storage. Permanent delete removes DB rows first, then deletes blobs after commit; failures are logged and left for GC.
- In v1 the retention policy keeps only the current version: when a new version commits, older versions are marked for removal and their blobs deleted by a job. (Phase 2 version history changes the policy, not the layout.)
- Jobs (run via `dropshare-server jobs run <job>` from the OS scheduler):
  - `expire-uploads`: discard sessions past `expires_at` and release quota.
  - `purge-trash`: permanently delete items trashed longer than the retention period.
  - `gc-blobs`: delete blob keys older than 24 h with no `FileVersion` row; delete superseded versions per retention policy.
  - `purge-audit`: remove audit events past retention (logged).
- Every job is idempotent, processes in batches, and is safe to run concurrently with the API.
