"""How sizes, dates and similar values read in the UI (NFR-UX-03)."""

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import QDate, QLocale, QTime

from dropshare_client.controllers.messages import size_text as size_text


def modified_text(when: datetime, now: datetime | None = None) -> str:
    """ "Today 10:42", "Yesterday 18:05", or the system's short date and time, in local time."""
    local = when.astimezone()
    today = (now or datetime.now(UTC)).astimezone().date()
    locale = QLocale.system()
    clock = locale.toString(QTime(local.hour, local.minute), QLocale.FormatType.ShortFormat)
    if local.date() == today:
        return f"Today {clock}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday {clock}"
    day = QDate(local.year, local.month, local.day)
    return f"{locale.toString(day, QLocale.FormatType.ShortFormat)} {clock}"
