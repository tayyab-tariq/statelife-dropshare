"""Streaming file content over HTTP: one byte range, download headers (FR-XFER-02)."""

import re
from urllib.parse import quote

from fastapi.responses import StreamingResponse

from dropshare_server.services.content import Content

# At most 19 digits (fits in 64 bits): longer values would make int() fail (audit S10).
_SINGLE_RANGE = re.compile(r"^bytes=(\d{0,19})-(\d{0,19})$")


def content_response(content: Content) -> StreamingResponse:
    headers = {
        "Content-Length": str(content.end - content.start + 1),
        "Accept-Ranges": "bytes",
        "ETag": f'"{content.sha256}"',
        "Content-Disposition": _attachment(content.name),
        "X-Content-Type-Options": "nosniff",
    }
    if content.partial:
        headers["Content-Range"] = f"bytes {content.start}-{content.end}/{content.size_bytes}"
    return StreamingResponse(
        content.chunks,
        status_code=206 if content.partial else 200,
        media_type="application/octet-stream",
        headers=headers,
    )


def parse_range(header: str | None) -> tuple[int | None, int | None] | None:
    """One `bytes=first-last`, `bytes=first-` or `bytes=-suffix` range. Anything else (several
    ranges, other units, first > last) is ignored and the whole file is sent (RFC 9110 §14.2)."""
    match = _SINGLE_RANGE.match((header or "").strip())
    if match is None or match.group(1) == match.group(2) == "":
        return None
    first = int(match.group(1)) if match.group(1) else None
    last = int(match.group(2)) if match.group(2) else None
    if first is not None and last is not None and first > last:
        return None
    return first, last


def _attachment(name: str) -> str:
    """RFC 6266 / 5987: an ASCII fallback plus the exact UTF-8 name."""
    fallback = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\' else "_" for c in name
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
