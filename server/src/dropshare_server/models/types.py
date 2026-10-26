"""Portable column types shared by the models (references/database.md §3)."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import TIMESTAMP, Dialect, Enum, SmallInteger
from sqlalchemy.types import TypeDecorator


def string_enum[E: StrEnum](enum_type: type[E]) -> "Enum":
    """Store a StrEnum by value in VARCHAR(32), validated in Python.

    No CHECK constraint: adding a member (e.g. a new audit action) must not need a migration.
    """
    return Enum(
        enum_type,
        native_enum=False,
        create_constraint=False,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware UTC datetime stored as a plain TIMESTAMP in UTC.

    `DateTime(timezone=True)` renders as Oracle DATE (no fractional seconds), and drivers differ
    in how they return TIMESTAMP WITH TIME ZONE, so we normalise to UTC ourselves.
    """

    impl = TIMESTAMP
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetimes are not allowed; use datetime.now(UTC)")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Flag(TypeDecorator[bool]):
    """Boolean stored as SMALLINT 0/1: Oracle has no native BOOLEAN before 23ai."""

    impl = SmallInteger
    cache_ok = True

    def process_bind_param(self, value: bool | None, dialect: Dialect) -> int | None:
        return None if value is None else int(value)

    def process_result_value(self, value: int | None, dialect: Dialect) -> bool | None:
        return None if value is None else bool(value)
