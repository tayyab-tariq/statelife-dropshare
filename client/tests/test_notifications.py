"""Which notifications are shown, and when (FR-CLIENT-02, ui.md §7)."""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytestqt.qtbot import QtBot

from dropshare_client.controllers.notification_controller import Notice, NotificationController
from dropshare_client.services.config import ConfigStore, NotificationSettings
from dropshare_client.store.records import Activity, Problem
from dropshare_client.sync.status import malware_reason
from dropshare_shared.schemas.users import UserOut

_GATHER_MS = 20
_GAP_MS = 300
_GB = 1_000_000_000


def _user(used: int, quota: int = 10 * _GB) -> UserOut:
    return UserOut(
        id=uuid.UUID(int=1),
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        role="user",
        quota_bytes=quota,
        used_bytes=used,
    )


class _Account:
    """The server's GET /me: counts reads, answers with the usage set last."""

    def __init__(self) -> None:
        self.used = 0
        self.reads = 0

    def __call__(self) -> UserOut:
        self.reads += 1
        return _user(self.used)


@pytest.fixture
def config(tmp_path: Path) -> ConfigStore:
    return ConfigStore(tmp_path / "config.json")


@pytest.fixture
def account() -> _Account:
    return _Account()


@pytest.fixture
def shown(config: ConfigStore, account: _Account) -> tuple[NotificationController, list[str]]:
    controller = NotificationController(
        config, account, gather_ms=_GATHER_MS, gap_ms=_GAP_MS, quota_check_seconds=60
    )
    notes: list[str] = []
    controller.notify.connect(lambda title, text: notes.append(f"{title}: {text}"))
    return controller, notes


type Shown = tuple[NotificationController, list[str]]


def test_a_burst_is_one_notification_and_the_next_waits_its_turn(
    qtbot: QtBot, shown: Shown
) -> None:
    controller, notes = shown

    controller.new_shares(["Budget.xlsx"])
    controller.new_shares(["Plans", "Notes.txt"])
    qtbot.waitUntil(lambda: len(notes) == 1)
    assert notes == ["Shared with you: 3 items were shared with you."]

    controller.new_shares(["Late.txt"])
    qtbot.wait(_GATHER_MS * 5)
    assert len(notes) == 1  # not within the gap after the last one
    qtbot.waitUntil(lambda: len(notes) == 2, timeout=_GAP_MS * 5)
    assert notes[1] == "Shared with you: “Late.txt” was shared with you."


def test_types_are_gathered_separately(qtbot: QtBot, shown: Shown) -> None:
    controller, notes = shown

    controller.new_shares(["Plans"])
    controller.conflict('"a.txt" was changed on this computer and on the server.')

    qtbot.waitUntil(lambda: len(notes) == 2)
    assert {note.partition(":")[0] for note in notes} == {"Shared with you", "Conflicting changes"}


def test_a_type_turned_off_in_settings_is_not_shown(
    qtbot: QtBot, shown: Shown, config: ConfigStore
) -> None:
    controller, notes = shown
    config.update(notifications=NotificationSettings(conflicts=False))

    controller.conflict("one conflict")
    controller.new_shares(["Plans"])

    qtbot.waitUntil(lambda: len(notes) == 1)
    qtbot.wait(_GATHER_MS * 5)
    assert notes[0].startswith("Shared with you")


def _problem(path: str) -> Problem:
    return Problem(path.casefold(), path, "The name can't be used on Windows. Rename it.")


def test_only_items_newly_unable_to_sync_are_notified(qtbot: QtBot, shown: Shown) -> None:
    controller, notes = shown

    controller.problems_changed([_problem("Old:name.txt")])  # left by an earlier run
    controller.problems_changed([_problem("Old:name.txt"), _problem("Docs/new?.txt")])
    qtbot.waitUntil(lambda: len(notes) == 1)
    assert notes == [
        "Can't sync: “new?.txt” can't be synced. Open Can't sync in DropShare to see why and "
        "how to fix it."
    ]

    controller.problems_changed([_problem("Docs/new?.txt")])  # one fixed: nothing to say
    controller.reset()  # signed out: the next account starts from its own list
    controller.problems_changed([_problem("Docs/new?.txt"), _problem("b|c.txt")])
    qtbot.wait(_GAP_MS * 2)
    assert len(notes) == 1


@pytest.mark.parametrize(
    ("usages", "expected"),
    [
        ([8 * _GB, 8_900_000_000], []),
        ([9 * _GB, 9_500_000_000], ["Storage almost full"]),  # once per level
        ([9_200_000_000, 10 * _GB], ["Storage almost full", "Storage full"]),
        ([10 * _GB, 9_500_000_000], ["Storage full"]),
        ([9 * _GB, 5 * _GB, 9 * _GB], ["Storage almost full", "Storage almost full"]),
    ],
)
def test_quota_notifies_on_crossing_90_and_100_percent(
    qtbot: QtBot, shown: Shown, usages: list[int], expected: list[str]
) -> None:
    controller, notes = shown

    for used in usages:
        controller.account_loaded(_user(used))
        qtbot.wait(_GAP_MS + 50)

    assert [note.partition(":")[0] for note in notes] == expected


def test_quota_message_says_how_full_and_what_to_do(qtbot: QtBot, shown: Shown) -> None:
    controller, notes = shown

    controller.account_loaded(_user(9_100_000_000))

    qtbot.waitUntil(lambda: bool(notes))
    assert notes == [
        "Storage almost full: Your DropShare storage is 91% full (9.1 GB of 10.0 GB). Delete "
        "files you no longer need and empty the trash to make room."
    ]


def _uploaded(path: str) -> Activity:
    return Activity(datetime.now(UTC), "uploaded", path, "")


def test_uploads_read_the_usage_again_at_most_once_a_minute(
    qtbot: QtBot, shown: Shown, account: _Account
) -> None:
    controller, notes = shown
    account.used = 9_500_000_000

    controller.activity_added(Activity(datetime.now(UTC), "downloaded", "a.txt", ""))
    assert account.reads == 0  # only uploads use storage
    controller.activity_added(_uploaded("a.txt"))
    controller.activity_added(_uploaded("b.txt"))

    qtbot.waitUntil(lambda: bool(notes))
    assert account.reads == 1
    assert notes[0].startswith("Storage almost full")


def test_every_notification_type_has_its_switch() -> None:
    assert {notice.value for notice in Notice} == set(NotificationSettings.model_fields)


def test_a_virus_found_is_said_so(qtbot: QtBot, shown: Shown) -> None:
    # FR-SAFE-01, OQ-59 (3): still a Can't sync notice, worded for a virus.
    controller, notes = shown
    virus = Problem(
        "docs/report.docx",
        "Docs/report.docx",
        malware_reason("Win.Test.EICAR_HDB-1"),
    )

    controller.problems_changed([])
    controller.problems_changed([virus])

    qtbot.waitUntil(lambda: len(notes) == 1)
    assert notes == [
        "Virus found: A virus was found in “report.docx”, so it wasn't uploaded. Your copy "
        "hasn't been changed. Open Can't sync in DropShare for details."
    ]


def test_a_virus_among_other_items_is_named_first(qtbot: QtBot, shown: Shown) -> None:
    controller, notes = shown
    virus = Problem("bad.txt", "bad.txt", malware_reason("Eicar"))

    controller.problems_changed([])
    controller.problems_changed([virus, _problem("odd?.txt")])

    qtbot.waitUntil(lambda: len(notes) == 1)
    assert notes[0].startswith("Virus found: A virus was found in “bad.txt”")
    assert "1 more item can't be synced" in notes[0]


def test_a_path_once_refused_for_a_virus_isnt_called_one_later(qtbot: QtBot, shown: Shown) -> None:
    controller, notes = shown
    controller.problems_changed([])
    controller.problems_changed([Problem("r.pdf", "r.pdf", malware_reason("Eicar"))])
    qtbot.waitUntil(lambda: len(notes) == 1)

    controller.problems_changed([])  # cleaned up and synced
    qtbot.wait(_GAP_MS * 2)
    controller.problems_changed([_problem("r.pdf")])  # a new, unrelated problem there

    qtbot.waitUntil(lambda: len(notes) == 2, timeout=3000)
    assert notes[1].startswith("Can't sync:")
