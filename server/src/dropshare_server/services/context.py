"""Who is acting, from where: passed into every service call that writes audit events."""

import uuid
from dataclasses import dataclass, replace

from dropshare_shared.schemas.audit import AuditActorType


@dataclass(frozen=True)
class Actor:
    type: AuditActorType
    user_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    # OS user for the CLI; attempted login name for anonymous sign-in attempts.
    label: str | None = None


ANONYMOUS = Actor(AuditActorType.ANONYMOUS)


@dataclass(frozen=True)
class CallContext:
    actor: Actor
    request_id: str | None = None
    source_ip: str | None = None

    def as_actor(self, actor: Actor) -> "CallContext":
        return replace(self, actor=actor)


def cli_context(os_user: str) -> CallContext:
    return CallContext(actor=Actor(AuditActorType.CLI, label=os_user))
