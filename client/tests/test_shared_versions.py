"""A new version of a file shared with the user (OQ-32) while the server scans it for malware
(FR-SAFE-01): said so, asked about again until settled, and the outcome reported."""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from dropshare_client.controllers import messages
from dropshare_client.controllers.shared_controller import SharedController
from dropshare_client.services.errors import ApiError, ScanPendingError
from dropshare_client.services.sharing import Sharing
from dropshare_shared.schemas.nodes import NodeKind, NodeOut
from dropshare_shared.schemas.sharing import SharedItemOut, ShareRole, UserHit

_VIRUS = "DropShare's virus scan found a threat in this file (Eicar), so it wasn't uploaded."


class _ScanningSharing(Sharing):
    """The server took the new version and is still scanning it; then `answers` decide."""

    def __init__(self, answers: list[str]) -> None:  # no server: only these calls are made
        self.answers = answers
        self.based_on: list[int] = []

    def shared_with_me(self) -> list[SharedItemOut]:
        return []

    def current_revision(self, node_id: uuid.UUID) -> int:
        return 3

    def upload_version(
        self,
        node_id: uuid.UUID,
        base_revision: int,
        source: Path,
        stop: threading.Event,
        progress: Callable[[int, int], None] = lambda done, total: None,
    ) -> NodeOut:
        self.based_on.append(base_revision)
        answer = self.answers.pop(0)
        if answer == "scanning":
            raise ScanPendingError(0.01)
        if answer == "malware":
            raise ApiError(422, "MALWARE_DETECTED", _VIRUS, {"detection": "Eicar"})
        now = datetime.now(UTC)
        return NodeOut(
            id=node_id,
            namespace_id=uuid.uuid4(),
            parent_id=uuid.uuid4(),
            name="plan.docx",
            kind=NodeKind.FILE,
            size_bytes=4,
            content_hash="0" * 64,
            revision=base_revision + 1,
            created_at=now,
            modified_at=now,
            modified_by_user_id=uuid.uuid4(),
        )


def _item() -> SharedItemOut:
    now = datetime.now(UTC)
    return SharedItemOut(
        node_id=uuid.uuid4(),
        namespace_id=None,
        kind="file",
        name="plan.docx",
        owner=UserHit(id=uuid.uuid4(), display_name="Alice", email="alice@example.com"),
        role=ShareRole.EDITOR,
        shared_at=now,
        size_bytes=4,
        modified_at=now,
        mount_node_id=None,
    )


@pytest.fixture
def source(tmp_path: Path) -> Path:
    path = tmp_path / "plan.docx"
    path.write_bytes(b"plan")
    return path


def test_a_version_being_scanned_is_reported_then_confirmed(qtbot: QtBot, source: Path) -> None:
    sharing = _ScanningSharing(["scanning", "scanning", "published"])
    controller = SharedController(sharing)
    said: list[str] = []
    failed: list[str] = []
    controller.succeeded.connect(said.append)
    controller.action_failed.connect(failed.append)

    controller.upload_version(_item(), source)

    qtbot.waitUntil(lambda: len(said) == 2, timeout=3000)
    assert said == [messages.NEW_VERSION_SCANNING, messages.NEW_VERSION_UPLOADED]
    assert failed == []
    assert sharing.based_on == [3, 3, 3]  # the same upload each time: nothing sent again


def test_a_version_refused_after_its_scan_is_reported(qtbot: QtBot, source: Path) -> None:
    sharing = _ScanningSharing(["scanning", "malware"])
    controller = SharedController(sharing)
    said: list[str] = []
    failed: list[str] = []
    controller.succeeded.connect(said.append)
    controller.action_failed.connect(failed.append)

    controller.upload_version(_item(), source)

    qtbot.waitUntil(lambda: bool(failed), timeout=3000)
    assert said == [messages.NEW_VERSION_SCANNING]
    assert "virus scan found a threat" in failed[0]
