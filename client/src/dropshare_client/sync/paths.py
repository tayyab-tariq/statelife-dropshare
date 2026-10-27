"""Relative paths inside the sync folder, ignored names and conflict-copy names.

A path is relative to the sync root with "/" separators ("Reports/2026/q1.xlsx"); "" is the
root. Its key is the comparison form (NFC + casefold per segment), because NTFS treats names
that differ only in case as the same file (windows-fs.md).
"""

import fnmatch
import unicodedata
from collections.abc import Callable, Iterable
from datetime import datetime

# Partial downloads, kept-aside items and the owner marker; never synced (FR-SYNC-07).
from dropshare_client.services.sync_folder import TEMP_DIR as TEMP_DIR
from dropshare_shared.naming import (
    InvalidNameError,
    is_allowed_character,
    normalize_name,
    validate_name,
)

# FR-SYNC-07, compared case-insensitively.
_IGNORED_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store", TEMP_DIR})
_IGNORED_PATTERNS = ("~$*", "*.tmp", ".~lock.*")
_CONFLICT_MARK = "conflicted copy"
_DASH = "\N{EN DASH}"  # en dash, as in FR-CONF-01
# Kept short enough that the conflicted-copy label and extension always fit in 255.
_MAX_WHO = 64
_MAX_EXTENSION = 32
_MAX_NUMBER = 10_000


def key_of(path: str) -> str:
    return "/".join(normalize_name(part) for part in path.split("/")) if path else ""


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


def name_of(path: str) -> str:
    return path.rpartition("/")[2]


def join(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def depth(path: str) -> int:
    return path.count("/") + 1 if path else 0


def is_within(key: str, ancestor: str) -> bool:
    """`key` is `ancestor` or below it."""
    return not ancestor or key == ancestor or key.startswith(f"{ancestor}/")


def ancestors(key: str) -> list[str]:
    """Every key above `key`, nearest first, ending with the root ""."""
    result = []
    while key:
        key = parent_of(key)
        result.append(key)
    return result


def outermost(keys: Iterable[str]) -> set[str]:
    """The keys that aren't below another one of them."""
    result: set[str] = set()
    for key in sorted(set(keys), key=lambda k: (depth(k), k)):
        if not any(is_within(key, kept) for kept in result):
            result.add(key)
    return result


def is_ignored(name: str) -> bool:
    folded = unicodedata.normalize("NFC", name).casefold()
    return folded in _IGNORED_NAMES or any(
        fnmatch.fnmatchcase(folded, pattern) for pattern in _IGNORED_PATTERNS
    )


def name_problem(name: str) -> str | None:
    """Why the server would refuse this name, or None (NFR-COMPAT-02)."""
    try:
        validate_name(name)
    except InvalidNameError as exc:
        return str(exc)
    return None


def conflict_name(
    name: str, display_name: str, when: datetime, taken: Callable[[str], bool]
) -> str:
    """`Name (conflicted copy - Display Name - YYYY-MM-DD HH-MM).ext` with en dashes (FR-CONF-01),
    plus ` (2)`, ` (3)`... when that name is already used. `when` is shown in local time."""
    stem, dot, extension = name.rpartition(".")
    if not stem or len(extension) > _MAX_EXTENSION:  # no extension, or ".profile", or prose
        stem, dot, extension = name, "", ""
    who = "".join(c if is_allowed_character(c) else "_" for c in display_name)[:_MAX_WHO]
    stamp = when.astimezone().strftime("%Y-%m-%d %H-%M")
    label = f"{_CONFLICT_MARK} {_DASH} {who.strip() or 'unknown'} {_DASH} {stamp}"
    for number in range(1, _MAX_NUMBER):
        suffix = f" ({label})" if number == 1 else f" ({label}) ({number})"
        tail = f"{suffix}{dot}{extension}"
        candidate = f"{_fit(stem, tail)}{tail}"
        if name_problem(candidate) is None and not taken(candidate):
            return candidate
    raise ValueError(f"No free conflicted-copy name for {name!r}")


def _fit(stem: str, tail: str) -> str:
    """Shorten `stem` so `stem + tail` stays within the 255-character name limit."""
    room = 255 - len(tail.encode("utf-16-le")) // 2
    while len(stem.encode("utf-16-le")) // 2 > room:
        stem = stem[:-1]
    return stem.rstrip(" .") or "file"
