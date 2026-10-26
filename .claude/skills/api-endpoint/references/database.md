# Database rules (SQLAlchemy 2.0 + Alembic on Oracle 19c, portable)

Loaded by the `api-endpoint` skill and the `/migration` command.
Goal: correct on Oracle 19c today, and swappable to another database later (requirements D3, NFR-COMPAT-01).

## Contents
1. Engine and sessions
2. Model style
3. Portable types
4. Oracle 19c pitfalls
5. Uniqueness among live rows (no partial indexes)
6. Queries
7. Transactions and locking
8. Migrations (Alembic)
9. Testing

## 1. Engine and sessions

- Driver: `python-oracledb` in **thin mode** (no Oracle Instant Client needed). Build the URL from separate settings (`DB_HOST`, `DB_PORT`, `DB_SERVICE_NAME`, `DB_USER`, `DB_PASSWORD`) with `sqlalchemy.engine.URL.create("oracle+oracledb", username=..., password=..., host=..., port=..., query={"service_name": ...})` so special characters in passwords need no manual escaping. Never hard-code credentials.
- Connect to the **PDB service name**, never the CDB root. Setup and troubleshooting: `docs/oracle-setup.md`.
- Production uses two accounts: a schema owner (runs migrations) and a runtime app user with only DML grants (and only `SELECT, INSERT` on `audit_events`, FR-AUDIT-03). The app user sets `CURRENT_SCHEMA` to the owner on connect (SQLAlchemy `connect` event). Models never hard-code a schema name. Dev and test use a single account each.
- `create_engine(url, pool_pre_ping=True, pool_size=..., max_overflow=..., pool_recycle=1800)`; pool sizes from settings.
- One `sessionmaker(expire_on_commit=False)`. A request-scoped `get_db()` dependency yields a `Session`, commits on success, rolls back on exception, and always closes.
- Jobs and the CLI open their own sessions with the same factory.

## 2. Model style

- SQLAlchemy 2.0 typed declarative: `class Node(Base)` with `Mapped[...]` and `mapped_column(...)`. No legacy `Column` style.
- `Base.metadata` uses a **naming convention**, so every constraint and index has a short, deterministic name (Oracle needs explicit names to match `IntegrityError`s and to drop constraints later):

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

  Keep resulting names ≤ 128 bytes (Oracle 12.2+ limit); shorten table/column names or give an explicit `name=` if needed.
- Table names plural snake_case (`users`, `nodes`, `file_versions`). Avoid Oracle reserved words as **column** names (`size`, `level`, `comment`, `date`, `number`, `mode`, `uid`, `user`, `access`, `file`, `session`, `resource`, `group`, `order`, `rows`): quoting them makes them case-sensitive in Oracle. Use `size_bytes`, `group_id`, `sort_order`, etc.
- Primary keys: `Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)`. Append-only tables (`changes`, `audit_events`) use `BigInteger` with `Identity()` — the ID doubles as the sync cursor/ordering.
- Timestamps: `DateTime(timezone=True)`, set from `datetime.now(UTC)` in Python (not DB defaults), so all databases behave the same.
- Relationships: explicit `lazy="raise"` by default on collections to prevent accidental N+1 queries; load with `selectinload`/`joinedload` where needed.

## 3. Portable types

| Need | Use | Notes on Oracle 19c |
|---|---|---|
| ID | `Uuid` | No native UUID; SQLAlchemy stores it as a fixed-length string. Fine. |
| Short text | `String(n)` | Keep `n ≤ 1000` for anything that may contain non-ASCII (4000-byte `VARCHAR2` limit, up to 4 bytes per char). Check the rendered DDL. |
| Long text | `Text` | Becomes `CLOB`: never filter, sort, group, or index on it. |
| Flag | `Boolean` | No native boolean before 23ai; rendered as a small number. Don't compare to strings. |
| Enum | `String(32)` + Python `Enum` validated in code (or `Enum(..., native_enum=False, create_constraint=True, name=...)`) | Avoid native enum types for portability. |
| Byte size / counts | `BigInteger` | File sizes exceed 2^31. |
| Structured details | `Text` holding JSON serialized by a Pydantic model | 19c has no native `JSON` type; don't query inside it. |
| Hash | `String(64)` (hex SHA-256) | |

## 4. Oracle 19c pitfalls

- **Empty string is NULL.** `''` stored in a `VARCHAR2` becomes `NULL`. Never rely on `''` meaning anything: required strings have `min_length=1` in Pydantic and `nullable=False`; optional strings are `None`, never `''`.
- **DDL auto-commits.** Each DDL statement commits implicitly, so a migration that fails midway is **not** rolled back. Keep migrations small (one concern), and test them on a scratch schema before shared ones.
- **IN-list limit of 1000** (ORA-01795). Chunk large `in_()` lists, or use a join/temporary table.
- **No partial indexes.** See section 5.
- **No `LIMIT`.** SQLAlchemy renders `OFFSET ... FETCH FIRST` (12c+) from `.limit().offset()` automatically; always add a deterministic `order_by`.
- **Case-sensitive comparisons by default.** Store a normalized column (`username_normalized`, `name_normalized` = NFC + casefold, computed in Python) with a unique index, instead of function-based indexes.
- **Identity columns** need Oracle 12c+ (fine on 19c) and are portable via `Identity()`.
- **Large objects:** don't select `CLOB` columns in list queries.

## 5. Uniqueness among live rows (no partial indexes)

We need "name is unique within a folder among **non-trashed** nodes". Postgres would use a partial
index; Oracle can't, and a composite unique index with some NULL columns still collides in Oracle.
Portable pattern:

- Add `live_key: Mapped[str | None] = mapped_column(String(64), unique=True)`.
- Set it to `sha256(f"{namespace_id}/{parent_id}/{name_normalized}").hexdigest()` while the node is live, and `None` when trashed. A single-column unique index allows any number of NULLs on Oracle and Postgres alike.
- The repository is the only place that sets it, on every create, rename, move, trash, and restore path, and there is a test for each path.
- A unique violation on `uq_nodes_live_key` maps to 409 `NAME_CONFLICT`.

Use the same pattern for any other "unique while active" rule.

## 6. Queries

- Use the 2.0 API: `select()`, `session.scalars()`, `session.execute()`. No `session.query()`.
- Select only what's needed for lists (`load_only` or column selects); never load blobs/CLOBs or password hashes into list results.
- Raw SQL only when unavoidable, via `text()` with **bound parameters**. Never f-strings or `%` formatting in SQL.
- Bulk inserts via `session.execute(insert(Model), rows)`.
- Index foreign keys and every column used for filtering/sorting (`nodes.parent_id`, `nodes.namespace_id`, `changes.namespace_id + id`, `audit_events.occurred_at`, `audit_events.actor_user_id`).

## 7. Transactions and locking

- A service operation that changes a node writes, in **one transaction**: the node change (with `revision + 1`), the `Change` journal row, quota usage updates, and the success audit event.
- Optimistic concurrency: `UPDATE ... WHERE id = :id AND revision = :base_revision`; zero rows updated → `REVISION_CONFLICT`.
- Quota: lock the user's usage row with `with_for_update()` when reserving space at upload start and at completion.
- Jobs that process queues use `with_for_update(skip_locked=True)` so parallel runs don't collide.
- Storage first, metadata second: write the blob, then commit the DB row. If the commit fails, the orphaned blob is removed by the GC job.

## 8. Migrations (Alembic)

- All schema changes via Alembic; never hand-edit the database. Use `/migration <change>`.
- `env.py` uses `Base.metadata` with the naming convention, and `compare_type=True`.
- Review every autogenerated file (autogenerate misses renames, server defaults, and some Oracle types). Prefer explicit `op.*` calls you understand.
- Every migration has a real `downgrade()` or raises with a clear reason if irreversible.
- Data migrations: batch updates; no ORM model imports (copy minimal table definitions into the migration) so old migrations don't break when models change.
- Never edit a migration that has been committed (a hook blocks it). Create a follow-up migration.
- Never run `upgrade`/`downgrade` against a shared database without the user's approval.

## 9. Testing

- Server tests run against the dedicated `DROPSHARE_TEST` schema (`TEST_DB_*` settings), migrated to head once per session.
- Isolate tests with a transaction-per-test (connection-level transaction + nested savepoints) or table truncation; never depend on test order.
- Write specific tests for Oracle behaviours we rely on: `live_key` uniqueness, case-insensitive name collisions, empty-string handling, and IN-list chunking.
