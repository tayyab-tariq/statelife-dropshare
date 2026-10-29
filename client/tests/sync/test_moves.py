"""Renames and moves on either side against every other change (FR-SYNC-10, FR-CONF-03,
NFR-REL-01, roadmap M7).

The item keeps its server node ID whenever it survives: a rename or move is one PATCH, never
trash + upload. If both sides moved it differently, the server's location wins (OQ-27). A
rename against a delete keeps the item, as an edit against a delete does (FR-CONF-02).
"""

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sync_fakes import Crash, Device, FakeFileServer, Listing, Tree, plain

from dropshare_client.services.errors import OfflineError

BASE = b"base"
LOCAL = b"local edit"
REMOTE = b"remote edit"
A = b"a"
D: Tree = {"Dest": None}


@dataclass(frozen=True)
class Case:
    local: str
    remote: str
    expected: Tree
    trashed: Listing = field(default_factory=list)
    same_node: bool = True  # the surviving item is the node that was synced first

    def __str__(self) -> str:
        return f"{self.local}-{self.remote}"


FILE_CASES = [
    Case("rename", "none", {**D, "here.txt": BASE}),
    Case("move", "none", {**D, "Dest/doc.txt": BASE}),
    Case("none", "rename", {**D, "there.txt": BASE}),
    Case("none", "move", {**D, "Dest/doc.txt": BASE}),
    Case("rename", "rename", {**D, "there.txt": BASE}),
    Case("rename", "move", {**D, "Dest/doc.txt": BASE}),
    Case("move", "rename", {**D, "there.txt": BASE}),
    Case("move", "move", {**D, "Dest/doc.txt": BASE}),
    Case("rename", "modify", {**D, "here.txt": REMOTE}),
    Case("move", "modify", {**D, "Dest/doc.txt": REMOTE}),
    Case("modify", "rename", {**D, "there.txt": LOCAL}),
    Case("modify", "move", {**D, "Dest/doc.txt": LOCAL}),
    Case("rename", "delete", {**D, "here.txt": BASE}, [("doc.txt", BASE)], same_node=False),
    Case("move", "delete", {**D, "Dest/doc.txt": BASE}, [("doc.txt", BASE)], same_node=False),
    Case("delete", "rename", {**D, "there.txt": BASE}),
    Case("delete", "move", {**D, "Dest/doc.txt": BASE}),
]

FOLDER_CASES = [
    Case("rename", "none", {**D, "Here": None, "Here/a.txt": A}),
    Case("move", "none", {**D, "Dest/F": None, "Dest/F/a.txt": A}),
    Case("none", "rename", {**D, "There": None, "There/a.txt": A}),
    Case("none", "move", {**D, "Dest/F": None, "Dest/F/a.txt": A}),
    Case("rename", "rename", {**D, "There": None, "There/a.txt": A}),
    Case("rename", "move", {**D, "Dest/F": None, "Dest/F/a.txt": A}),
    Case("move", "rename", {**D, "There": None, "There/a.txt": A}),
    Case("move", "move", {**D, "Dest/F": None, "Dest/F/a.txt": A}),
    Case("rename", "modify", {**D, "Here": None, "Here/a.txt": A, "Here/remote.txt": REMOTE}),
    Case(
        "move",
        "modify",
        {**D, "Dest/F": None, "Dest/F/a.txt": A, "Dest/F/remote.txt": REMOTE},
    ),
    Case("modify", "rename", {**D, "There": None, "There/a.txt": A, "There/local.txt": LOCAL}),
    Case(
        "modify",
        "move",
        {**D, "Dest/F": None, "Dest/F/a.txt": A, "Dest/F/local.txt": LOCAL},
    ),
    Case(
        "rename",
        "delete",
        {**D, "Here": None, "Here/a.txt": A},
        [("F", None)],
        same_node=False,
    ),
    Case("delete", "rename", {**D, "There": None, "There/a.txt": A}),
    Case("delete", "move", {**D, "Dest/F": None, "Dest/F/a.txt": A}),
]

# Where each action leaves the item, for the file and the folder.
_FILE_PATHS = {"rename": ("here.txt", "there.txt"), "move": ("Dest/doc.txt", "Dest/doc.txt")}
_FOLDER_PATHS = {"rename": ("Here", "There"), "move": ("Dest/F", "Dest/F")}
MODES = ["full", "dirty"]


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


@pytest.fixture
def device(tmp_path: Path, server: FakeFileServer) -> Device:
    return Device(tmp_path, server)


def _rename(device: Device, old: str, new: str) -> None:
    (device.root / old).rename(device.root / new)


def _node_id(server: FakeFileServer, path: str) -> object:
    return next(n.id for n in server.nodes.values() if server.path_of(n) == path and not n.trashed)


def _check(device: Device, server: FakeFileServer, case: Case) -> None:
    assert plain(device.tree()) == case.expected
    assert plain(server.tree()) == case.expected
    assert server.trashed() == case.trashed
    assert device.bin.items() == []
    assert device.log.conflicts == []


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", FILE_CASES, ids=str)
def test_file(device: Device, server: FakeFileServer, case: Case, mode: str) -> None:
    device.mkdir("Dest")
    device.write("doc.txt", BASE)
    device.sync()
    original = _node_id(server, "doc.txt")
    touched = ["doc.txt"]

    match case.local:
        case "rename" | "move":
            target = _FILE_PATHS[case.local][0]
            _rename(device, "doc.txt", target)
            touched.append(target)
        case "modify":
            device.write("doc.txt", LOCAL)
        case "delete":
            device.delete("doc.txt")
    match case.remote:
        case "rename" | "move":
            server.relocate("doc.txt", _FILE_PATHS[case.remote][1])
        case "modify":
            server.put("doc.txt", REMOTE)
        case "delete":
            server.remove("doc.txt")

    device.sync(local=touched if mode == "dirty" else None)

    _check(device, server, case)
    survivor = next(path for path in case.expected if path != "Dest")
    assert (_node_id(server, survivor) == original) is case.same_node


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("case", FOLDER_CASES, ids=str)
def test_folder(device: Device, server: FakeFileServer, case: Case, mode: str) -> None:
    device.mkdir("Dest")
    device.write("F/a.txt", A)
    device.sync()
    original = _node_id(server, "F/a.txt")
    touched = ["F"]

    match case.local:
        case "rename" | "move":
            target = _FOLDER_PATHS[case.local][0]
            _rename(device, "F", target)
            touched.append(target)
        case "modify":
            device.write("F/local.txt", LOCAL)
            touched.append("F/local.txt")
        case "delete":
            device.delete("F")
    match case.remote:
        case "rename" | "move":
            server.relocate("F", _FOLDER_PATHS[case.remote][1])
        case "modify":
            server.put("F/remote.txt", REMOTE)
        case "delete":
            server.remove("F")

    device.sync(local=touched if mode == "dirty" else None)

    _check(device, server, case)
    survivor = next(path for path in case.expected if path.endswith("/a.txt"))
    assert (_node_id(server, survivor) == original) is case.same_node


def test_renaming_a_folder_of_1000_files_sends_one_request(
    device: Device, server: FakeFileServer
) -> None:
    for number in range(1000):
        device.write(f"Big/{number}.txt", str(number).encode())
    device.sync()
    ids = {n.id for n in server.nodes.values()}
    server.calls.clear()

    _rename(device, "Big", "Huge")
    device.sync(local=["Big", "Huge"])

    assert server.calls.count("move") == 1
    assert not {"upload", "trash", "create_folder"} & set(server.calls)
    assert {n.id for n in server.nodes.values()} == ids
    assert len(plain(server.tree())) == 1001 and "Huge/999.txt" in server.tree()


def test_a_case_only_file_rename_on_either_side(device: Device, server: FakeFileServer) -> None:
    device.write("report.docx", BASE)
    device.sync()

    _rename(device, "report.docx", "Report.docx")
    device.sync(local=["Report.docx"])
    assert set(server.tree()) == {"Report.docx"}

    server.relocate("Report.docx", "REPORT.docx")
    device.sync()
    assert set(device.tree()) == set(server.tree()) == {"REPORT.docx"}
    assert server.trashed() == [] and device.bin.items() == []


def test_a_move_whose_target_is_taken_on_the_server_keeps_both(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "doc.txt", "taken.txt")
    server.put("taken.txt", REMOTE)

    device.sync()

    tree = plain(server.tree())
    assert plain(device.tree()) == tree
    assert tree["taken.txt"] == REMOTE
    assert BASE in tree.values()  # kept as a conflicted copy, nothing lost
    assert device.log.conflicts != []


def test_a_server_move_whose_target_is_taken_here_keeps_both(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    device.write("taken.txt", LOCAL)
    server.relocate("doc.txt", "taken.txt")

    device.sync()

    tree = plain(server.tree())
    assert plain(device.tree()) == tree
    assert sorted(v for v in tree.values() if v is not None) == sorted([BASE, LOCAL])
    assert device.bin.items() == []


def test_moving_into_a_new_folder_creates_it_first(device: Device, server: FakeFileServer) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    original = _node_id(server, "doc.txt")
    device.mkdir("New")
    _rename(device, "doc.txt", "New/doc.txt")

    device.sync(local=["doc.txt", "New", "New/doc.txt"])

    assert server.tree() == {"New": None, "New/doc.txt": BASE}
    assert _node_id(server, "New/doc.txt") == original


def test_moving_into_a_folder_deleted_on_the_server_brings_the_folder_back(
    device: Device, server: FakeFileServer
) -> None:
    device.mkdir("Dest")
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "doc.txt", "Dest/doc.txt")
    server.remove("Dest")

    device.sync()

    assert plain(device.tree()) == plain(server.tree())
    assert server.tree()["Dest/doc.txt"] == BASE
    assert device.bin.items() == []


def test_a_conflicted_copy_is_never_sent_as_a_rename(
    device: Device, server: FakeFileServer
) -> None:
    """The conflict step renames the local file; the server's version keeps its name."""
    device.write("doc.txt", BASE)
    device.sync()
    device.write("doc.txt", LOCAL)
    server.put("doc.txt", REMOTE)
    original = _node_id(server, "doc.txt")

    device.sync()

    assert server.tree()["doc.txt"] == REMOTE
    assert _node_id(server, "doc.txt") == original
    assert sorted(v for v in server.tree().values() if v is not None) == sorted([REMOTE, LOCAL])


def test_moves_dont_count_as_deletes(device: Device, server: FakeFileServer) -> None:
    for number in range(20):
        device.write(f"Old/{number}.txt", b"x%d" % number)
    device.sync()
    _rename(device, "Old", "New")

    result = device.sync(confirm_deletes=False)

    assert result.confirm_deletes is None
    assert set(server.tree()) == {"New", *(f"New/{n}.txt" for n in range(20))}


@pytest.mark.parametrize("point", ["move_remote.done", "move_local.done"])
def test_a_crash_after_a_move_converges_after_restart(
    device: Device, server: FakeFileServer, point: str
) -> None:
    device.write("F/a.txt", A)
    device.write("doc.txt", BASE)
    device.sync()
    _rename(device, "F", "Here")
    server.relocate("doc.txt", "there.txt")
    original = _node_id(server, "F/a.txt")

    def crash(name: str) -> None:
        if name == point:
            raise Crash(name)

    device.restart(crash_point=crash)
    with pytest.raises(Crash):
        device.sync()
    device.restart()
    device.sync()

    expected = {"Here": None, "Here/a.txt": A, "there.txt": BASE}
    assert plain(device.tree()) == plain(server.tree()) == expected
    assert _node_id(server, "Here/a.txt") == original
    assert server.trashed() == [] and device.bin.items() == []
    assert device.index.journal() == []


def test_a_lost_answer_to_a_move_is_recognised_as_done(
    device: Device, server: FakeFileServer
) -> None:
    device.write("doc.txt", BASE)
    device.sync()
    original = _node_id(server, "doc.txt")
    _rename(device, "doc.txt", "here.txt")
    server.fail("move", OfflineError("answer lost"), after=True)

    with pytest.raises(OfflineError):
        device.sync()
    server.calls.clear()
    device.sync()

    assert plain(device.tree()) == plain(server.tree()) == {"here.txt": BASE}
    assert _node_id(server, "here.txt") == original
    assert "upload" not in server.calls and server.trashed() == []


def test_a_move_on_the_server_never_overwrites_a_file_made_here_meanwhile(
    device: Device, server: FakeFileServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target appears between planning and renaming: the rename is refused, not forced,
    and both contents are kept."""
    from dropshare_client import platform
    from dropshare_client.sync import executor

    device.write("doc.txt", BASE)
    device.sync()
    server.relocate("doc.txt", "there.txt")
    real = platform.rename_no_replace
    raced: list[Path] = []

    def racing(source: Path, target: Path) -> None:
        if not raced:  # another program creates the target just now, once
            raced.append(target)
            target.write_bytes(LOCAL)
        real(source, target)

    monkeypatch.setattr(executor, "rename_no_replace", racing)
    device.sync()

    assert raced
    tree = plain(device.tree())
    assert tree == plain(server.tree())
    kept = [*tree.values(), *(content for _, content in device.bin.items())]
    assert LOCAL in kept and BASE in kept


@pytest.mark.parametrize("side", ["here", "server"])
def test_a_folder_rename_and_a_move_into_it_at_once_keep_both_ids(
    device: Device, server: FakeFileServer, side: str
) -> None:
    """The file's move overlaps the folder's: it waits a cycle instead of being read as
    deleted (audit S1)."""
    device.write("A/a.txt", A)
    device.write("x.txt", BASE)
    device.sync()
    folder, file = _node_id(server, "A"), _node_id(server, "x.txt")

    if side == "here":
        _rename(device, "A", "B")
        _rename(device, "x.txt", "B/x.txt")
    else:
        server.relocate("A", "B")
        server.relocate("x.txt", "B/x.txt")
    device.sync(local=["A", "B", "x.txt", "B/x.txt"])

    expected = {"B": None, "B/a.txt": A, "B/x.txt": BASE}
    assert plain(device.tree()) == plain(server.tree()) == expected
    assert (_node_id(server, "B"), _node_id(server, "B/x.txt")) == (folder, file)
    assert server.trashed() == [] and device.bin.items() == []


def test_an_editors_backup_copy_doesnt_take_the_files_identity(
    device: Device, server: FakeFileServer
) -> None:
    """Emacs-style save: the original is renamed to `report.txt~` and a new file written in
    its place. The node stays with report.txt (audit S2)."""
    device.write("report.txt", BASE)
    device.sync()
    original = _node_id(server, "report.txt")

    _rename(device, "report.txt", "report.txt~")
    device.write("report.txt", LOCAL)
    device.sync()

    assert plain(server.tree()) == {"report.txt": LOCAL, "report.txt~": BASE}
    assert _node_id(server, "report.txt") == original


def test_files_moved_out_of_a_deleted_folder_dont_count_as_deletes(
    device: Device, server: FakeFileServer
) -> None:
    """FR-SYNC-08 counts what leaves, not what moves (audit S3)."""
    for number in range(12):
        device.write(f"Old/{number}.txt", b"old %d" % number)
    for number in range(6):
        device.write(f"keep/{number}.txt", b"keep %d" % number)
    device.sync()
    device.mkdir("New")
    for number in range(12):
        _rename(device, f"Old/{number}.txt", f"New/{number}.txt")
    device.delete("Old")

    result = device.sync(confirm_deletes=False)

    assert result.confirm_deletes is None
    assert {p for p in server.tree() if p.startswith("New/")} == {f"New/{n}.txt" for n in range(12)}
    assert server.trashed() == [("Old", None)]


def test_an_empty_file_isnt_matched_to_a_moved_one_by_content(
    device: Device, server: FakeFileServer
) -> None:
    device.write("a.txt", b"")
    device.sync()
    original = _node_id(server, "a.txt")

    device.delete("a.txt")
    device.write("todo.txt", b"")
    device.sync()

    assert plain(server.tree()) == {"todo.txt": b""}
    assert _node_id(server, "todo.txt") != original
    assert server.trashed() == [("a.txt", b"")]


def test_a_rename_to_a_name_windows_cant_use_loses_nothing(
    device: Device, server: FakeFileServer
) -> None:
    """The scanner refuses the name (as the server would, NFR-COMPAT-02): the file stays
    here, listed as can't sync, and the old name goes to the server trash."""
    device.write("doc.txt", BASE)
    device.sync()

    _rename(device, "doc.txt", "CON.txt")
    device.sync()

    assert (device.root / "CON.txt").read_bytes() == BASE
    assert [problem.path for problem in device.index.problems()] == ["CON.txt"]
    assert server.trashed() == [("doc.txt", BASE)]
