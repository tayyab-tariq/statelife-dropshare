"""Engine and session factory for Oracle via python-oracledb thin mode (D3)."""

from sqlalchemy import URL, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings


def database_url(settings: Settings) -> URL:
    return URL.create(
        "oracle+oracledb",
        username=settings.db_user,
        password=settings.db_password.get_secret_value(),
        host=settings.db_host,
        port=settings.db_port,
        query={"service_name": settings.db_service_name},
    )


def create_db_engine(settings: Settings) -> Engine:
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
