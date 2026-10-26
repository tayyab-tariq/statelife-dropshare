"""Client version format, shared so client and server agree (FR-UPD-04, FR-UPD-05).

Versions are MAJOR.MINOR.PATCH with no pre-release suffix: beta builds use the beta channel,
not a "-beta" version string.
"""

import re

CLIENT_VERSION_HEADER = "X-Client-Version"

_VERSION = re.compile(r"^(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})\.(0|[1-9]\d{0,4})$")

type Version = tuple[int, int, int]


def parse_version(text: str) -> Version:
    """Parse "1.2.3" for numeric comparison; raise ValueError for anything else."""
    match = _VERSION.fullmatch(text.strip())
    if match is None:
        raise ValueError(f"Not a version like 1.2.3: {text!r}")
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch
