from sqlalchemy.orm import Session

from dropshare_server.models import Namespace


def add(session: Session, namespace: Namespace) -> None:
    session.add(namespace)
