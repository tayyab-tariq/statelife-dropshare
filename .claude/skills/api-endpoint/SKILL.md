---
name: api-endpoint
description: Build or change the FastAPI backend of this file-sync project following the routers → services → repositories layering, shared Pydantic contracts, the standard response/error envelope, server-side authorization and audit logging, SQLAlchemy 2.0 + Alembic on Oracle 19c, and the StorageBackend interface. Use this for ANY server-side work — new resources or endpoints, auth and tokens, sharing/permissions, uploads/downloads, the change journal and long-poll, trash, the admin CLI, ORM models, migrations, storage backends, background jobs, configuration — even small changes to an existing route.
---

# API endpoint

Before writing code, read `references/conventions.md` (REST design, envelope, errors, auth,
audit, async vs sync handlers). For any model, query, or migration work also read
`references/database.md` (SQLAlchemy 2.0 + Oracle 19c rules). For uploads, downloads, blobs, or
storage backends also read `references/storage.md`.

Check `docs/requirements.md` for the requirement IDs and acceptance criteria you are implementing.
If an endpoint isn't listed in requirements §9, or is blocked by an open question, ask before building it.

## Workflow for a new endpoint or resource

1. **Contract first.** Add or change Pydantic models in `shared/src/dropshare_shared/schemas/<resource>.py` (create/update inputs, list query, response) with `model_config = ConfigDict(extra="forbid")`. Add any new `ErrorCode` values. The client imports these too, so the contract is fixed before either side is built.
2. **Data layer.** ORM model in `server/.../models/`, then a migration via `/migration`. Then a repository with only the functions the service needs.
3. **Service.** Business rules, **authorization** (ownership/share role for every node touched), quota and revision checks, change-journal rows, and audit events. Raises `AppError`; never imports FastAPI.
4. **Router.** Thin: declare models, dependencies (`current_user`, `db`, pagination), call the service, return the envelope with the correct status code. Mount under `/api/v1`.
5. **Tests.** TestClient: happy path plus 400, 401, 403/404 (another user's item), 409, and quota/size errors where they apply; assert the audit event. Delegate to the `test-writer` agent if the suite is large.
6. **Verify.** `uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest server/tests`.

## Checklist before reporting done

- No SQLAlchemy outside repositories (and models/migrations); no file bytes outside storage backends
- Every request model has `extra="forbid"`; file names validated with `dropshare_shared.naming`
- Authorization checked in the service for every node, target parent, and namespace involved
- Node writes take the base revision and bump it; a `Change` row is written in the same transaction
- Audit event written for every action in FR-AUDIT-01 (success in the same transaction; failures/denials via the error path)
- Lists paginated with capped `limit`; deterministic `order_by`
- Responses never expose password hashes, token hashes, blob keys, storage paths, or internal-only fields
- New settings added to the pydantic-settings config and `server/.env.example`
- Migration reviewed for Oracle (see `references/database.md`)

If the existing project structure differs from CLAUDE.md, follow the existing code and mention the discrepancy rather than restructuring unasked.
