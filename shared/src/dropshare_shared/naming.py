"""Cross-platform file-name rules (NFR-COMPAT-02), shared by client and server."""

import re
import unicodedata
from collections.abc import Collection
from enum import StrEnum

# Counted in UTF-16 code units, as NTFS does: a character outside the BMP (e.g. emoji) counts 2.
MAX_NAME_LENGTH = 255

_FORBIDDEN_CHARACTERS = frozenset('<>:"/\\|?*')
# Windows treats COM¹-COM³ and LPT¹-LPT³ as device names too.
_RESERVED_STEM = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])$", re.IGNORECASE)


class NameProblem(StrEnum):
    """Stable machine codes; user-facing text lives in NAME_PROBLEM_MESSAGES."""

    EMPTY = "EMPTY"
    TOO_LONG = "TOO_LONG"
    INVALID_UNICODE = "INVALID_UNICODE"
    FORBIDDEN_CHARACTER = "FORBIDDEN_CHARACTER"
    CONTROL_CHARACTER = "CONTROL_CHARACTER"
    TRAILING_DOT_OR_SPACE = "TRAILING_DOT_OR_SPACE"
    RESERVED = "RESERVED"


NAME_PROBLEM_MESSAGES: dict[NameProblem, str] = {
    NameProblem.EMPTY: "Name can't be empty.",
    NameProblem.TOO_LONG: f"Name is too long (the limit is {MAX_NAME_LENGTH} characters).",
    NameProblem.INVALID_UNICODE: "Name contains characters that can't be stored.",
    NameProblem.FORBIDDEN_CHARACTER: (
        "Name can't contain any of these characters: < > : \" / \\ | ? *"
    ),
    NameProblem.CONTROL_CHARACTER: "Name can't contain control characters.",
    NameProblem.TRAILING_DOT_OR_SPACE: "Name can't end with a dot or a space.",
    NameProblem.RESERVED: (
        "Name is reserved by Windows (for example CON, PRN, AUX, NUL, COM1, LPT1)."
    ),
}


class InvalidNameError(ValueError):
    def __init__(self, problem: NameProblem) -> None:
        super().__init__(NAME_PROBLEM_MESSAGES[problem])
        self.problem = problem


def is_allowed_character(char: str) -> bool:
    """Whether one character may appear in a name (NFR-COMPAT-02)."""
    return char not in _FORBIDDEN_CHARACTERS and ord(char) >= 32 and ord(char) != 127


def validate_name(name: str) -> str:
    """Return the name in NFC form, or raise InvalidNameError explaining what is wrong."""
    nfc = unicodedata.normalize("NFC", name)
    if not nfc:
        raise InvalidNameError(NameProblem.EMPTY)
    try:
        utf16_length = len(nfc.encode("utf-16-le")) // 2
    except UnicodeEncodeError:  # lone surrogates can't be stored or sent anywhere
        raise InvalidNameError(NameProblem.INVALID_UNICODE) from None
    if utf16_length > MAX_NAME_LENGTH:
        raise InvalidNameError(NameProblem.TOO_LONG)
    if any(char in _FORBIDDEN_CHARACTERS for char in nfc):
        raise InvalidNameError(NameProblem.FORBIDDEN_CHARACTER)
    if any(ord(char) < 32 or ord(char) == 127 for char in nfc):
        raise InvalidNameError(NameProblem.CONTROL_CHARACTER)
    if nfc.endswith((".", " ")):
        raise InvalidNameError(NameProblem.TRAILING_DOT_OR_SPACE)
    # "CON.txt" and "con .tar.gz" are reserved too: Windows looks at the part before the first dot.
    if _RESERVED_STEM.match(nfc.split(".", 1)[0].rstrip(" ")):
        raise InvalidNameError(NameProblem.RESERVED)
    return nfc


def normalize_name(name: str) -> str:
    """Comparison key for case-insensitive uniqueness: Unicode NFC, then casefold."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", name).casefold())


def file_extension(name: str) -> str | None:
    """The last extension of a name in the form the ban list compares (FR-SAFE-02): the dot and
    what follows the last dot, NFC and casefolded. None when there is no dot or nothing follows
    it. "invoice.pdf.EXE" gives ".exe"; ".exe" on its own is an extension too, as Windows reads it.
    """
    key = normalize_name(name)
    dot = key.rfind(".")
    if dot == -1 or dot == len(key) - 1:
        return None
    return key[dot:]


def is_banned(name: str, banned: Collection[str]) -> bool:
    """Whether a file name's last extension is in `banned`, whose entries are already in
    `normalize_extension` form."""
    extension = file_extension(name)
    return extension is not None and extension in banned


def normalize_extension(extension: str) -> str:
    """A ban-list entry such as ".EXE" in comparison form (".exe"), or ValueError if it could
    never match the last extension of a valid name."""
    if not extension.startswith(".") or len(extension) < 2:
        raise ValueError(f"{extension!r} must be a dot followed by the extension, like '.exe'")
    if "." in extension[1:]:
        raise ValueError(f"{extension!r} has more than one dot; only the last extension counts")
    if not all(is_allowed_character(char) for char in extension):
        raise ValueError(f"{extension!r} contains a character file names can't contain")
    if extension.endswith(" "):
        raise ValueError(f"{extension!r} ends with a space, which file names can't")
    return normalize_name(extension)
