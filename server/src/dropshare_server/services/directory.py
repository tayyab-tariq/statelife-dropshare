"""Finding people and groups to share with (FR-SHARE-08, OQ-24): any signed-in user can search
all active users by name or email, and groups by name. Only what the share dialog shows."""

from sqlalchemy.orm import Session

from dropshare_server.core.errors import invalid
from dropshare_server.repositories import groups as group_repository
from dropshare_server.repositories import users as user_repository
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.sharing import (
    SEARCH_MIN_LENGTH,
    SEARCH_RESULTS_MAX,
    GroupHit,
    SearchQuery,
    UserHit,
)


def _like_escaped(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _query_text(query: SearchQuery) -> str:
    text = query.q.strip()
    if len(text) < SEARCH_MIN_LENGTH:
        raise invalid(
            f"Type at least {SEARCH_MIN_LENGTH} characters to search.", "string_too_short", ["q"]
        )
    return text


def search_users(session: Session, query: SearchQuery) -> list[UserHit]:
    text = _query_text(query)
    users = user_repository.search_active(
        session,
        login_prefix=_like_escaped(normalize_name(text)),
        name_prefix=_like_escaped(text.lower()),
        limit=SEARCH_RESULTS_MAX,
    )
    return [UserHit(id=u.id, display_name=u.display_name, email=u.email) for u in users]


def search_groups(session: Session, query: SearchQuery) -> list[GroupHit]:
    text = _query_text(query)
    groups = group_repository.list_with_counts(
        session, name_prefix=_like_escaped(normalize_name(text)), limit=SEARCH_RESULTS_MAX
    )
    return [GroupHit(id=g.id, name=g.name, member_count=count) for g, count in groups]
