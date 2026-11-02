"""NFR-SEC-04: a new password must not be on the bundled common-password list (OQ-14).

Length rules live in the shared models; this is the list check, server-side only. The list
(common_passwords.txt, see common_passwords.LICENSE) holds entries of 12-128 characters already
normalized, and is compared case-insensitively, so `Password1234` is caught like `password1234`.
"""

from functools import cache
from importlib.resources import files

from dropshare_shared.naming import normalize_name


@cache
def _common_passwords() -> frozenset[str]:
    text = files(__package__).joinpath("common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(text.splitlines())


def is_common(password: str) -> bool:
    return normalize_name(password) in _common_passwords()
