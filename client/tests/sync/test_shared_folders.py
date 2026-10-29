"""Shared folders in the sync folder (FR-SHARE-02, OQ-30, OQ-31, OQ-33): adding and leaving
them, the owner sharing a synced folder, editing inside them, moving in and out, and access
ending with and without unsynced changes. Nothing is ever lost."""

import uuid
from pathlib import Path
from typing import NoReturn

import pytest
from cloud_fakes import Account, FakeCloud
from sync_fakes import Device, plain

from dropshare_client.services.errors import ApiError, OfflineError
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.nodes import AccessRole
from dropshare_shared.schemas.sync import ChangesResponse


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


def _device(tmp_path: Path, name: str, account: Account) -> Device:
    return Device(tmp_path / name, account)


def _shared_budget(
    cloud: FakeCloud, role: AccessRole = AccessRole.EDITOR
) -> tuple[Account, Account]:
    """Alice's Budget (plan.txt, Q1/notes.txt), shared with Bob, who added it."""
    alice, bob = cloud.account("alice"), cloud.account("bob")
    alice.put("Budget")
    alice.put("Budget/plan.txt", b"plan")
    alice.put("Budget/Q1")
    alice.put("Budget/Q1/notes.txt", b"notes")
    namespace = cloud.share("alice", "Budget", "bob", role)
    bob.accept(namespace)
    return alice, bob


def test_an_added_shared_folder_syncs_like_the_users_own(cloud: FakeCloud, tmp_path: Path) -> None:
    _, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)

    laptop.sync()

    assert laptop.tree() == {
        "Budget": None,
        "Budget/Q1": None,
        "Budget/Q1/notes.txt": b"notes",
        "Budget/plan.txt": b"plan",
    }


def test_edits_and_new_files_in_a_shared_folder_reach_the_owner(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud)
    bobs, alices = _device(tmp_path, "bob", bob), _device(tmp_path, "alice", alice)
    bobs.sync()
    alices.sync()

    bobs.write("Budget/plan.txt", b"plan by bob")
    bobs.write("Budget/Q1/new.txt", b"new")
    bobs.mkdir("Budget/Q2")
    bobs.sync()
    alices.sync()

    assert alices.tree() == bobs.tree()
    assert alice.tree()["Budget/Q1/new.txt"] == b"new"
    assert "Budget/Q2" in alice.tree()


def test_the_owners_devices_keep_a_folder_they_share_without_downloading_it_again(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = cloud.account("alice"), cloud.account("bob")
    alice.put("Projects")
    alice.put("Projects/Budget")
    alice.put("Projects/Budget/plan.txt", b"plan")
    laptop = _device(tmp_path, "alice", alice)
    laptop.sync()
    before = laptop.tree()

    cloud.share("alice", "Projects/Budget", "bob", AccessRole.EDITOR)  # from the app elsewhere
    alice.calls.clear()
    laptop.sync()

    assert laptop.tree() == before
    assert "download" not in alice.calls and "upload" not in alice.calls
    assert laptop.bin.items() == []
    bob.accept(
        next(
            iter(
                n
                for n in cloud.namespaces
                if cloud.namespaces[n].owner == "alice" and n != cloud.personal["alice"]
            )
        )
    )
    bob.put("Budget/from-bob.txt", b"hi")
    laptop.sync()
    assert laptop.tree()["Projects/Budget/from-bob.txt"] == b"hi"


def test_a_shared_folder_is_followed_again_after_a_restart(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    laptop.restart()
    alice.put("Budget/later.txt", b"later")
    laptop.sync()

    assert laptop.tree()["Budget/later.txt"] == b"later"


def test_leaving_a_folder_without_changes_sends_it_to_the_recycle_bin(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    cloud.trash(bob.lookup("Budget"))  # left from the app (DELETE /mounts)
    laptop.sync()

    assert laptop.tree() == {}
    assert laptop.bin.items() == [
        ("Budget", None),
        ("Budget/Q1", None),
        ("Budget/Q1/notes.txt", b"notes"),
        ("Budget/plan.txt", b"plan"),
    ]
    assert "Budget/plan.txt" in alice.tree()  # nobody else is affected


def test_losing_access_keeps_unsynced_files_and_recycles_the_rest(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None
    laptop.write("Budget/plan.txt", b"bob's edit")  # not uploaded yet
    laptop.write("Budget/Q1/mine.txt", b"mine")

    cloud.unshare(namespace, "bob")
    laptop.sync()

    kept = {
        "Budget (access removed)": None,
        "Budget (access removed)/Q1": None,
        "Budget (access removed)/Q1/mine.txt": b"mine",
        "Budget (access removed)/plan.txt": b"bob's edit",
    }
    assert laptop.tree() == kept
    assert bob.tree() == kept  # uploaded to Bob's own DropShare (OQ-33)
    # The unchanged file, which the owner still has, goes to the Recycle Bin.
    assert laptop.bin.items() == [
        ("Budget", None),
        ("Budget/Q1", None),
        ("Budget/Q1/notes.txt", b"notes"),
    ]
    assert alice.tree()["Budget/plan.txt"] == b"plan"


def test_deleting_the_shared_folder_here_leaves_it(cloud: FakeCloud, tmp_path: Path) -> None:
    alice, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    laptop.delete("Budget")
    laptop.sync()

    assert "Budget" not in bob.tree()
    assert alice.tree()["Budget/plan.txt"] == b"plan"
    namespace = alice.lookup("Budget").mount_namespace_id
    assert namespace is not None and cloud.live_mount("bob", namespace) is None


def test_the_owner_deleting_a_folder_others_still_have_gets_it_back(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, _ = _shared_budget(cloud)
    laptop = _device(tmp_path, "alice", alice)
    laptop.sync()

    laptop.delete("Budget")
    laptop.sync()

    assert laptop.tree()["Budget/plan.txt"] == b"plan"
    assert "Budget/plan.txt" in alice.tree()


def test_moving_a_file_into_a_shared_folder_keeps_its_id(cloud: FakeCloud, tmp_path: Path) -> None:
    alice, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    mine = bob.put("mine.txt", b"mine")
    laptop.sync()

    (laptop.root / "mine.txt").rename(laptop.root / "Budget" / "mine.txt")
    laptop.sync()

    assert alice.lookup("Budget/mine.txt").id == mine.id
    assert "mine.txt" not in bob.tree() and laptop.tree()["Budget/mine.txt"] == b"mine"


def test_an_editor_moving_out_of_someone_elses_folder_gets_a_copy(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    """OQ-35: the server refuses; the original stays in the shared folder (and comes back
    here), and what the editor moved becomes their own copy. Nothing is deleted."""
    alice, bob = _shared_budget(cloud)
    laptop, desktop = _device(tmp_path, "laptop", bob), _device(tmp_path, "desktop", bob)
    laptop.sync()
    desktop.sync()

    (laptop.root / "Budget" / "Q1").rename(laptop.root / "Q1")
    laptop.sync()
    desktop.sync()

    assert alice.tree()["Budget/Q1/notes.txt"] == b"notes"
    assert bob.tree()["Q1/notes.txt"] == b"notes"  # the editor's own copy
    assert laptop.tree() == desktop.tree() == bob.tree()
    assert laptop.bin.items() == [] and desktop.bin.items() == []


def test_the_owner_moving_out_of_her_shared_folder_arrives_whole_on_other_devices(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, _ = _shared_budget(cloud)
    laptop, desktop = _device(tmp_path, "laptop", alice), _device(tmp_path, "desktop", alice)
    laptop.sync()
    desktop.sync()

    (laptop.root / "Budget" / "Q1").rename(laptop.root / "Q1")
    laptop.sync()
    desktop.sync()

    assert desktop.tree() == laptop.tree()
    assert desktop.tree()["Q1/notes.txt"] == b"notes"
    assert desktop.bin.items() == []  # arrived, not deleted and downloaded again
    assert "Budget/Q1" not in alice.tree()


def test_a_recipient_renames_their_shared_folder_for_themselves(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    (laptop.root / "Budget").rename(laptop.root / "Alice's budget")
    laptop.sync()

    assert bob.tree()["Alice's budget/plan.txt"] == b"plan"
    assert "Budget/plan.txt" in alice.tree()


def test_a_release_interrupted_by_a_crash_finishes_after_restart(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    _, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None
    laptop.write("Budget/plan.txt", b"unsynced")
    cloud.unshare(namespace, "bob")

    laptop.core.fetch()  # the release is journaled here, then the app stops
    laptop.restart()
    laptop.sync()

    assert laptop.tree()["Budget (access removed)/plan.txt"] == b"unsynced"
    assert "Budget" not in laptop.tree()


def test_leaving_and_adding_again_unseen_keeps_the_folder_whole(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    """Both happened while this device wasn't looking: one fetch holds the old mount's trash
    and the new mount (sync review blocker)."""
    _, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None

    cloud.trash(bob.lookup("Budget"))
    bob.accept(namespace)
    laptop.sync()

    assert laptop.tree() == bob.tree()
    assert laptop.tree()["Budget/Q1/notes.txt"] == b"notes"


def test_a_release_survives_a_fetch_cut_short(
    cloud: FakeCloud, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mount's trash is saved, then the connection drops before the shared folder is
    looked at; after a restart the folder is still let go as OQ-33 says."""
    _, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None
    laptop.write("Budget/plan.txt", b"unsynced")
    cloud.unshare(namespace, "bob")

    def cut_short(dirty: set[str]) -> None:
        raise OfflineError("down")  # after the user's own journal was read and saved

    monkeypatch.setattr(laptop.core, "_follow_mounts", cut_short)
    with pytest.raises(OfflineError):
        laptop.sync()
    laptop.restart()
    laptop.sync()

    assert laptop.tree()["Budget (access removed)/plan.txt"] == b"unsynced"
    assert "Budget" not in laptop.tree()


def test_an_item_still_moving_between_namespaces_is_held_back_not_deleted(
    cloud: FakeCloud, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner's other device keeps moving files out of her shared folder while this one
    reads: whatever left and hasn't been seen arriving yet waits for the next fetch."""
    alice, _ = _shared_budget(cloud)
    for number in range(6):
        alice.put(f"Budget/f{number}.txt", f"{number}".encode())
    laptop = _device(tmp_path, "alice", alice)
    laptop.sync()
    namespace = alice.lookup("Budget").mount_namespace_id
    real_changes = alice.changes
    moved = iter(range(6))

    def changes_while_moving(namespace_id: uuid.UUID, cursor: int) -> ChangesResponse:
        number = next(moved, None)
        if namespace_id == namespace and number is not None:
            node = alice.lookup(f"Budget/f{number}.txt")
            alice.move(node.id, node.revision, parent_id=alice.personal.root_id)
        return real_changes(namespace_id, cursor)

    monkeypatch.setattr(alice, "changes", changes_while_moving)
    laptop.sync()
    monkeypatch.setattr(alice, "changes", real_changes)
    laptop.sync()

    assert laptop.bin.items() == []
    assert laptop.tree() == alice.tree()


def test_a_folder_that_cant_go_to_the_recycle_bin_yet_is_left_alone(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    """Nothing in it is uploaded to the user's own folder or deleted meanwhile; it is let go
    once it can be."""
    _, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None
    laptop.bin.broken = True

    cloud.unshare(namespace, "bob")
    laptop.sync()

    assert bob.tree() == {}  # nothing went to Bob's own folder
    assert laptop.tree()["Budget/plan.txt"] == b"plan"
    assert [p.key for p in laptop.index.problems()] == ["budget"]

    laptop.bin.broken = False
    laptop.restart()
    laptop.sync()
    assert laptop.tree() == {} and bob.tree() == {}


def test_local_items_that_never_synced_are_kept_too(cloud: FakeCloud, tmp_path: Path) -> None:
    """A link the scanner refuses to sync was never on the server: it is the user's."""
    _, bob = _shared_budget(cloud)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None
    (laptop.root / "Budget" / "shortcut").symlink_to(tmp_path)

    cloud.unshare(namespace, "bob")
    laptop.sync()

    assert (laptop.root / "Budget (access removed)" / "shortcut").is_symlink()


# --- view-only shared folders (FR-SHARE-05) ---------------------------------------------------


def test_a_viewers_files_are_read_only_on_disk(cloud: FakeCloud, tmp_path: Path) -> None:
    _shared_budget(cloud, AccessRole.VIEWER)
    laptop = _device(tmp_path, "bob", cloud.account("bob"))

    laptop.sync()

    assert laptop.attributes.marked(laptop.root) == {"Budget/plan.txt", "Budget/Q1/notes.txt"}


def test_a_viewers_edit_becomes_their_own_conflicted_copy_and_the_original_comes_back(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud, AccessRole.VIEWER)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    laptop.write("Budget/plan.txt", b"bob's edit")  # the attribute cleared by hand
    laptop.sync()

    assert plain(laptop.tree()) == {
        "Budget": None,
        "Budget/Q1": None,
        "Budget/Q1/notes.txt": b"notes",
        "Budget/plan.txt": b"plan",  # restored
        "plan (conflict).txt": b"bob's edit",  # in Bob's own folder, and uploaded there
    }
    assert plain(bob.tree()) == plain(laptop.tree())
    assert alice.tree()["Budget/plan.txt"] == b"plan"
    [(original, copy)] = laptop.log.conflicts
    assert original == "Budget/plan.txt" and copy.startswith("plan (conflicted copy")
    assert "Budget/plan.txt" in laptop.attributes.marked(laptop.root)
    assert copy not in laptop.attributes.marked(laptop.root)


def test_a_viewers_new_file_and_folder_move_to_their_own_folder(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud, AccessRole.VIEWER)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    laptop.write("Budget/mine.txt", b"mine")
    laptop.mkdir("Budget/Drafts")
    laptop.write("Budget/Drafts/d.txt", b"draft")
    laptop.sync()

    tree = plain(laptop.tree())
    assert tree["mine (conflict).txt"] == b"mine"
    assert tree["Drafts (conflict)/d.txt"] == b"draft"
    assert "Budget/mine.txt" not in tree and "Budget/Drafts" not in tree
    assert set(alice.tree()) == {"Budget", "Budget/Q1", "Budget/Q1/notes.txt", "Budget/plan.txt"}


def test_a_viewers_delete_and_rename_are_undone(cloud: FakeCloud, tmp_path: Path) -> None:
    alice, bob = _shared_budget(cloud, AccessRole.VIEWER)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    laptop.delete("Budget/Q1")
    (laptop.root / "Budget" / "plan.txt").rename(laptop.root / "Budget" / "renamed.txt")
    laptop.sync()

    tree = plain(laptop.tree())
    assert tree["Budget/Q1/notes.txt"] == b"notes"
    assert tree["Budget/plan.txt"] == b"plan"
    assert tree["renamed (conflict).txt"] == b"plan"  # nothing lost, nothing changed for others
    assert set(alice.tree()) == {"Budget", "Budget/Q1", "Budget/Q1/notes.txt", "Budget/plan.txt"}


def test_a_new_role_takes_effect_on_disk_at_the_next_sync(cloud: FakeCloud, tmp_path: Path) -> None:
    _, bob = _shared_budget(cloud, AccessRole.VIEWER)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None

    cloud.set_role(namespace, "bob", AccessRole.EDITOR)
    laptop.sync()
    writable = laptop.attributes.marked(laptop.root)
    laptop.write("Budget/plan.txt", b"edited")
    laptop.sync()
    cloud.set_role(namespace, "bob", AccessRole.VIEWER)
    laptop.sync()

    assert writable == set()
    assert bob.tree()["Budget/plan.txt"] == b"edited"
    assert laptop.attributes.marked(laptop.root) == {"Budget/plan.txt", "Budget/Q1/notes.txt"}


def test_a_file_moved_into_a_folder_the_viewer_may_edit_is_writable_again(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice, bob = _shared_budget(cloud, AccessRole.VIEWER)
    alice.put("Team")
    team = cloud.share("alice", "Team", "bob", AccessRole.EDITOR)
    bob.accept(team)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    assert "Budget/plan.txt" in laptop.attributes.marked(laptop.root)

    plan = alice.lookup("Budget/plan.txt")
    alice.move(plan.id, plan.revision, parent_id=alice.lookup("Team").id)  # the owner moves it
    laptop.sync()

    assert laptop.tree()["Team/plan.txt"] == b"plan"
    assert "Team/plan.txt" not in laptop.attributes.marked(laptop.root)


def test_kept_files_are_writable_in_the_users_own_folder(cloud: FakeCloud, tmp_path: Path) -> None:
    _, bob = _shared_budget(cloud, AccessRole.VIEWER)
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()
    namespace = bob.lookup("Budget").mount_namespace_id
    assert namespace is not None
    laptop.write("Budget/plan.txt", b"edited, still marked read-only")
    laptop.attributes.set_read_only(laptop.root / "Budget" / "plan.txt", True)

    cloud.unshare(namespace, "bob")
    laptop.sync()

    assert laptop.tree()["Budget (access removed)/plan.txt"] == b"edited, still marked read-only"
    assert laptop.attributes.marked(laptop.root) == set()


def test_a_refusal_outside_a_view_only_folder_is_listed_not_moved(
    cloud: FakeCloud, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a view-only folder's changes are moved aside; any other 403 is shown in Can't
    sync, so it can never loop."""
    bob = cloud.account("bob")
    laptop = _device(tmp_path, "bob", bob)
    laptop.sync()

    def refused(*args: object, **kwargs: object) -> NoReturn:
        raise ApiError(403, ErrorCode.FORBIDDEN, "Not allowed.")

    monkeypatch.setattr(bob, "upload", refused)
    laptop.write("mine.txt", b"mine")
    laptop.sync()

    assert laptop.tree() == {"mine.txt": b"mine"}
    assert [p.key for p in laptop.index.problems()] == ["mine.txt"]
