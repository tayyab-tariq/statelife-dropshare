import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Row, select
from sqlalchemy.orm import Session

from dropshare_server.models import ClientRelease
from dropshare_shared.schemas.releases import ReleaseChannel, ReleasePlatform
from dropshare_shared.version import Version

_IN_LIST_LIMIT = 1000  # Oracle ORA-01795

_NEWEST_FIRST = (
    ClientRelease.version_major.desc(),
    ClientRelease.version_minor.desc(),
    ClientRelease.version_patch.desc(),
    ClientRelease.published_at.desc(),
)


def add(session: Session, release: ClientRelease) -> None:
    session.add(release)
    session.flush()


def get(session: Session, release_id: uuid.UUID) -> ClientRelease | None:
    return session.get(ClientRelease, release_id)


def get_by_version(
    session: Session, platform: ReleasePlatform, channel: ReleaseChannel, version: str
) -> ClientRelease | None:
    return session.scalars(
        select(ClientRelease).where(
            ClientRelease.platform == platform,
            ClientRelease.channel == channel,
            ClientRelease.version == version,
        )
    ).one_or_none()


def latest(
    session: Session, platform: ReleasePlatform, channels: Sequence[ReleaseChannel]
) -> ClientRelease | None:
    """The newest release not withdrawn on any of these channels."""
    return session.scalars(
        select(ClientRelease)
        .where(
            ClientRelease.platform == platform,
            ClientRelease.channel.in_(channels),
            ClientRelease.withdrawn_at.is_(None),
        )
        .order_by(*_NEWEST_FIRST)
        .limit(1)
    ).first()


def latest_minimum(
    session: Session, platform: ReleasePlatform, channel: ReleaseChannel
) -> str | None:
    """The minimum supported version of the newest release not withdrawn on this channel."""
    return session.scalars(
        select(ClientRelease.min_supported_version)
        .where(
            ClientRelease.platform == platform,
            ClientRelease.channel == channel,
            ClientRelease.withdrawn_at.is_(None),
        )
        .order_by(*_NEWEST_FIRST)
        .limit(1)
    ).first()


def highest_version(
    session: Session, platform: ReleasePlatform, channel: ReleaseChannel
) -> Version | None:
    """The highest version ever published on this channel, withdrawn ones included."""
    row = session.execute(
        select(
            ClientRelease.version_major, ClientRelease.version_minor, ClientRelease.version_patch
        )
        .where(ClientRelease.platform == platform, ClientRelease.channel == channel)
        .order_by(*_NEWEST_FIRST)
        .limit(1)
    ).first()
    return None if row is None else (row[0], row[1], row[2])


type ListingRow = Row[ReleasePlatform, ReleaseChannel, str, str, int, datetime, datetime | None]


def list_all(session: Session) -> Sequence[ListingRow]:
    """Every release, newest first per platform and channel; without the notes (a CLOB)."""
    return session.execute(
        select(
            ClientRelease.platform,
            ClientRelease.channel,
            ClientRelease.version,
            ClientRelease.min_supported_version,
            ClientRelease.size_bytes,
            ClientRelease.published_at,
            ClientRelease.withdrawn_at,
        ).order_by(ClientRelease.platform, ClientRelease.channel, *_NEWEST_FIRST)
    ).all()


def known_blob_keys(session: Session, blob_keys: Sequence[str]) -> set[str]:
    found: set[str] = set()
    for start in range(0, len(blob_keys), _IN_LIST_LIMIT):
        found.update(
            session.scalars(
                select(ClientRelease.blob_key).where(
                    ClientRelease.blob_key.in_(blob_keys[start : start + _IN_LIST_LIMIT])
                )
            )
        )
    return found
