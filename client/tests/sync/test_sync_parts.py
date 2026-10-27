"""The pieces under the engine: paths and conflict names, the remote tree, the index."""

import contextlib
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dropshare_client.platform.common import StatFileIdentity
from dropshare_client.store.index import Index
from dropshare_client.store.records import Binding, Entry, NamespaceCursor, Problem, RemoteNode
from dropshare_client.sync import paths
from dropshare_client.sync.remote_tree import RemoteTree, TreeDelta
from dropshare_client.sync.scanner import Scanner
from dropshare_shared.schemas.nodes import NodeKind
from dropshare_shared.schemas.sync import ChangeOut

WHEN = datetime(2026, 10, 27, 14, 5, tzinfo=UTC)
DASH = "\N{EN DASH}"


def _local_stamp() -> str:
    return WHEN.astimezone().strftime("%Y-%m-%d %H-%M")


def test_conflict_name_follows_fr_conf_01() -> None:
    name = paths.conflict_name("Budget.xlsx", "Alice Example", WHEN, lambda _: False)
    assert name == f"Budget (conflicted copy {DASH} Alice Example {DASH} {_local_stamp()}).xlsx"


def test_conflict_name_counts_up_when_taken() -> None:
    first = paths.conflict_name("a.txt", "Al", WHEN, lambda _: False)
    second = paths.conflict_name("a.txt", "Al", WHEN, lambda n: n == first)
    assert second == first.replace(").txt", ") (2).txt")


@pytest.mark.parametrize(
    ("name", "stem_end"),
    [("Makefile", "Makefile ("), (".profile", ".profile ("), ("a.b.c", "a.b (")],
)
def test_conflict_name_keeps_only_the_last_extension(name: str, stem_end: str) -> None:
    assert paths.conflict_name(name, "Al", WHEN, lambda _: False).startswith(stem_end)


@pytest.mark.parametrize(
    ("name", "who"),
    [
        ("report.txt", "A" * 255),  # display names can be up to 255 characters
        ("v1.2 notes about the " + "x" * 200, "Al"),  # a "long extension" after the dot
        ("report.txt", "Al\x7fice"),  # DEL isn't allowed in names
        ("x" * 255, "Al"),
    ],
)
def test_conflict_name_always_finds_a_valid_name(name: str, who: str) -> None:
    copy = paths.conflict_name(name, who, WHEN, lambda _: False)
    assert paths.name_problem(copy) is None


def test_conflict_name_stays_a_valid_name() -> None:
    name = paths.conflict_name("x" * 250 + ".txt", 'A/B:"C"', WHEN, lambda _: False)
    assert paths.name_problem(name) is None
    assert name.endswith(".txt")
    assert "A_B__C_" in name


@pytest.mark.parametrize(
    ("name", "ignored"),
    [
        ("desktop.ini", True),
        ("Desktop.INI", True),
        ("Thumbs.db", True),
        ("~$Budget.xlsx", True),
        ("notes.TMP", True),
        (".~lock.report.odt#", True),
        (".DS_Store", True),
        (".dropshare-tmp", True),
        ("budget.xlsx", False),
        ("tmp", False),
    ],
)
def test_ignored_names(name: str, ignored: bool) -> None:
    assert paths.is_ignored(name) is ignored


def test_outermost_keeps_only_top_level_keys() -> None:
    assert paths.outermost({"a/b", "a", "c/d", "c/de", "ab"}) == {"a", "ab", "c/d", "c/de"}
    assert paths.outermost({"x", ""}) == {""}


def _change(
    cursor: int,
    kind: str,
    node: uuid.UUID,
    parent: uuid.UUID,
    name: str,
    revision: int = 1,
    node_kind: str = "folder",
) -> ChangeOut:
    return ChangeOut.model_validate(
        {
            "cursor": cursor,
            "type": kind,
            "occurred_at": WHEN,
            "node_id": node,
            "parent_id": parent,
            "name": name,
            "kind": node_kind,
            "size_bytes": 0,
            "content_hash": "0" * 64 if node_kind == "file" else None,
            "revision": revision,
            "modified_at": WHEN,
            "modified_by_user_id": uuid.uuid4(),
        }
    )


def test_tree_follows_creates_and_trashes() -> None:
    root, ns, folder, file = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    delta = tree.apply(
        ns,
        [
            _change(1, "created", folder, root, "Docs"),
            _change(2, "created", file, folder, "a.txt", node_kind="file"),
        ],
    )
    assert delta.dirty == {"docs", "docs/a.txt"}
    assert set(tree.items_under("")) == {"docs", "docs/a.txt"}
    assert tree.path_of(file) == "Docs/a.txt"

    delta = tree.apply(ns, [_change(3, "trashed", folder, root, "Docs", revision=2)])

    assert delta.removed == {folder, file}
    assert tree.items_under("") == {}


def test_tree_ignores_an_older_revision_than_it_has() -> None:
    root, ns, node = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    tree.apply(ns, [_change(1, "created", node, root, "new name", revision=3)])

    tree.apply(ns, [_change(2, "modified", node, root, "old name", revision=2)])

    assert tree.path_of(node) == "new name"


def test_unknown_change_types_ask_for_the_node() -> None:
    root, node = uuid.uuid4(), uuid.uuid4()

    delta = RemoteTree(root).apply(uuid.uuid4(), [_change(1, "restored", node, root, "x")])

    assert delta.refetch == {node}


@pytest.mark.parametrize(
    "name", ["..", "a/b", "a\\b", "C:x", "CON", "trailing.", "bad\x01name", "Thumbs.db"]
)
def test_tree_never_offers_unusable_names(name: str) -> None:
    """Names decide paths on disk; one the server should never have sent is left out."""
    root, ns = uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    folder = uuid.uuid4()
    tree.apply(
        ns,
        [
            _change(1, "created", folder, root, name),
            _change(2, "created", uuid.uuid4(), folder, "inside.txt", node_kind="file"),
        ],
    )
    problems: dict[str, Problem] = {}

    assert tree.items_under("", problems) == {}
    assert len(problems) == (0 if name == "Thumbs.db" else 1)


def test_tree_never_offers_files_without_a_proper_hash() -> None:
    root, ns = uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    change = _change(1, "created", uuid.uuid4(), root, "a.txt", node_kind="file")
    tree.apply(ns, [change.model_copy(update={"content_hash": "../../x"})])

    assert tree.items_under("") == {}  # not a SHA-256: it can't be verified or used


def test_a_trashed_node_doesnt_come_back_from_an_older_answer() -> None:
    root, ns, node = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    tree.apply(ns, [_change(1, "created", node, root, "f", revision=1)])
    tree.apply(ns, [_change(2, "trashed", node, root, "f", revision=2)])
    stale = RemoteNode(node, ns, root, "f", NodeKind.FOLDER, 0, None, 1, WHEN)

    tree.upsert(stale, TreeDelta())  # e.g. a replayed upload answer

    assert tree.node_at("f") is None


def test_a_removal_of_an_unknown_node_is_still_saved() -> None:
    """After a failed save the database may hold rows memory doesn't: drop them too."""
    node = uuid.uuid4()
    delta = TreeDelta()

    RemoteTree(uuid.uuid4()).remove(node, delta, revision=3)

    assert delta.removed == {node}


def test_a_parent_cycle_has_no_path() -> None:
    root, ns, a, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    tree.apply(ns, [_change(1, "created", a, b, "a"), _change(2, "created", b, a, "b")])

    assert tree.path_of(a) is None


def test_tree_skips_ignored_names() -> None:
    root, ns = uuid.uuid4(), uuid.uuid4()
    tree = RemoteTree(root)
    tree.apply(ns, [_change(1, "created", uuid.uuid4(), root, "Thumbs.db", node_kind="file")])
    assert tree.items_under("") == {}


def _entry(key: str, kind: NodeKind = NodeKind.FILE) -> Entry:
    return Entry(key, key, uuid.uuid4(), kind, 1, None, 0, 0, None)


def test_index_subtree_queries_match_exact_prefixes(tmp_path: Path) -> None:
    index = Index(tmp_path / "index.db")
    for key in ("a", "a/b", "a/b/c", "a%", "a_b", "ab", "a0"):
        index.put_entry(_entry(key))

    assert set(index.entries_under("a")) == {"a", "a/b", "a/b/c"}
    assert set(index.entries_under("")) == {"a", "a/b", "a/b/c", "a%", "a_b", "ab", "a0"}

    index.remove_entries("a", subtree=True)
    assert set(index.entries_under("")) == {"a%", "a_b", "ab", "a0"}


def test_index_keeps_tree_and_cursor_together(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    index = Index(path)
    namespace, root = uuid.uuid4(), uuid.uuid4()
    index.start_namespace(namespace, root)
    node = RemoteNode(uuid.uuid4(), namespace, root, "a", NodeKind.FOLDER, 0, None, 1, WHEN)
    index.save_remote([node], [], NamespaceCursor(namespace, root, 7))
    index.close()

    reopened = Index(path)
    assert reopened.cursors() == [NamespaceCursor(namespace, root, 7)]
    assert reopened.remote_nodes() == [node]


def test_index_reset_forgets_everything_and_binds(tmp_path: Path) -> None:
    index = Index(tmp_path / "index.db")
    index.put_entry(_entry("a"))
    index.begin("upload", "a", {"sha256": "x"})
    binding = Binding("https://dropshare.example.com", uuid.uuid4(), "/Users/a/DropShare")

    index.reset(binding)

    assert index.binding() == binding
    assert index.entries_under("") == {}
    assert index.journal() == []


def test_index_keeps_recent_activity_only(tmp_path: Path) -> None:
    index = Index(tmp_path / "index.db")
    for n in range(1005):
        index.add_activity("uploaded", f"{n}.txt")

    recent = index.recent_activity(2000)

    assert len(recent) == 1000
    assert recent[0].path == "1004.txt"


def test_index_destroy_removes_its_files(tmp_path: Path) -> None:
    path = tmp_path / "index.db"
    index = Index(path)
    index.put_entry(_entry("a"))
    index.close()

    Index.destroy(path)

    assert list(tmp_path.iterdir()) == []


def test_tree_delta_marks_upserts_for_saving() -> None:
    root = uuid.uuid4()
    tree = RemoteTree(root)
    node = RemoteNode(uuid.uuid4(), uuid.uuid4(), root, "a", NodeKind.FOLDER, 0, None, 1, WHEN)
    delta = TreeDelta()

    tree.upsert(node, delta)

    assert delta.upserts == {node.node_id: node}
    assert tree.node_at("a") == node.node_id


class _Entry:
    """Enough of os.DirEntry for the scanner."""

    def __init__(self, folder: Path, name: str) -> None:
        self.name = name
        self._path = folder / name

    def stat(self, *, follow_symlinks: bool = True) -> os.stat_result:
        return self._path.lstat()


def test_two_names_with_one_key_are_both_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NTFS can hold "Straße.txt" and "Strasse.txt" (or NFC and NFD spellings) side by
    side; both map to one key, so neither is synced and both are listed (audit S5)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.txt").write_bytes(b"1")
    (root / "two.txt").write_bytes(b"2")

    @contextlib.contextmanager
    def listing(path: Path) -> Iterator[list[_Entry]]:
        assert Path(path) == root  # only files here: nothing else is listed
        yield [_Entry(root, "one.txt"), _Entry(root, "two.txt")]

    monkeypatch.setattr("dropshare_client.sync.scanner.os.scandir", listing)
    monkeypatch.setattr(
        "dropshare_client.sync.scanner.paths.key_of",
        lambda p: "same" if p.endswith(".txt") else p,
    )

    scan = Scanner(root, StatFileIdentity(), stable_after_ns=0).scan("")

    assert scan.items == {}
    assert "same" in scan.deferred
    assert "same name" in scan.problems["same"].reason
