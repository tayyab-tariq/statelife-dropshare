"""The user's signed-in devices: list and revoke (FR-AUTH-07)."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from dropshare_server.core.config import Settings
from dropshare_server.repositories import devices as device_repository
from dropshare_server.services import access
from dropshare_server.services import audit as audit_service
from dropshare_server.services import sessions as session_service
from dropshare_server.services.audit import AuditTarget
from dropshare_server.services.auth import CurrentSession
from dropshare_server.services.context import CallContext
from dropshare_shared.schemas.audit import AuditAction
from dropshare_shared.schemas.devices import DeviceOut

# More than anyone signs in from; keeps the unpaginated list bounded.
_DEVICES_LISTED = 200


def list_devices(session: Session, settings: Settings, current: CurrentSession) -> list[DeviceOut]:
    """Devices that can still sync. One unseen for longer than the refresh-token inactivity
    limit can't refresh its token any more, so it isn't listed (FR-AUTH-03)."""
    seen_since = datetime.now(UTC) - timedelta(days=settings.refresh_token_inactivity_days)
    devices = device_repository.list_active(session, current.user.id, seen_since, _DEVICES_LISTED)
    return [
        DeviceOut(
            id=device.id,
            name=device.name,
            os_name=device.os_name,
            client_version=device.client_version,
            created_at=device.created_at,
            last_seen_at=device.last_seen_at,
            is_current=device.id == current.device.id,
        )
        for device in devices
    ]


def revoke_device(
    session: Session, ctx: CallContext, current: CurrentSession, device_id: uuid.UUID
) -> None:
    """End a device's session: its refresh token is revoked and its next request gets 401
    (`auth.authenticate` checks the device on every request). Another user's device, an
    unknown one and one already revoked are all 404."""
    device = device_repository.get(session, device_id)
    if device is None or device.revoked_at is not None:
        raise access.not_found("Device")
    if device.user_id != current.user.id:
        # The same 404, so nothing is revealed; audited as a denial (FR-AUDIT-01).
        raise access.not_found("Device", denied=True, target=("device", str(device_id)))
    session_service.end_device_session(session, device.id, datetime.now(UTC))
    audit_service.record(
        session,
        ctx,
        AuditAction.AUTH_DEVICE_REVOKED,
        target=AuditTarget(type="device", id=str(device.id), name=device.name),
        details={"current_device": device.id == current.device.id},
    )
