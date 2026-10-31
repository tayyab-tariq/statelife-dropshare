"""Transfers interrupted by the app stopping pick up where they were after it starts again
(FR-XFER-01, FR-XFER-02, NFR-PERF-05): an upload resumes the server session the index kept,
a download its partial file. (That a resumed upload sends only the missing chunks is tested
over HTTP in test_files_service.py; this fake doesn't model chunks.)"""

from pathlib import Path

import pytest
from sync_fakes import Device, FakeFileServer

from dropshare_client.services.errors import OfflineError
from dropshare_client.services.files import NewFile, upload_key


@pytest.fixture
def server() -> FakeFileServer:
    return FakeFileServer()


def test_an_upload_cut_short_resumes_its_session_after_a_restart(
    tmp_path: Path, server: FakeFileServer
) -> None:
    laptop = Device(tmp_path / "laptop", server)
    laptop.write("big.bin", b"x" * 64)
    server.cut_upload_at = 32
    with pytest.raises(OfflineError):  # the connection drops halfway: the engine goes offline
        laptop.sync()
    assert "big.bin" not in server.tree()

    laptop.restart()  # the app quit and started again
    laptop.sync()

    assert server.tree()["big.bin"] == b"x" * 64
    assert len(server.resumed_uploads) == 1  # the session the index kept, not a new one


def test_a_download_cut_short_resumes_from_its_partial_file_after_a_restart(
    tmp_path: Path, server: FakeFileServer
) -> None:
    server.put("big.bin", b"y" * 64)
    laptop = Device(tmp_path / "laptop", server)
    server.cut_download_at = 40
    with pytest.raises(OfflineError):
        laptop.sync()
    assert "big.bin" not in laptop.tree()  # never under its real name until verified

    laptop.restart()
    laptop.sync()

    assert laptop.tree()["big.bin"] == b"y" * 64
    assert server.download_offsets == [0, 40]  # the rest only


def test_a_completed_upload_settled_by_recovery_forgets_its_session(
    tmp_path: Path, server: FakeFileServer
) -> None:
    """B2: the answer to the upload was lost, and recovery found it on the server. Its session
    must go, or the same content saved there later would be bound to that file wherever it is
    by then."""
    laptop = Device(tmp_path / "laptop", server)
    laptop.write("report.pdf", b"pdf")
    server.fail("upload", OfflineError("answer lost"), after=True)
    with pytest.raises(OfflineError):
        laptop.sync()
    key = upload_key(NewFile(server.root.id, "report.pdf"))
    assert laptop.index.upload_session(key) is not None

    laptop.sync()  # recovery: the server has it

    assert laptop.index.upload_session(key) is None
    assert server.tree() == {"report.pdf": b"pdf"}
