"""One running client per user and profile (FR-CLIENT-07)."""

import uuid
from pathlib import Path

from pytestqt.qtbot import QtBot

from dropshare_client.single_instance import SingleInstance, instance_name


def test_second_launch_wakes_the_first_and_steps_aside(qtbot: QtBot, tmp_path: Path) -> None:
    name = f"dropshare-test-{uuid.uuid4().hex[:8]}"
    first = SingleInstance(name, tmp_path / "instance.lock")
    assert first.claim()
    handed_over: list[bool] = []
    second = SingleInstance(name, tmp_path / "instance.lock", lambda: handed_over.append(True))

    with qtbot.waitSignal(first.activated, timeout=3000):
        assert not second.claim()

    assert handed_over == [True]  # foreground permission given before waking (Windows)
    first.release()


def test_the_lock_decides_even_before_anyone_listens(tmp_path: Path) -> None:
    lock = tmp_path / "instance.lock"
    first = SingleInstance(f"dropshare-test-{uuid.uuid4().hex[:8]}", lock)
    second = SingleInstance(f"dropshare-test-{uuid.uuid4().hex[:8]}", lock)

    assert first.claim()
    assert not second.claim()  # different socket names, same lock: still refused
    first.release()
    assert second.claim()
    second.release()


def test_profiles_get_their_own_instance() -> None:
    assert instance_name(None) != instance_name("second")
    assert instance_name("second") == instance_name("second")
