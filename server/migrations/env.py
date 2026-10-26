"""Alembic environment.

Uses the server settings for the database URL. Tests pass an open connection through
`config.attributes["connection"]` to migrate the test schema instead.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from dropshare_server.core.config import Settings
from dropshare_server.core.db import create_db_engine, database_url
from dropshare_server.models import Base

config = context.config
if config.config_file_name is not None and not config.attributes.get("connection"):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(Settings()),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if isinstance(connection, Connection):
        _run(connection)
        return
    engine = create_db_engine(Settings())
    try:
        with engine.connect() as connection:
            _run(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
