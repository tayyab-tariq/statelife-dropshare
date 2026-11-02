"""Rebuild the bundled common-password list (NFR-SEC-04, OQ-14) from a SecLists download.

    uv run python scripts/dev/build_common_passwords.py Pwdb_top-1000000.txt <commit sha>

Keeps only what the policy could otherwise accept (12-128 characters), normalized the way the
server compares (NFC + casefold), deduplicated and sorted. Hashcat-style `$HEX[...]` entries are
decoded; lines that aren't valid UTF-8, or hold replacement characters, are skipped.
"""

import argparse
import binascii
import re
import sys
from pathlib import Path

from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.auth import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH

_OUTPUT = Path(__file__).parents[2] / "server/src/dropshare_server/core/common_passwords.txt"
_HEX = re.compile(rb"^\$HEX\[([0-9a-fA-F]*)\]$")


def _decode(line: bytes) -> str | None:
    match = _HEX.fullmatch(line)
    try:
        return (binascii.unhexlify(match.group(1)) if match else line).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path, help="Downloaded SecLists list")
    parser.add_argument("commit", help="SecLists commit the list was downloaded at")
    args = parser.parse_args()

    kept: set[str] = set()
    for line in args.source.read_bytes().splitlines():
        password = _decode(line)
        if password is None or "�" in password:  # mangled upstream; nobody can type it
            continue
        normalized = normalize_name(password)  # casefolding can lengthen it: "ß" is "ss"
        if PASSWORD_MIN_LENGTH <= len(normalized) <= PASSWORD_MAX_LENGTH:
            kept.add(normalized)
    kept.discard("")
    _OUTPUT.write_text("".join(f"{password}\n" for password in sorted(kept)), encoding="utf-8")
    sys.stdout.write(f"Wrote {len(kept)} passwords from {args.source.name}@{args.commit}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
