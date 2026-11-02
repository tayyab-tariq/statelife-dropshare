"""Storage report (FR-ADMIN-01): usage per user with trash, and what is stored, audited."""

from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from dropshare_server.core.config import Settings
from dropshare_server.models import AuditEvent
from dropshare_server.services import storage_report as storage_report_service
from dropshare_server.services.context import cli_context
from dropshare_server.services.storage_report import StorageReport
from dropshare_server.storage.factory import create_storage_backend
from dropshare_server.storage.local import LocalFilesystemBackend
from dropshare_shared.schemas.audit import AuditAction, AuditActorType, AuditOutcome

type Cli = Callable[..., tuple[int, str, str]]
type Account = dict[str, str]
type AuditEvents = Callable[..., list[AuditEvent]]


def _report(sessions: sessionmaker[Session], settings: Settings) -> StorageReport:
    stored_blobs = storage_report_service.count_blobs(create_storage_backend(settings))
    with sessions.begin() as session:
        return storage_report_service.build(session, cli_context("it-admin"), stored_blobs)


def _auth(user: Account) -> dict[str, str]:
    return {"Authorization": user["authorization"]}


def _trash(api: TestClient, user: Account, node: dict[str, object]) -> None:
    response = api.delete(
        f"/api/v1/nodes/{node['id']}", params={"if_revision": node["revision"]}, headers=_auth(user)
    )
    assert response.status_code == 204, response.text


def test_an_empty_system_reports_zeros(cli: Cli) -> None:
    code, out, err = cli("storage", "report")

    assert code == 0, err
    assert "Users: 0" in out
    assert "Used: 0.00 GB of 0.00 GB allocated" in out
    assert "File versions: 0 (0.00 GB); blobs in storage: 0" in out


def test_usage_includes_trash_and_shared_folders_count_for_their_owner(
    api: TestClient,
    sign_in: Callable[..., Account],
    root_of: Callable[[Account], str],
    upload_file: Callable[..., Response],
    session_factory: sessionmaker[Session],
    db_settings: Settings,
) -> None:
    alice, bob = sign_in("alice"), sign_in("bob")
    assert upload_file(alice, b"kept", parent_id=root_of(alice), name="kept.txt").is_success
    deleted = upload_file(alice, b"deleted!", parent_id=root_of(alice), name="old.txt")
    _trash(api, alice, deleted.json()["data"])
    folder = api.post(
        "/api/v1/nodes", json={"parent_id": root_of(alice), "name": "Team"}, headers=_auth(alice)
    ).json()["data"]
    shared_file = upload_file(alice, b"shared-trash", parent_id=folder["id"], name="s.txt")
    bob_id = api.get("/api/v1/me", headers=_auth(bob)).json()["data"]["id"]
    shared = api.post(
        f"/api/v1/nodes/{folder['id']}/members",
        json={"user_id": bob_id, "role": "editor"},
        headers=_auth(alice),
    )
    assert shared.status_code == 201, shared.text
    current = api.get(f"/api/v1/nodes/{shared_file.json()['data']['id']}", headers=_auth(alice))
    _trash(api, alice, current.json()["data"])  # from the shared folder's own namespace

    report = _report(session_factory, db_settings)

    usage = {user.username: user for user in report.users}
    assert usage["alice"].used_bytes == len(b"kept") + len(b"deleted!") + len(b"shared-trash")
    assert usage["alice"].trash_bytes == len(b"deleted!") + len(b"shared-trash")
    assert (usage["bob"].used_bytes, usage["bob"].trash_bytes) == (0, 0)
    assert report.used_bytes == usage["alice"].used_bytes
    assert report.quota_bytes == usage["alice"].quota_bytes + usage["bob"].quota_bytes
    assert (report.version_count, report.stored_blobs) == (3, 3)
    assert report.version_bytes == usage["alice"].used_bytes


def test_the_report_lists_each_user_and_is_audited(
    cli: Cli, make_user: Callable[..., tuple[str, str]], audit_events: AuditEvents
) -> None:
    make_user("alice")
    make_user("bob")

    code, out, _ = cli("storage", "report")

    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["USERNAME", "STATUS", "QUOTA_GB", "USED_GB", "USED%", "TRASH_GB"]
    assert [line.split()[0] for line in lines[1:3]] == ["alice", "bob"]
    assert lines[1].split()[4] == "0%"
    assert "Users: 2" in out
    (event,) = audit_events(AuditAction.STORAGE_REPORTED)
    assert (event.actor_type, event.actor_label, event.outcome) == (
        AuditActorType.CLI,
        "it-admin",
        AuditOutcome.SUCCESS,
    )


def test_unreadable_storage_fails_without_a_path_and_is_audited(
    cli: Cli,
    storage_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_events: AuditEvents,
) -> None:
    def unreadable(_: LocalFilesystemBackend) -> Iterator[tuple[str, datetime]]:
        raise PermissionError(13, "Permission denied", str(storage_root / "blobs"))

    monkeypatch.setattr(LocalFilesystemBackend, "blobs", unreadable)

    code, out, err = cli("storage", "report")

    assert code == 1
    assert "The file storage couldn't be read (PermissionError)" in err
    assert str(storage_root) not in err + out and "Traceback" not in err
    (event,) = audit_events(AuditAction.STORAGE_REPORTED)
    assert event.outcome is AuditOutcome.FAILURE
    assert '"code": "STORAGE_UNAVAILABLE"' in (event.details or "")
