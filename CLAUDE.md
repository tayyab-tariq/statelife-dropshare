# CLAUDE.md

Project memory for Claude Code for **StateLife DropShare**, a self-hosted Dropbox-style sync and sharing app.
These rules apply to every task in this repo.
Where a rule conflicts with an explicit instruction from me in chat, my instruction wins.

This file holds only what matters on every task. Detailed conventions live in skills
(`.claude/skills/`) and load on demand, which keeps context small and focused.

---

## 0. Source of truth

**`docs/requirements.md` (repo root) defines what we are building.** Before planning any task:

- Find the requirement IDs it implements (`FR-SYNC-03`, `NFR-SEC-02`, ...) and cite them in the plan and commit body.
- If a task isn't covered by a requirement, or contradicts one, stop and say so. Don't extend scope silently.
- Never build anything tagged `P2` or `Later`, or anything blocked by an open question (§15), unless I say so.
- Decisions in requirements §2 are fixed. Proposing to change one is fine; changing one silently is not.

## 1. Stack

- **Language:** Python 3.12 everywhere. uv workspace with three packages.
- **Desktop client (`client/`):** PySide6 (Qt 6), httpx (HTTP), watchdog (file watching), keyring (Windows Credential Manager), SQLAlchemy 2.0 + SQLite (local sync index).
- **Backend (`server/`):** FastAPI, uvicorn, SQLAlchemy 2.0 ORM, Alembic, python-oracledb, pydantic-settings, argon2-cffi, PyJWT.
- **Shared (`shared/`):** Pydantic v2 request/response models, enums, error codes, name-validation rules. Imported by client and server.
- **Database:** Oracle 19c now, swappable later. Only through SQLAlchemy (see `api-endpoint` skill → `references/database.md`). Connection and schema setup: `docs/oracle-setup.md`.
- **File storage:** `StorageBackend` interface. Local disk in dev; production backend decided at deployment (requirements OQ-2).
- **Platforms:** development happens on **macOS**. The server deploys to **Linux** (NFR-COMPAT-06). The desktop client targets **Windows**: it runs on the Mac via a development-grade `platform/macos/` layer, and Windows behaviour is verified on a separate Windows machine (NFR-COMPAT-07).
- **Tooling:** ruff (lint + format), mypy (`--strict`), pytest, pytest-qt. Packaging: PyInstaller (one-folder), Inno Setup, Authenticode signing.

The packages above are the **approved baseline**. Anything else needs my approval (§4.4).

## 2. Commands

```bash
uv sync                                   # install/refresh the whole workspace
uv run ruff check .                       # lint
uv run ruff format .                      # format
uv run mypy .                             # type check (strict)
uv run pytest                             # all tests
uv run pytest server/tests                # server tests (needs Oracle test schema)
uv run pytest client/tests                # client tests (pytest-qt, no server needed)

# Server
uv run --package dropshare-server uvicorn dropshare_server.main:app --reload
uv run --package dropshare-server dropshare-server --help         # admin CLI: users, groups, quotas, audit export, releases (FR-ADMIN-01)
uv run --package dropshare-server dropshare-server jobs run <job> # scheduled jobs

# Migrations (run from repo root)
uv run alembic -c server/alembic.ini revision --autogenerate -m "<change>"
uv run alembic -c server/alembic.ini upgrade head
uv run alembic -c server/alembic.ini check      # model/migration drift

# Client
uv run --package dropshare-client python -m dropshare_client

# Packaging (Windows machine only: PyInstaller/Inno Setup can't build Windows apps on macOS)
pwsh scripts/build_client.ps1             # freeze → sign → installer → sign

# On the Windows test machine
uv run pytest -m windows_only client/tests     # Windows-specific client tests
```

Database settings come from `server/.env` (`DB_*` for dev, `TEST_DB_*` for tests; see `server/.env.example`). Server tests apply migrations to the `DROPSHARE_TEST` schema first, so it must be reachable.
The project isn't scaffolded yet: the first task creates these. Update this section if the actual commands differ.

## 3. Repository structure

```
docs/requirements.md            # source of truth (see §0)
/pyproject.toml             # uv workspace root; ruff, mypy, pytest config
/shared/src/dropshare_shared/
  schemas/                  # Pydantic models: one module per resource (auth, nodes, uploads, sync, ...)
  errors.py                 # ErrorCode enum (stable SCREAMING_SNAKE_CASE)
  naming.py                 # file-name validation + normalization (NFC + casefold)
/server/
  src/dropshare_server/
    main.py                 # app factory, middleware, exception handlers
    api/v1/                 # routers only (one module per resource)
    dependencies/           # auth, db session, pagination, client-version check
    services/               # business logic + authorization; no FastAPI imports
    repositories/           # the ONLY layer that uses SQLAlchemy sessions/queries
    models/                 # SQLAlchemy ORM models
    storage/                # StorageBackend protocol + local / s3 implementations
    core/                   # config (pydantic-settings), AppError, security, logging
    jobs/                   # scheduled jobs (trash purge, GC, upload cleanup, audit retention)
    cli.py                  # admin CLI (v1 has no admin UI or admin API)
  migrations/               # Alembic
  tests/
/client/
  src/dropshare_client/
    __main__.py / app.py    # QApplication, single-instance guard, wiring
    ui/                     # windows, dialogs, widgets, Qt item models (views only)
    controllers/            # QObjects bridging UI ↔ services; run work off the UI thread
    services/               # api client, auth/session, transfers, updater
    sync/                   # engine, watcher, scanner, reconciler, conflicts, journal
    store/                  # local SQLite index (SQLAlchemy)
    platform/               # OS layer: base protocol, windows/ (shipped), macos/ (dev-only)
    resources/              # icons, .qrc
  tests/
/installer/                 # Inno Setup script, signing config (no secrets)
/scripts/
/docs/                      # oracle-setup.md, ADRs, runbooks, session audits
```

## 4. How to work with me

1. **Plan first.** For any non-trivial task, propose a short plan (requirement IDs, data model, endpoints, UI/threads, steps) and wait for my approval before writing code.
2. **Small, reviewable steps.** One concern per change. Don't touch unrelated files.
3. **Explain non-obvious decisions** in 1–2 sentences so I can discuss them. Flag tradeoffs and anything you're unsure about.
4. **Never add a dependency without asking.** State why it's needed and the alternative (stdlib, Qt, or an approved package). Don't run `uv add` without approval.
5. **Don't invent requirements or make assumptions.** If something is ambiguous, ask. If I tell you to proceed anyway, state the assumption explicitly in the plan and in requirements §15.
6. **Verify before declaring done:** ruff, mypy, and relevant tests.
7. Prefer editing existing code over creating parallel versions. No dead code, no commented-out blocks.

## 5. Python conventions

- Full type hints; `mypy --strict` passes. No `Any` (use `object`/`TypeVar`/`Protocol` and narrow). No `# type: ignore` without a code and reason.
- Pydantic models in `shared/` are the contract. Don't duplicate them as dataclasses or dicts on either side.
- `pathlib.Path` for paths, `logging` (never `print`), timezone-aware UTC datetimes only.
- No bare `except:` or `except Exception: pass`. Catch narrowly; log and re-raise or translate.
- Explicit imports, no wildcard imports. Module-private helpers start with `_`.
- Prefer `Protocol` for interfaces (storage backend, platform layer, API client in tests).

## 6. Always-on rules (details live in skills)

**Backend** (full spec: `api-endpoint` skill)

- Base path `/api/v1`, plural nouns, `PATCH` for partial updates.
- Layering: routers → services → repositories. Only repositories touch SQLAlchemy; only storage backends touch file bytes.
- Every body/params/query is a Pydantic model from `shared/` with `extra="forbid"`. Validation errors → `400 VALIDATION_ERROR`.
- No admin endpoints in v1: administration goes through the CLI, which calls the same services (never repositories directly) and audits as actor `cli`.
- Server code is OS-neutral (Windows dev, Linux prod): `pathlib`, no hard-coded separators, no POSIX- or Windows-only calls outside an isolated module.
- Responses are always `{ data }`, `{ data, meta }`, or `{ error: { code, message, details } }`.
- Raise `AppError`; central exception handlers format every error. No stack traces, SQL, or storage paths to clients.
- Authorization (ownership, share role) is checked in services on every operation. Every security-relevant action writes an audit event (FR-AUDIT-01).
- Writes to nodes require the base revision; stale writes → `409 REVISION_CONFLICT`.

**Database** (full spec: `api-endpoint` → `references/database.md`)

- ORM only; portable types; schema changes only via Alembic; never edit an applied migration.
- Remember Oracle 19c quirks: `''` is `NULL`, no native boolean, 4000-byte `VARCHAR2`, no partial indexes.

**Desktop client** (full spec: `desktop-feature` skill)

- Never block the UI thread: network, disk scans, hashing, and DB work run in workers; results come back via signals.
- UI talks only to controllers; controllers talk to services. No `httpx` or index access from `ui/`.
- Every data-driven view handles loading, error (with retry), empty, and populated states.
- OS-specific code only in `platform/`.

**Sync** (full spec: `sync-engine` skill)

- Never lose data: conflicts create copies, remote deletes go to the Recycle Bin, index updates only after the operation succeeds.
- Downloads go to a temp file, are hash-verified, then `os.replace`d into place.

**Security:** HTTPS only; Argon2id; refresh tokens only in Credential Manager on the client and only hashed on the server; secrets never in code, logs, or audit details.

**Windows verification:** you work on macOS and can't run Windows-only code. For any change to `client/.../platform/windows/`, Windows file-system handling in `sync/`, the link scheme, installer, or updater: mark Windows-specific tests `@pytest.mark.windows_only` (skipped elsewhere) and end the task with a short **Windows check** list I can run on the Windows machine. Don't claim Windows behaviour works without that.

**Testing:** behavior over implementation. Server: TestClient tests per endpoint (happy path + main errors + authorization). Sync: table-driven tests over every local/remote change combination. UI: pytest-qt, query widgets by accessible name/role.

## 7. Skills, agents, and commands

| Type    | Name                    | Use it for                                                                                  |
| ------- | ----------------------- | ------------------------------------------------------------------------------------------- |
| Skill   | `api-endpoint`          | Any server work: endpoints, services, repositories, models, migrations, storage, auth, jobs |
| Skill   | `desktop-feature`       | Any client UI work: windows, dialogs, widgets, controllers, threads, tray, settings         |
| Skill   | `sync-engine`           | Sync, transfers, local index, conflicts, watcher, file handling on disk                     |
| Skill   | `session-audit`         | Reviewing everything changed this session before wrapping up                                |
| Agent   | `code-reviewer`         | Correctness, conventions, maintainability review of a diff (read-only)                      |
| Agent   | `security-reviewer`     | Auth, authorization, input handling, secrets, audit coverage (read-only)                    |
| Agent   | `api-contract-reviewer` | Client ↔ server contract via `shared/` models (read-only)                                   |
| Agent   | `sync-reviewer`         | Data-loss, race, and crash-safety review of sync/transfer code (read-only)                  |
| Agent   | `ui-reviewer`           | UI thread safety, UI states, accessibility, HiDPI (read-only)                               |
| Agent   | `test-writer`           | Writing focused tests for new or changed behavior                                           |
| Command | `/kickoff <brief>`      | Turn a new feature brief into requirement entries + clarifying questions                    |
| Command | `/plan <task>`          | Plan-first workflow tied to requirement IDs; waits for approval                             |
| Command | `/migration <change>`   | Create and review an Alembic migration for Oracle + portability                             |
| Command | `/audit`                | Runs the `session-audit` skill                                                              |
| Command | `/commit`               | Conventional commit of reviewed changes                                                     |

## 8. Session workflow

1. **Start:** a SessionStart hook snapshots the repo state so the audit can diff exactly this session's work.
2. **Plan:** `/plan` for anything non-trivial. No code before I approve.
3. **Build:** small steps; the matching skill loads. Python files are auto-formatted after each edit (ruff), and edits to already-committed migrations are blocked.
4. **Verify:** ruff, mypy, relevant tests.
5. **Audit:** `/audit` before ending the session or committing. Fix blockers, then `/commit`.

Hooks are Python (stdlib only) and run via `uv run --no-project python`, because macOS has no `python` command by default and `python3` on Windows can be a Store stub.

## 9. Git

- Conventional commits: `feat:`, `fix:`, `refactor:`, `test:`, `chore:`, `docs:`, `build:`. Reference requirement IDs in the body.
- Code moves between the Mac and the Windows machine via git. `.gitattributes` keeps LF in the repo (`* text=auto eol=lf`), with CRLF only for `*.bat`/`*.cmd`.
- One logical change per commit. Never commit `.env`, secrets, certificates/keys (`*.pfx`, `*.p12`, `*.key`), build output (`dist/`, `build/`), `.venv/`, or `.claude/.session/`.

## 10. Definition of done

Before saying a task is complete, confirm:

- [ ] Linked requirement IDs and their acceptance criteria are met (or the gap is stated)
- [ ] `ruff check`, `ruff format --check`, and `mypy` pass
- [ ] Relevant tests written and passing
- [ ] Server: inputs validated by `shared/` models; errors use the standard shape; correct status codes; authorization checked in the service; audit event written where required
- [ ] Database: changes via an Alembic migration that runs on Oracle 19c; no Oracle-only SQL outside migrations
- [ ] Client: UI thread never blocked; loading/error/empty/populated states handled; keyboard-accessible with accessible names
- [ ] Windows-specific changes: `windows_only` tests written and a Windows check list provided (results confirmed by me, or stated as pending)
- [ ] Sync/transfers: no path loses data on conflict, crash, or retry
- [ ] No secrets in code or logs; no new dependencies without approval
- [ ] Brief summary of what changed, tradeoffs, and follow-ups
