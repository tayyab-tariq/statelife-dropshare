"""Every local x remote change combination for a file and for a folder (NFR-REL-01, roadmap
M5): {none, create, modify, delete} on this computer x the same on the server.

"create" on an item that was already synced means it was replaced: locally the file is
overwritten (or the folder deleted and made again); on the server the item went to the trash
and a new one took its name. After syncing, both sides must hold the same tree, and every
piece of content must still be somewhere: in the tree, the Recycle Bin or the server trash.
Renames and moves against every change are in test_moves.py; a crash at every step of every
case is in test_matrix_crashes.py.
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer, Listing, Tree, plain

BASE = b"base"
LOCAL = b"local edit"
REMOTE = b"remote edit"
A = b"a"
COPY = "doc (conflict).txt"


@dataclass(frozen=True)
class Case:
    local: str
    remote: str
    expected: Tree
    trashed: Listing = field(default_factory=list)  # the server trash afterwards
    recycled: Listing = field(default_factory=list)  # the Recycle Bin afterwards
    conflicts: int = 0

    @property
    def synced_first(self) -> bool:
        """The item existed and was synced before the changes."""
        return not {self.local, self.remote} <= {"none", "create"} or (
            self.local == self.remote == "none"
        )

    def __str__(self) -> str:
        return f"{self.local}-{self.remote}"


FILE_CASES = [
    Case("none", "none", {"doc.txt": BASE}),
    Case("none", "create", {"doc.txt": REMOTE}),
    Case("none", "modify", {"doc.txt": REMOTE}),
    Case("none", "delete", {}, trashed=[("doc.txt", BASE)], recycled=[("doc.txt", BASE)]),
    Case("create", "none", {"doc.txt": LOCAL}),
    Case("create", "create", {"doc.txt": REMOTE, COPY: LOCAL}, conflicts=1),
    Case("create", "modify", {"doc.txt": REMOTE, COPY: LOCAL}, conflicts=1),
    Case("create", "delete", {"doc.txt": LOCAL}, trashed=[("doc.txt", BASE)]),
    Case("modify", "none", {"doc.txt": LOCAL}),
    Case("modify", "create", {"doc.txt": REMOTE, COPY: LOCAL}, [("doc.txt", BASE)], conflicts=1),
    Case("modify", "modify", {"doc.txt": REMOTE, COPY: LOCAL}, conflicts=1),
    Case("modify", "delete", {"doc.txt": LOCAL}, trashed=[("doc.txt", BASE)]),
    Case("delete", "none", {}, trashed=[("doc.txt", BASE)]),
    Case("delete", "create", {"doc.txt": REMOTE}, trashed=[("doc.txt", BASE)]),
    Case("delete", "modify", {"doc.txt": REMOTE}),
    Case("delete", "delete", {}, trashed=[("doc.txt", BASE)]),
]

F: Tree = {"F": None}
F_A: Tree = {"F": None, "F/a.txt": A}
FOLDER_CASES = [
    Case("none", "none", F_A),
    Case("none", "create", F),
    Case("none", "modify", {**F_A, "F/remote.txt": REMOTE}),
    Case("none", "delete", {}, trashed=[("F", None)], recycled=[("F", None), ("F/a.txt", A)]),
    Case("create", "none", F),
    Case("create", "create", F),
    Case("create", "modify", {**F, "F/remote.txt": REMOTE}, trashed=[("a.txt", A)]),
    Case("create", "delete", {}, trashed=[("F", None)], recycled=[("F", None)]),
    Case("modify", "none", {**F_A, "F/local.txt": LOCAL}),
    Case(
        "modify",
        "create",
        {**F, "F/local.txt": LOCAL},
        trashed=[("F", None)],
        recycled=[("a.txt", A)],
    ),
    Case("modify", "modify", {**F_A, "F/local.txt": LOCAL, "F/remote.txt": REMOTE}),
    # FR-CONF-03: the folder deleted on the server comes back for the file added here.
    Case(
        "modify",
        "delete",
        {**F, "F/local.txt": LOCAL},
        trashed=[("F", None)],
        recycled=[("a.txt", A)],
    ),
    Case("delete", "none", {}, trashed=[("F", None)]),
    Case("delete", "create", F, trashed=[("F", None)]),
    # FR-CONF-03 the other way: the folder deleted here comes back for the server's new file.
    Case("delete", "modify", {**F, "F/remote.txt": REMOTE}, trashed=[("a.txt", A)]),
    Case("delete", "delete", {}, trashed=[("F", None)]),
]


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    return Device(tmp_path, server)


def check(device: Device, server: FakeFileServer, case: Case) -> None:
    assert plain(device.tree()) == case.expected
    assert plain(server.tree()) == case.expected
    assert server.trashed() == case.trashed
    assert device.bin.items() == case.recycled
    assert len(device.log.conflicts) == case.conflicts


# "full": every cycle scans the whole folder. "dirty": cycles look only at what the running
# engine would - the watcher's paths, the keys fetched changes touch, and retries.
MODES = ["full", "dirty"]


def _sync(device: Device, mode: str, touched: list[str]) -> None:
    device.sync(local=touched if mode == "dirty" else None)


def arrange_file(device: Device, server: FakeFileServer, case: Case) -> list[str]:
    """Sync the starting point, then make the case's changes; returns what the watcher saw."""
    if case.synced_first:
        device.write("doc.txt", BASE)
        device.sync()
        assert server.tree() == {"doc.txt": BASE}

    match case.local:
        case "create" | "modify":
            device.write("doc.txt", LOCAL)
        case "delete":
            device.delete("doc.txt")
    match case.remote:
        case "create":
            if case.synced_first:
                server.remove("doc.txt")
            server.put("doc.txt", REMOTE)
        case "modify":
            server.put("doc.txt", REMOTE)
        case "delete":
            server.remove("doc.txt")
    return [] if case.local == "none" else ["doc.txt"]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", FILE_CASES, ids=str)
def test_file(device: Device, server: FakeFileServer, case: Case, mode: str) -> None:
    _sync(device, mode, arrange_file(device, server, case))
    check(device, server, case)


def arrange_folder(device: Device, server: FakeFileServer, case: Case) -> list[str]:
    if case.synced_first:
        device.write("F/a.txt", A)
        device.sync()
        assert server.tree() == F_A

    match case.local:
        case "create":
            if case.synced_first:
                device.delete("F")
            device.mkdir("F")
        case "modify":
            device.write("F/local.txt", LOCAL)
        case "delete":
            device.delete("F")
    match case.remote:
        case "create":
            if case.synced_first:
                server.remove("F")
            server.put("F")
        case "modify":
            server.put("F/remote.txt", REMOTE)
        case "delete":
            server.remove("F")

    touched = {"none": [], "create": ["F"], "modify": ["F/local.txt"], "delete": ["F"]}
    return touched[case.local]


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", FOLDER_CASES, ids=str)
def test_folder(device: Device, server: FakeFileServer, case: Case, mode: str) -> None:
    _sync(device, mode, arrange_folder(device, server, case))
    check(device, server, case)


def test_same_edit_on_both_sides_is_not_a_conflict(device: Device, server: FakeFileServer) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    device.write("doc.txt", LOCAL)
    server.put("doc.txt", LOCAL)

    device.sync()

    assert device.tree() == server.tree() == {"doc.txt": LOCAL}
    assert device.log.conflicts == []
    assert server.calls.count("upload") == 1  # only the first one


def test_same_new_file_on_both_sides_is_linked(device: Device, server: FakeFileServer) -> None:
    device.write("doc.txt", LOCAL)
    server.put("doc.txt", LOCAL)

    device.sync()

    assert device.tree() == server.tree() == {"doc.txt": LOCAL}
    assert "upload" not in server.calls
    assert "download" not in server.calls


def test_a_second_sync_does_nothing(device: Device, server: FakeFileServer) -> None:
    device.write("F/a.txt", A)
    device.write("doc.txt", BASE)
    device.sync()
    server.calls.clear()

    device.sync()

    assert set(server.calls) == {"changes"}


def test_two_devices_see_each_other(tmp_path: Path, server: FakeFileServer) -> None:
    laptop = Device(tmp_path / "laptop", server)
    desktop = Device(tmp_path / "desktop", server)
    laptop.write("Reports/q1.xlsx", b"numbers")
    laptop.sync()

    desktop.sync()
    desktop.write("Reports/q1.xlsx", b"better numbers")
    desktop.sync()
    laptop.sync()

    expected = {"Reports": None, "Reports/q1.xlsx": b"better numbers"}
    assert laptop.tree() == desktop.tree() == server.tree() == expected


def test_both_devices_edit_the_same_file(tmp_path: Path, server: FakeFileServer) -> None:
    """FR-CONF-01: the server's version keeps the name; the later device's edit becomes a
    conflicted copy named after the user, on every device."""
    laptop = Device(tmp_path / "laptop", server)
    desktop = Device(tmp_path / "desktop", server)
    laptop.write("doc.txt", BASE)
    laptop.sync()
    desktop.sync()

    laptop.write("doc.txt", b"laptop")
    desktop.write("doc.txt", b"desktop")
    laptop.sync()
    desktop.sync()
    laptop.sync()

    expected = {"doc.txt": b"laptop", COPY: b"desktop"}
    assert plain(laptop.tree()) == plain(desktop.tree()) == plain(server.tree()) == expected
    (copy_name,) = [p for p in server.tree() if p != "doc.txt"]
    assert copy_name.startswith("doc (conflicted copy \N{EN DASH} Alice Example \N{EN DASH} ")


def test_a_cycle_for_a_folder_looks_inside_it(device: Device, server: FakeFileServer) -> None:
    """The engine's own download of a new folder makes watcher events for that folder; a
    cycle for just that folder must see its files, not take them for deleted (audit B1)."""
    server.put("X")
    server.put("X/a.txt", A)
    server.put("X/b.txt", BASE)
    device.sync()

    device.sync(local=["X"])
    device.sync(local=["X", "X/a.txt"])

    assert server.tree() == {"X": None, "X/a.txt": A, "X/b.txt": BASE}
    assert server.trashed() == []


def test_case_only_folder_rename_is_sent_as_a_rename(
    device: Device, server: FakeFileServer
) -> None:
    device.write("Proj/a.txt", A)
    device.sync()
    folder_id = next(n.id for n in server.nodes.values() if n.name == "Proj")
    (device.root / "Proj").rename(device.root / "PROJ")

    device.sync(local=["Proj", "PROJ"])

    assert server.trashed() == []
    assert server.tree() == {"PROJ": None, "PROJ/a.txt": A}
    assert server.nodes[folder_id].name == "PROJ"
