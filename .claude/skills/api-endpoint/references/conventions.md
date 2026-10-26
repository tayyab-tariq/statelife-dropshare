# Backend conventions (REST, errors, auth, audit)

Loaded by the `api-endpoint` skill and the `security-reviewer` / `api-contract-reviewer` agents.
Source of truth for server code. Requirements live in `docs/requirements.md`.

## Contents

1. Resource design and status codes
2. Response envelope
3. Lists and pagination
4. Layering
5. Validation
6. Errors
7. Sync vs async handlers
8. Auth and authorization
9. Audit events
10. Security and robustness
11. Configuration

## 1. Resource design and status codes

- Base path `/api/v1`. Plural nouns, no verbs: `GET /api/v1/nodes/{id}`, `PATCH /api/v1/nodes/{id}`.
- Nest one level only, when ownership is real: `GET /api/v1/nodes/{id}/children`.
- `PATCH` for partial updates; non-CRUD actions as sub-resources: `POST /api/v1/trash/{node_id}/restore`.
- IDs in URLs are UUIDs; validate them via path parameter types (`uuid.UUID`).

| Situation                                    | Code                                                                              |
| -------------------------------------------- | --------------------------------------------------------------------------------- |
| Read success                                 | 200                                                                               |
| Created                                      | 201 + created resource                                                            |
| Deleted / no body                            | 204                                                                               |
| Partial content (ranged download)            | 206                                                                               |
| Validation failed                            | 400 `VALIDATION_ERROR`                                                            |
| Not authenticated / token invalid or expired | 401 `UNAUTHENTICATED`                                                             |
| Authenticated but not allowed                | 403 `FORBIDDEN` (use 404 when revealing existence would leak another user's data) |
| Not found                                    | 404 `NOT_FOUND`                                                                   |
| Method not supported on this path            | 405 `METHOD_NOT_ALLOWED` (keeps the `Allow` header)                               |
| Name taken / stale revision                  | 409 `NAME_CONFLICT` / `REVISION_CONFLICT`                                         |
| Too large                                    | 413 `FILE_TOO_LARGE`                                                              |
| Over quota                                   | 409 `QUOTA_EXCEEDED`                                                              |
| Client too old                               | 426 `UPGRADE_REQUIRED`                                                            |
| Account locked / rate limited                | 423 `ACCOUNT_LOCKED` / 429 `RATE_LIMITED`                                         |
| Unexpected                                   | 500 `INTERNAL_ERROR`                                                              |
| Not ready (database or storage unreachable)  | 503 `SERVICE_UNAVAILABLE` (`/health/ready` only)                                  |

Framework-raised `HTTPException`s are mapped to the same codes in `core/error_handlers.py`; any
other 4xx becomes `VALIDATION_ERROR` and any 5xx becomes 500 `INTERNAL_ERROR`.

## 2. Response envelope

Generic wrappers live in `shared/` so the client parses the same types:

```python
# shared/src/dropshare_shared/schemas/envelope.py
class Meta(BaseModel):
    page: int
    limit: int
    total: int

class DataResponse(BaseModel, Generic[T]):
    data: T

class ListResponse(BaseModel, Generic[T]):
    data: list[T]
    meta: Meta

class ErrorBody(BaseModel):
    code: Annotated[ErrorCode | str, Field(union_mode="left_to_right")]  # unknown codes stay str
    message: str
    details: dict[str, object] | list[object] | None = None

class ErrorResponse(BaseModel):
    error: ErrorBody
```

Routers declare `response_model=DataResponse[NodeOut]` (or `ListResponse[...]`) so FastAPI
validates outgoing data and drops any field not in the model.

## 3. Lists and pagination

- Page-based lists: `?page=1&limit=50`, default 50, max 200, validated by a shared `PageQuery` model.
- `sort` uses a whitelisted field name with `-` for descending; always add a unique tiebreaker (`id`).
- The change feed is cursor-based, not page-based: `?cursor=<int>&limit=` returns `{ data: [...], meta: { cursor, has_more } }` (a separate `ChangesMeta` model).

## 4. Layering

- **routers** (`api/v1/`): declare paths, models, dependencies; call one service method; build the envelope. No business logic, no SQLAlchemy.
- **services**: business rules, authorization, quotas, revisions, change journal, audit. Receive plain arguments and a repository/unit-of-work; raise `AppError`. No FastAPI or Starlette imports.
- **repositories**: the only code that builds queries or touches the `Session`. Return ORM objects or simple values; never Pydantic response models.
- **storage**: the only code that reads or writes file bytes (see `storage.md`).
- Mapping ORM → response model happens in the service or a small mapper, via `Model.model_validate(orm_obj, from_attributes=True)` on models that only contain safe fields.

## 5. Validation

- Every body, query, and path model comes from `shared/` with `extra="forbid"` and explicit constraints (`min_length`, `max_length`, `ge`, `le`).
- Response models do **not** use `extra="forbid"`: they keep the default (`ignore`) so installed clients keep parsing responses after the server adds fields (FR-UPD-04). Clients must also tolerate unknown `ErrorCode` values (`ErrorBody.code` is `ErrorCode | str`).
- File and folder names: always `dropshare_shared.naming.validate_name()` (rules in NFR-COMPAT-02) and store `normalize_name()` (NFC + casefold) for uniqueness checks.
- FastAPI returns 422 for validation errors by default; we override it so validation errors become `400 VALIDATION_ERROR` with `details` = list of `{loc, msg, type}`.

## 6. Errors

One exception type, central handlers. Services and routers **raise**; they never build error responses.

```python
# server/src/dropshare_server/core/errors.py
class AppError(Exception):
    def __init__(self, status_code: int, code: ErrorCode, message: str,
                 details: dict[str, object] | list[object] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    @classmethod
    def not_found(cls, resource: str = "Item") -> "AppError":
        return cls(404, ErrorCode.NOT_FOUND, f"{resource} not found")

    @classmethod
    def forbidden(cls) -> "AppError":
        return cls(403, ErrorCode.FORBIDDEN, "You don't have permission to do that")

    @classmethod
    def revision_conflict(cls, current_revision: int) -> "AppError":
        return cls(409, ErrorCode.REVISION_CONFLICT, "This item changed on the server",
                   {"current_revision": current_revision})
```

Handlers registered in the app factory map:

- `AppError` → its status and code.
- `RequestValidationError` → 400 `VALIDATION_ERROR`.
- `sqlalchemy.exc.IntegrityError` on a known unique constraint (match the constraint **name** from the naming convention) → 409 `NAME_CONFLICT`; others → 500.
- `StarletteHTTPException` 404/405 → envelope with `NOT_FOUND` / `METHOD_NOT_ALLOWED`.
- Anything else → 500 `INTERNAL_ERROR` with a generic message; log the exception with request ID, user ID, method, and path.

Never return stack traces, SQL, ORA- codes, blob keys, or storage paths.

## 7. Sync vs async handlers

We use a **synchronous** SQLAlchemy `Session` (python-oracledb thin mode) for simplicity and
mature Oracle support. Therefore:

- Default: route handlers are plain `def`. FastAPI runs them in its threadpool.
- Handlers that **read a raw request body** (chunk upload) or **wait** (long-poll) are `async def`, and push any blocking work (DB, storage) to `starlette.concurrency.run_in_threadpool`. Never call blocking code directly inside `async def`.
- Long-poll (`POST /changes/wait`): `async def`; loop with `await asyncio.sleep(1)` checking the latest change IDs (via threadpool) until something is newer than the client's cursors or ~55 s pass; return `{ data: { changed: [namespace_ids] } }`. Bound the number of cursors per request.
- Downloads use `StreamingResponse` over a sync generator from the storage backend (Starlette iterates it in a thread).

## 8. Auth and authorization

- `POST /auth/login` → access token (JWT, 15 min, `sub`=user ID, `did`=device ID, `ver`=user token version) + refresh token (opaque random 256-bit, stored **hashed**, rotated on every refresh, family ID for reuse detection). See FR-AUTH-01..07.
- A `current_user` dependency validates the JWT, then loads the user and device and rejects disabled users, revoked devices, and stale token versions (bumped on password change/disable). This is a DB hit per request, which is acceptable at our scale and needed for immediate revocation.
- A `client_version` dependency reads `X-Client-Version` and raises 426 `UPGRADE_REQUIRED` below the configured minimum (FR-UPD-04).
- Authorization lives in services. One helper resolves the caller's effective role on a node (`owner` / `editor` / `viewer` / none) via namespace ownership, namespace membership (direct or via group), or a file share. Every service method that touches a node calls it — including the destination parent of a move and the original parent of a restore.
- No admin endpoints in v1 (FR-ADMIN-01): the admin CLI (`cli.py`) calls services directly, never repositories, and records audit events with actor `cli` plus the OS user. In Phase 2, admin endpoints use a `require_admin` dependency **and** the service re-checks the role.

## 9. Audit events

- `AuditService.record(actor, action, target, outcome, details)` writes an append-only row (fields in FR-AUDIT-02). `action` values come from an `AuditAction` enum in `shared/`.
- **Successful** actions: record in the same transaction as the change, so audit and data commit together.
- **Failures and denials:** the `AppError` handler records `outcome="denied"`/`"failure"` for 401/403/423 and selected 4xx, using a separate short-lived session so the request's rollback doesn't erase it.
- `details` is a small JSON object (serialized from a Pydantic model) — never file content, passwords, tokens, or full request bodies.
- No code path updates or deletes audit rows except the retention job (FR-AUDIT-03).

## 10. Security and robustness

- TLS terminates at the reverse proxy; trust `X-Forwarded-*` only from the configured proxy IPs (for the audit source IP).
- Rate limiting at the proxy; account lockout in the auth service (5 failures → 15 min).
- Hash passwords with `argon2-cffi` (`PasswordHasher` defaults, rehash when parameters change). Compare tokens with `hmac.compare_digest`.
- Enforce size limits before writing: declared file size ≤ `MAX_FILE_SIZE`, chunk size exact, quota check (with row lock on the user's usage) at upload start and again at completion.
- CORS: none needed (desktop client only). Don't enable it.
- Security headers via middleware: `Strict-Transport-Security` (if not set by the proxy), `X-Content-Type-Options: nosniff`, `Content-Disposition: attachment` with RFC 5987 filename encoding on downloads.
- Handle startup/shutdown with the lifespan context: create the engine and storage backend, dispose cleanly.

## 11. Configuration

- `core/config.py`: one `Settings(BaseSettings)` class, loaded once, injected via dependency. Fails fast on missing or invalid values.
- Every setting has a matching line in `server/.env.example` with a safe placeholder and comment.
- Limits from requirements §7.2 (max file size, default quota, chunk size, trash retention, audit retention, long-poll timeout, minimum client version) are settings, not constants.
