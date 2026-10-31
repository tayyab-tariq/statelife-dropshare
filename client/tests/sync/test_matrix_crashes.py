"""A crash at every step of every local x remote case (NFR-REL-01, M10).

Each case of the change matrix (test_sync_matrix.py) and of the move matrix (test_moves.py) is
run once to list the executor steps it passes. Then, for each of those steps, it is run again
from scratch with the power "cut" there, the app restarted, and synced to the end.

What must hold is "no data loss", not the very same result: both sides end the same; every
piece of content the uninterrupted run leaves in the files is still in the files (an edit
that wins stays live, FR-CONF-02), and everything else it keeps is still kept somewhere (the
server trash or the Recycle Bin); nothing appears that wasn't there before. The cases run one
transfer at a time, so both runs pass the same steps in the same order. A crash may make the engine
keep more: a file replaced here and moved on the server keeps its old version too when the
crash comes between the move and its record (test_moves "create-rename").
"""

from collections.abc import Callable
from pathlib import Path

import pytest
from sync import test_moves, test_sync_matrix
from sync_fakes import Crash, Device, FakeFileServer, plain

type Arrange = Callable[[Device, FakeFileServer], object]
type Check = Callable[[Device, FakeFileServer], None]


def _cases() -> list[tuple[str, Arrange, Check]]:
    cases: list[tuple[str, Arrange, Check]] = []
    for prefix, module, file_cases, folder_cases in (
        ("change", test_sync_matrix, test_sync_matrix.FILE_CASES, test_sync_matrix.FOLDER_CASES),
        ("move", test_moves, test_moves.FILE_CASES, test_moves.FOLDER_CASES),
    ):
        for kind, cases_of_kind, arrange in (
            ("file", file_cases, module.arrange_file),
            ("folder", folder_cases, module.arrange_folder),
        ):
            for case in cases_of_kind:
                cases.append(
                    (
                        f"{prefix}-{kind}-{case}",
                        lambda d, s, a=arrange, c=case: a(d, s, c),  # type: ignore[misc]  # each module's Case
                        lambda d, s, m=module, c=case: m.check(d, s, c),  # type: ignore[misc]  # each module's Case
                    )
                )
    return cases


CASES = _cases()


def _crash_at(index: int, seen: list[str]) -> Callable[[str], None]:
    def point(name: str) -> None:
        seen.append(name)
        if len(seen) == index + 1:
            raise Crash(name)

    return point


def _kept(device: Device, server: FakeFileServer) -> set[bytes]:
    """Every piece of content still somewhere: in the files, the server trash, the Recycle Bin."""
    found = {c for c in device.tree().values() if c is not None}
    found |= {c for c in server.tree().values() if c is not None}
    found |= {c for _, c in server.trashed() if c is not None}
    found |= {c for _, c in device.bin.items() if c is not None}
    return found


def _live(device: Device) -> set[bytes]:
    return {c for c in device.tree().values() if c is not None}


def _reference(
    base: Path, arrange: Arrange, check: Check
) -> tuple[list[str], set[bytes], set[bytes]]:
    """The steps an uninterrupted run passes, what it leaves in the files, and what it keeps
    anywhere (checked as the case says)."""
    server = FakeFileServer()
    device = Device(base, server)
    arrange(device, server)
    points: list[str] = []
    device.restart(crash_point=points.append, max_transfers=1)
    device.sync()
    check(device, server)
    return points, _live(device), _kept(device, server)


@pytest.mark.parametrize(("arrange", "check"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_a_crash_at_every_step_loses_nothing(
    tmp_path: Path, arrange: Arrange, check: Check
) -> None:
    points, live, kept = _reference(tmp_path / "reference", arrange, check)

    for index, point in enumerate(points):
        server = FakeFileServer()
        device = Device(tmp_path / f"crash-{index}", server)
        arrange(device, server)
        existed = _kept(device, server)  # before the sync: everything that may survive
        device.restart(crash_point=_crash_at(index, []), max_transfers=1)
        with pytest.raises(Crash):
            device.sync()

        device.restart(max_transfers=1)  # the app starts again after the power cut
        device.sync()

        after = f"after a crash at {point} (step {index})"
        assert plain(device.tree()) == plain(server.tree()), after
        assert live <= _live(device), f"{after}: content no longer in the files"
        assert kept <= _kept(device, server), f"{after}: content lost"
        assert _kept(device, server) <= existed | kept, f"{after}: content made up"
