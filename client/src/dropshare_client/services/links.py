"""Internal links (FR-SHARE-07): `statelife-dropshare://open?node=<id>`, the item's ID only.
A link never grants access; the app checks it strictly and ignores anything else."""

import uuid
from urllib.parse import parse_qs, urlsplit

SCHEME = "statelife-dropshare"
_ACTION = "open"


def link_to(node_id: uuid.UUID) -> str:
    return f"{SCHEME}://{_ACTION}?node={node_id}"


def node_of(link: str) -> uuid.UUID | None:
    """The node a link points at, or None for anything that isn't exactly such a link."""
    if len(link) > 200:
        return None
    try:
        parts = urlsplit(link.strip())
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    if (
        parts.scheme.lower() != SCHEME
        or parts.netloc.lower() != _ACTION
        or parts.path not in ("", "/")
        or parts.fragment
        or list(query) != ["node"]
        or len(query["node"]) != 1
    ):
        return None
    try:
        node_id = uuid.UUID(query["node"][0])
    except ValueError:
        return None
    return node_id if str(node_id) == query["node"][0].lower() else None
