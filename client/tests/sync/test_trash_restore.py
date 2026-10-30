"""Items restored from the server trash come back to the sync folder (FR-TRASH-02): the server
sends CREATED for the item and everything restored with it, and the engine downloads them like
any new item. Nothing on the computer is lost on the way."""

from pathlib import Path

import pytest
from cloud_fakes import FakeCloud
from sync_fakes import Device

from dropshare_shared.schemas.nodes import AccessRole


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


def test_a_folder_deleted_elsewhere_and_restored_comes_back_with_its_files(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice = cloud.account("alice")
    alice.put("Docs")
    alice.put("Docs/Notes")
    alice.put("Docs/Notes/a.txt", b"apple")
    laptop = Device(tmp_path / "laptop", alice)
    laptop.sync()
    docs = alice.lookup("Docs")

    cloud.trash(docs)
    laptop.sync()
    assert "Docs" not in laptop.tree()
    cloud.restore(docs)
    laptop.sync()

    assert laptop.tree() == {"Docs": None, "Docs/Notes": None, "Docs/Notes/a.txt": b"apple"}
    assert laptop.index.entry_of(docs.id) is not None  # the same item, not a copy


def test_a_new_local_file_with_the_restored_name_is_kept_beside_it(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice = cloud.account("alice")
    alice.put("report.txt", b"old")
    laptop = Device(tmp_path / "laptop", alice)
    laptop.sync()
    report = alice.lookup("report.txt")
    cloud.trash(report)
    laptop.sync()

    laptop.write("report.txt", b"new, not uploaded yet")
    cloud.restore(report)
    laptop.sync()

    assert sorted(laptop.tree().values(), key=repr) == [b"new, not uploaded yet", b"old"]
    assert sorted(alice.tree().values(), key=repr) == [b"new, not uploaded yet", b"old"]


def test_the_owners_restored_shared_folder_is_followed_again(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice = cloud.account("alice")
    alice.put("Budget")
    alice.put("Budget/plan.txt", b"plan")
    namespace = cloud.share("alice", "Budget", "bob", AccessRole.EDITOR)
    cloud.namespaces[namespace].members.clear()  # everyone's access removed first (OQ-36)
    laptop = Device(tmp_path / "laptop", alice)
    laptop.sync()
    mount = alice.lookup("Budget")

    cloud.trash(mount)
    laptop.sync()
    assert laptop.tree() == {}
    cloud.restore(mount)
    laptop.sync()
    alice.put("Budget/later.txt", b"later")
    laptop.sync()

    assert laptop.tree() == {
        "Budget": None,
        "Budget/later.txt": b"later",
        "Budget/plan.txt": b"plan",
    }


def test_a_restore_renamed_on_the_server_arrives_beside_the_new_file(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice = cloud.account("alice")
    alice.put("report.txt", b"old")
    laptop = Device(tmp_path / "laptop", alice)
    laptop.sync()
    old = alice.lookup("report.txt")
    cloud.trash(old)
    alice.put("report.txt", b"new")
    laptop.sync()

    cloud.restore(old)
    laptop.sync()

    assert laptop.tree() == {"report.txt": b"new", "report (restored).txt": b"old"}


def test_a_file_whose_folder_was_deleted_after_it_comes_back_in_a_recreated_folder(
    cloud: FakeCloud, tmp_path: Path
) -> None:
    alice = cloud.account("alice")
    alice.put("Docs")
    alice.put("Docs/a.txt", b"apple")
    alice.put("Docs/b.txt", b"bee")
    laptop = Device(tmp_path / "laptop", alice)
    laptop.sync()
    a = alice.lookup("Docs/a.txt")
    cloud.trash(a)  # first the file, then the folder it was in
    cloud.trash(alice.lookup("Docs"))
    laptop.sync()
    assert laptop.tree() == {}

    cloud.restore(a)
    laptop.sync()

    assert laptop.tree() == {"Docs": None, "Docs/a.txt": b"apple"}  # b.txt stays in the trash
