"""Engine and session factory: Oracle via python-oracledb thin mode (D3), or PostgreSQL."""

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import DatabaseSettings


def database_url(settings: DatabaseSettings) -> URL:
    password = settings.db_password.get_secret_value()
    if settings.db_dialect == "postgresql":
        return URL.create(
            "postgresql+psycopg",
            username=settings.db_user,
            password=password,
            host=settings.db_host,
            port=settings.db_port,
            database=settings.db_name,
        )
    return URL.create(
        "oracle+oracledb",
        username=settings.db_user,
        password=password,
        host=settings.db_host,
        port=settings.db_port,
        query={"service_name": settings.db_name},
    )


def create_db_engine(settings: DatabaseSettings) -> Engine:
    return create_engine(
        database_url(settings),
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_recycle=1800,
        # Keep bound values (password/token hashes, emails, names) out of error text and logs.
        hide_parameters=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
