"""Cross-platform file-name rules (NFR-COMPAT-02), shared by client and server."""

import re
import unicodedata
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
