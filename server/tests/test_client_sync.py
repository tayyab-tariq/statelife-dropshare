"""The desktop client's sync engine against the real server and database (requirements §10).

The engine tests use a fake server; this checks the two sides agree where it matters: the
change journal and cursors, chunked uploads, verified downloads, trash, name and revision
conflicts, and the long-poll. Two "devices" share one account, as on two computers.
"""

import threading
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sync_fakes import FakeRecycleBin, InodeIdentity, NoAttributes, Recorder, plain, read_tree

from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.errors import OfflineError
from dropshare_client.services.files import RemoteFiles
from dropshare_client.services.tokens import TokenStore
from dropshare_client.store.index import Index
from dropshare_client.sync.core import CycleResult, SyncCore
from dropshare_client.sync.engine import SyncEngine, SyncProblem, SyncState
from dropshare_shared.schemas.auth import TokenPairOut


class _Credentials:
    def __init__(self) -> None:
        self.secret: str | None = None

    def get(self) -> str | None:
        return self.secret

    def set(self, secret: str) -> None:
        self.secret = secret

    def delete(self) -> None:
        self.secret = None


class Computer:
    def __init__(self, api: TestClient, user: dict[str, str], base: Path) -> None:
        tokens = TokenStore(_Credentials())
        tokens.store(
            TokenPairOut(
                access_token=user["access_token"],
                refresh_token=user["refresh_token"],
                expires_in=900,
                must_change_password=False,
            )
        )

        # Requests matching this reach the server, but their answer is lost on the way back.
        self.lose_answer: Callable[[httpx.Request], bool] = lambda _: False
        self.sent: list[httpx.Request] = []

        def forward(request: httpx.Request) -> httpx.Response:
            self.sent.append(request)
            response: httpx.Response = api.request(
                request.method, str(request.url), content=request.content, headers=request.headers
            )
            if self.lose_answer(request):
                self.lose_answer = lambda _: False
                raise httpx.ReadTimeout("answer lost", request=request)
            return response

        self.api = ApiClient(
            "http://testserver",
            tokens,
            client_version="0.1.0",
            user_agent="DropShare/0.1.0 (test)",
            transport=httpx.MockTransport(forward),
        )
        self.files = RemoteFiles(self.api)
        self.root = base / "DropShare"
        self.root.mkdir(parents=True)
        self.bin = FakeRecycleBin(base / "bin")
        self.log = Recorder()
        self.index = Index(base / "index.db")
        self.core = SyncCore(
            self.root,
            self.index,
            self.files,
            self.bin,
            InodeIdentity(),
            NoAttributes(),
            self.log,
            display_name="Alice",
            stop=threading.Event(),
            stable_after_ns=0,
        )
        self.core.connect()

    def sync(self) -> CycleResult:
        for _ in range(10):
            self.core.fetch()
            result = self.core.cycle({""})
            if result.confirm_deletes is not None:
                self.core.approve_deletes()
                continue
            if not result.again:
                return result
        raise AssertionError("sync didn't settle")

    def write(self, path: str, content: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def tree(self) -> dict[str, bytes | None]:
        return read_tree(self.root)


@pytest.fixture
def computers(
    api: TestClient, signed_in: dict[str, str], tmp_path: Path
) -> tuple[Computer, Computer]:
    return (
        Computer(api, signed_in, tmp_path / "laptop"),
        Computer(api, signed_in, tmp_path / "desktop"),
    )


def test_files_and_folders_reach_the_other_computer(computers: tuple[Computer, Computer]) -> None:
    laptop, desktop = computers
    laptop.write("Docs/plan.txt", b"a plan spread over several chunks")
    laptop.write("notes.txt", b"")
    laptop.sync()

    desktop.sync()

    expected = {
        "Docs": None,
        "Docs/plan.txt": b"a plan spread over several chunks",
        "notes.txt": b"",
    }
    assert desktop.tree() == laptop.tree() == expected


def test_edits_on_both_computers_keep_both(computers: tuple[Computer, Computer]) -> None:
    laptop, desktop = computers
    laptop.write("plan.txt", b"v1")
    laptop.sync()
    desktop.sync()

    desktop.write("plan.txt", b"desktop's edit")
    laptop.write("plan.txt", b"laptop's edit")
    desktop.sync()
    laptop.sync()  # meets REVISION_CONFLICT or the newer revision: a conflicted copy
    desktop.sync()

    expected = {"plan.txt": b"desktop's edit", "plan (conflict).txt": b"laptop's edit"}
    assert plain(laptop.tree()) == plain(desktop.tree()) == expected
    assert len(laptop.log.conflicts) == 1


def test_deletes_go_to_the_trash_and_the_recycle_bin(
    computers: tuple[Computer, Computer], api: TestClient, signed_in: dict[str, str]
) -> None:
    laptop, desktop = computers
    for name in ("a", "b", "c", "d", "e"):
        laptop.write(f"{name}.txt", name.encode())
    laptop.sync()
    desktop.sync()
    entry = laptop.index.entry("a.txt")
    assert entry is not None

    (laptop.root / "a.txt").unlink()
    laptop.sync()
    desktop.sync()

    gone = api.get(
        f"/api/v1/nodes/{entry.node_id}", headers={"Authorization": signed_in["authorization"]}
    )
    assert gone.status_code == 404  # in the trash, out of the live tree
    assert "a.txt" not in desktop.tree()
    assert desktop.bin.items() == [("a.txt", b"a")]


def test_a_folder_deleted_on_one_computer_keeps_files_added_on_the_other(
    computers: tuple[Computer, Computer],
) -> None:
    laptop, desktop = computers
    for n in range(6):
        laptop.write(f"Project/{n}.txt", b"x")
    laptop.sync()
    desktop.sync()

    for n in range(6):
        (laptop.root / "Project" / f"{n}.txt").unlink()
    (laptop.root / "Project").rmdir()
    desktop.write("Project/new.txt", b"added meanwhile")
    laptop.sync()
    desktop.sync()
    laptop.sync()

    expected = {"Project": None, "Project/new.txt": b"added meanwhile"}
    assert laptop.tree() == desktop.tree() == expected


def test_the_long_poll_wakes_for_the_other_computers_change(
    computers: tuple[Computer, Computer],
) -> None:
    laptop, desktop = computers
    laptop.sync()
    desktop.sync()
    cursors = desktop.core.cursors()

    assert desktop.files.wait(cursors) == []  # nothing new: the server's wait runs out

    laptop.write("ping.txt", b"ping")
    laptop.sync()
    assert desktop.files.wait(cursors) == list(cursors)


def test_an_upload_whose_answer_was_lost_is_not_sent_again(
    computers: tuple[Computer, Computer],
) -> None:
    """NFR-REL-04 end to end: the next fetch shows the file the upload made, with the same
    hash, so it is linked instead of uploaded twice."""
    laptop, desktop = computers
    laptop.write("report.txt", b"quarterly numbers")
    laptop.lose_answer = lambda r: r.method == "POST" and r.url.path.endswith("/complete")
    with pytest.raises(OfflineError):
        laptop.sync()

    laptop.sync()
    desktop.sync()

    starts = [r for r in laptop.sent if r.method == "POST" and r.url.path == "/api/v1/uploads"]
    assert len(starts) == 1
    assert laptop.index.entry("report.txt") is not None
    assert desktop.tree() == {"report.txt": b"quarterly numbers"}


def test_a_repeated_trash_succeeds_again(computers: tuple[Computer, Computer]) -> None:
    laptop, _ = computers
    laptop.write("old.txt", b"old")
    laptop.sync()
    entry = laptop.index.entry("old.txt")
    assert entry is not None

    laptop.files.trash(entry.node_id, entry.revision)
    laptop.files.trash(entry.node_id, entry.revision)  # e.g. after a lost answer


def test_a_download_resumes_with_a_range(
    computers: tuple[Computer, Computer], tmp_path: Path
) -> None:
    laptop, desktop = computers
    content = b"a file sent in several pieces"
    laptop.write("big.bin", content)
    laptop.sync()
    entry = laptop.index.entry("big.bin")
    assert entry is not None and entry.sha256 is not None
    part = tmp_path / "resume.part"
    part.write_bytes(content[:10])

    desktop.files.download(entry.node_id, entry.sha256, len(content), part, threading.Event())

    assert part.read_bytes() == content
    assert desktop.sent[-1].headers["Range"] == "bytes=10-"


def test_a_folder_made_on_both_computers_is_one_folder(
    computers: tuple[Computer, Computer],
) -> None:
    laptop, desktop = computers
    laptop.write("Shared/a.txt", b"from the laptop")
    desktop.write("Shared/b.txt", b"from the desktop")
    laptop.sync()

    desktop.core.cycle({""})  # without fetching first: NAME_CONFLICT, then linked
    desktop.sync()
    laptop.sync()

    expected = {
        "Shared": None,
        "Shared/a.txt": b"from the laptop",
        "Shared/b.txt": b"from the desktop",
    }
    assert laptop.tree() == desktop.tree() == expected


def test_a_folder_delete_keeps_a_file_the_other_computer_just_added(
    computers: tuple[Computer, Computer],
) -> None:
    """D2 against the real server: the delete carries the cursor and is refused."""
    laptop, desktop = computers
    for n in range(6):
        laptop.write(f"Project/{n}.txt", b"x")
    laptop.sync()
    desktop.sync()
    for n in range(6):
        (laptop.root / "Project" / f"{n}.txt").unlink()
    (laptop.root / "Project").rmdir()
    laptop.core.fetch()
    desktop.write("Project/new.txt", b"added meanwhile")
    desktop.sync()

    laptop.core.cycle({""})  # the trash meets REVISION_CONFLICT
    laptop.sync()
    desktop.sync()

    assert laptop.tree()["Project/new.txt"] == b"added meanwhile"
    assert desktop.tree()["Project/new.txt"] == b"added meanwhile"


class _States:
    def __init__(self) -> None:
        self.up_to_date = threading.Event()

    def state_changed(self, state: SyncState, problem: SyncProblem | None) -> None:
        if state == SyncState.UP_TO_DATE:
            self.up_to_date.set()

    def conflict(self, path: str, copy_path: str) -> None:
        return None

    def confirm_deletes(self, here: int, on_server: int) -> None:
        return None

    def session_expired(self) -> None:
        return None

    def progress(self, pending: int) -> None:
        return None


def test_a_saved_file_appears_on_the_other_computer_within_10_seconds(
    api: TestClient, signed_in: dict[str, str], tmp_path: Path
) -> None:
    """NFR-PERF-02 end to end with the real timings: watcher, 2 s stability (FR-SYNC-06),
    upload, journal, long-poll, download."""
    engines = []
    roots = []
    for name in ("laptop", "desktop"):
        computer = Computer(api, signed_in, tmp_path / name)
        states = _States()
        engine = SyncEngine(
            computer.root,
            computer.index,
            computer.files,
            computer.bin,
            InodeIdentity(),
            NoAttributes(),
            states,
            display_name="Alice",
        )
        engine.start()
        assert states.up_to_date.wait(30)
        engines.append(engine)
        roots.append(computer.root)
    try:
        started = time.monotonic()
        (roots[0] / "minutes.txt").write_bytes(b"agreed")
        target = roots[1] / "minutes.txt"
        while not (target.exists() and target.read_bytes() == b"agreed"):
            assert time.monotonic() - started < 10, "not there within 10 s"
            time.sleep(0.05)
    finally:
        for engine in engines:
            engine.stop()


def test_a_renamed_folder_reaches_the_other_computer_as_one_move(
    computers: tuple[Computer, Computer],
) -> None:
    """FR-SYNC-10 against the real server: same node IDs, nothing trashed, nothing re-sent."""
    laptop, desktop = computers
    laptop.write("Projects/2026/plan.txt", b"plan")
    laptop.write("Projects/notes.txt", b"notes")
    laptop.sync()
    desktop.sync()
    before = laptop.index.entry("projects/2026/plan.txt")
    assert before is not None
    laptop.sent.clear()

    (laptop.root / "Projects").rename(laptop.root / "Archive")
    laptop.sync()
    desktop.sync()

    expected = {
        "Archive": None,
        "Archive/2026": None,
        "Archive/2026/plan.txt": b"plan",
        "Archive/notes.txt": b"notes",
    }
    assert laptop.tree() == desktop.tree() == expected
    assert [r.method for r in laptop.sent if r.method not in ("GET", "POST")] == ["PATCH"]
    after = desktop.index.entry("archive/2026/plan.txt")
    assert after is not None and after.node_id == before.node_id
    assert desktop.bin.items() == []


def test_a_move_whose_answer_was_lost_is_not_sent_again(
    computers: tuple[Computer, Computer],
) -> None:
    laptop, desktop = computers
    laptop.write("a.txt", b"a")
    laptop.write("Dest/keep.txt", b"k")
    laptop.sync()
    laptop.lose_answer = lambda r: r.method == "PATCH"

    (laptop.root / "a.txt").rename(laptop.root / "Dest" / "b.txt")
    with pytest.raises(OfflineError):
        laptop.sync()
    laptop.sync()  # sees the move done on the server, and only records it
    desktop.sync()

    patches = [r for r in laptop.sent if r.method == "PATCH"]
    assert len(patches) == 1
    assert plain(desktop.tree()) == {"Dest": None, "Dest/b.txt": b"a", "Dest/keep.txt": b"k"}
