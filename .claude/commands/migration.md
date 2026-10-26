---
description: Create and review an Alembic migration that is safe on Oracle 19c and portable
argument-hint: <short description of the schema change>
---

Create an Alembic migration for: $ARGUMENTS

1. Read `.claude/skills/api-endpoint/references/database.md` first.
2. Confirm the ORM model changes are already made (or make them as part of an approved plan). Then run:
   `uv run alembic -c server/alembic.ini revision --autogenerate -m "<snake_case description>"`
3. Review the generated file line by line. Autogenerate misses or mangles things on Oracle, so check:
   - Identifier names ≤ 128 bytes and explicit, stable names for every constraint and index (from the naming convention).
   - No reliance on `''` vs `NULL`, native booleans, partial indexes, or `VARCHAR2` over 4000 bytes.
   - Server defaults and `Identity` columns render correctly for Oracle.
   - Data migrations are batched and don't load whole tables into memory.
   - `downgrade()` is implemented and reverses `upgrade()` exactly, or explicitly raises with a reason if irreversible.
4. Render the SQL without applying it: `uv run alembic -c server/alembic.ini upgrade head --sql` (offline mode, if configured) and show me the DDL for the new revision.
5. Run `uv run alembic -c server/alembic.ini check` to confirm no remaining drift.

Do not run `upgrade` against any database until I approve. Report the revision ID, the DDL summary, and any risks.
