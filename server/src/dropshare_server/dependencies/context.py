"""Builds the CallContext (actor, source IP, request ID) for each request."""

from collections.abc import Sequence
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Annotated

from fastapi import Depends, Request

from dropshare_server.core.logging import request_id_var
from dropshare_server.dependencies.resources import Resources
from dropshare_server.services.context import ANONYMOUS, Actor, CallContext


def client_ip(
    peer: str | None,
    forwarded_for: str | None,
    trusted_proxies: Sequence[IPv4Address | IPv6Address],
) -> str | None:
    """The caller's IP. X-Forwarded-For is only believed when the direct peer is a trusted proxy;
    then the right-most address that isn't itself a trusted proxy is the client."""
    if peer is None or not forwarded_for or not _is_trusted(peer, trusted_proxies):
        return peer
    for candidate in reversed([part.strip() for part in forwarded_for.split(",")]):
        try:
            address = ip_address(candidate)
        except ValueError:
            return peer  # malformed header: fall back to what we can verify
        if address not in trusted_proxies:
            return str(address)
    return peer


def _is_trusted(peer: str, trusted_proxies: Sequence[IPv4Address | IPv6Address]) -> bool:
    try:
        return ip_address(peer) in trusted_proxies
    except ValueError:
        return False


def request_actor(request: Request) -> Actor:
    """The actor that `current_user` identified for this request, or anonymous."""
    actor = getattr(request.state, "actor", None)
    return actor if isinstance(actor, Actor) else ANONYMOUS


def build_call_context(request: Request, resources: Resources) -> CallContext:
    return CallContext(
        actor=request_actor(request),
        request_id=request_id_var.get(),
        source_ip=client_ip(
            request.client.host if request.client else None,
            request.headers.get("X-Forwarded-For"),
            resources.settings.trusted_proxies,
        ),
    )


Context = Annotated[CallContext, Depends(build_call_context)]
