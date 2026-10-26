"""The migrated schema matches the ORM models (the `alembic check` guarantee, on the test DB)."""

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine

from dropshare_server.models import Base


def test_migrations_match_models(test_engine: Engine) -> None:
    with test_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == []
