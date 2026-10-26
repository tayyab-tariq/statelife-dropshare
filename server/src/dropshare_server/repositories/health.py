import logging

from sqlalchemy import literal, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def database_reachable(session: Session) -> bool:
    try:
        session.execute(select(literal(1)))
    except SQLAlchemyError:
        logger.warning("Database readiness check failed", exc_info=True)
        session.rollback()
        return False
    return True
