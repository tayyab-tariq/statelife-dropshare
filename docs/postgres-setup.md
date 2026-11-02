# PostgreSQL for local development

Temporary alternative to Oracle 19c for local dev and tests (requirements OQ-25). Oracle remains
the target for staging and production; see `docs/oracle-setup.md`.

## 1. Run PostgreSQL in Docker

```bash
docker run -d --name dropshare-pg -e POSTGRES_PASSWORD=<local password> \
  -p 5432:5432 -v dropshare-pg:/var/lib/postgresql/data postgres:16-alpine
```

The volume keeps the data when the container is recreated.

## 2. Create the databases

```bash
docker exec dropshare-pg createdb -U postgres dropshare_dev
docker exec dropshare-pg createdb -U postgres dropshare_test
```

Tests empty every table before each test, so never point `TEST_DB_*` at `dropshare_dev`.

## 3. Configure `server/.env`

```dotenv
DB_DIALECT=postgresql
DB_HOST=localhost
DB_PORT=5432
DB_NAME=dropshare_dev
DB_USER=postgres
DB_PASSWORD=<local password>

TEST_DB_DIALECT=postgresql
TEST_DB_HOST=localhost
TEST_DB_PORT=5432
TEST_DB_NAME=dropshare_test
TEST_DB_USER=postgres
TEST_DB_PASSWORD=<local password>
```

To go back to Oracle, set `DB_DIALECT=oracle` (or remove it), `DB_PORT=1521`, and `DB_NAME` to
the PDB service name. Same for `TEST_DB_*`.

## 4. Migrate

```bash
uv run alembic -c server/alembic.ini upgrade head
uv run alembic -c server/alembic.ini check
```

## Troubleshooting

- **`database "dropshare_dev" does not exist` although it does:** another PostgreSQL (for example
  Postgres.app or Homebrew) is listening on `127.0.0.1:5432` and answers before Docker. Stop it, or
  publish the container on another port (`-p 5433:5432`) and set `DB_PORT=5433`.
