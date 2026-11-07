"""The clamd client (FR-SAFE-01, OQ-57 (7)): its protocol against a fake clamd on a local socket,
and, marked `clamav`, against a real one (CLAMD_TEST_HOST)."""

import io
import os
import socket
import socketserver
import threading
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

from dropshare_server.core.config import Settings
from dropshare_server.scanner.base import ScannerUnavailableError
from dropshare_server.scanner.clamd import (
    LIMITS_EXCEEDED,
    ClamdScanner,
    parse_scan_reply,
    parse_version,
)

_VERSION = b"ClamAV 1.4.1/27450/Tue Nov  4 08:30:00 2026\0"
# The standard antivirus test file, split so no scanner flags this source file.
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$" + b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"

type Reply = Callable[[bytes], bytes]


class _FakeClamd(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, reply: Reply) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.reply = reply
        self.streams: list[bytes] = []
        self.frame_sizes: list[int] = []


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _FakeClamd)
        command = _read_until_nul(self.request)
        if command == b"zVERSION":
            self.request.sendall(_VERSION)
            return
        assert command == b"zINSTREAM"
        stream = bytearray()
        while True:
            size = int.from_bytes(_read_exactly(self.request, 4), "big")
            if size == 0:
                break
            server.frame_sizes.append(size)
            stream += _read_exactly(self.request, size)
        server.streams.append(bytes(stream))
        self.request.sendall(server.reply(bytes(stream)))


def _read_until_nul(conn: socket.socket) -> bytes:
    data = bytearray()
    while not data.endswith(b"\0"):
        data += conn.recv(1)
    return bytes(data[:-1])


def _read_exactly(conn: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        piece = conn.recv(size - len(data))
        if not piece:
            raise ConnectionError("closed")
        data += piece
    return bytes(data)


type StartClamd = Callable[[Reply], tuple["_FakeClamd", ClamdScanner]]


@pytest.fixture
def clamd() -> Iterator[StartClamd]:
    servers: list[_FakeClamd] = []

    def start(reply: Reply) -> tuple[_FakeClamd, ClamdScanner]:
        server = _FakeClamd(reply)
        servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        host, port = server.server_address[:2]
        return server, ClamdScanner(str(host), int(port), connect_timeout=2, reply_timeout=2)

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_clean_content_scans_clean_with_the_signature_version(clamd: StartClamd) -> None:
    server, scanner = clamd(lambda _stream: b"stream: OK\0")

    result = scanner.scan([b"hello ", b"world"])

    assert (result.detection, result.signature) == (None, "27450")
    assert server.streams == [b"hello world"]


def test_a_detection_is_reported_by_name(clamd: StartClamd) -> None:
    _, scanner = clamd(lambda _stream: b"stream: Win.Test.EICAR_HDB-1 FOUND\0")

    assert scanner.scan([b"x"]).detection == "Win.Test.EICAR_HDB-1"


def test_content_is_streamed_in_pieces_of_at_most_a_mebibyte(clamd: StartClamd) -> None:
    server, scanner = clamd(lambda _stream: b"stream: OK\0")
    big = bytes(range(256)) * (4096 * 3 + 7)  # just over 3 MiB in one chunk

    scanner.scan([big, b"", b"tail"])

    assert server.streams == [big + b"tail"]
    assert max(server.frame_sizes) <= 1024 * 1024
    assert 0 not in server.frame_sizes  # an empty chunk never ends the stream early


def test_a_stream_over_clamds_limit_is_refused_not_accepted(clamd: StartClamd) -> None:
    # OQ-58 (2): a file the scanner can't scan in full is never let through.
    _, scanner = clamd(lambda _stream: b"INSTREAM size limit exceeded. ERROR\0")

    assert scanner.scan([b"x"]).detection == LIMITS_EXCEEDED


@pytest.mark.parametrize(
    "reply",
    [b"Can't allocate memory ERROR\0", b"stream:  FOUND\0", b"what?\0", b""],
)
def test_anything_but_a_clear_answer_counts_as_no_answer(clamd: StartClamd, reply: bytes) -> None:
    _, scanner = clamd(lambda _stream: reply)

    with pytest.raises(ScannerUnavailableError):
        scanner.scan([b"x"])


def test_a_scanner_that_isnt_running_is_unavailable() -> None:
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    scanner = ClamdScanner("127.0.0.1", port, connect_timeout=1, reply_timeout=1)

    with pytest.raises(ScannerUnavailableError):
        scanner.scan([b"x"])
    with pytest.raises(ScannerUnavailableError):
        scanner.version()


def test_a_scanner_that_never_answers_times_out(clamd: StartClamd) -> None:
    release = threading.Event()

    def stall(_stream: bytes) -> bytes:
        release.wait(5)
        return b"stream: OK\0"

    _, scanner = clamd(stall)
    try:
        with pytest.raises(ScannerUnavailableError, match="in time"):
            scanner.scan([b"x"])
    finally:
        release.set()


def test_version_gives_the_signature_and_its_date(clamd: StartClamd) -> None:
    _, scanner = clamd(lambda _stream: b"stream: OK\0")

    version = scanner.version()

    assert version.signature == "27450"
    assert version.published_at == datetime(2026, 11, 4, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("reply", "detection"),
    [
        ("stream: OK", None),
        ("stream: Eicar-Signature FOUND", "Eicar-Signature"),
        (
            "stream: Heuristics.Limits.Exceeded.MaxFileSize FOUND",
            "Heuristics.Limits.Exceeded.MaxFileSize",
        ),
    ],
)
def test_scan_replies_are_read_exactly(reply: str, detection: str | None) -> None:
    assert parse_scan_reply(reply) == detection


@pytest.mark.parametrize("reply", ["ClamAV 1.4.1", "ClamAV 1.4.1/abc/date", "clamd/1/x"])
def test_unexpected_version_replies_count_as_no_answer(reply: str) -> None:
    with pytest.raises(ScannerUnavailableError):
        parse_version(reply)


def test_a_version_without_a_readable_date_still_gives_the_signature() -> None:
    version = parse_version("ClamAV 1.4.1/27450/sometime")

    assert (version.signature, version.published_at) == ("27450", None)


# --- A real clamd (run with CLAMD_TEST_HOST set; see docs/clamav-setup.md) -------------------


def _real_scanner() -> ClamdScanner:
    return ClamdScanner(
        os.environ["CLAMD_TEST_HOST"],
        int(os.environ.get("CLAMD_TEST_PORT", "3310")),
        connect_timeout=5,
        reply_timeout=900,
    )


@pytest.mark.clamav
def test_real_clamd_detects_the_eicar_test_file() -> None:
    assert _real_scanner().scan([EICAR]).detection is not None


@pytest.mark.clamav
def test_real_clamd_detects_eicar_inside_a_zip() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.txt", "nothing to see")
        archive.writestr("deep/eicar.com", EICAR)

    assert _real_scanner().scan([buffer.getvalue()]).detection is not None


@pytest.mark.clamav
def test_real_clamd_passes_clean_content_and_reports_fresh_signatures() -> None:
    scanner = _real_scanner()

    assert scanner.scan([b"%PDF-1.7\nhello\n"]).detection is None
    published = scanner.version().published_at
    assert published is not None
    assert (datetime.now(UTC) - published).days < 3


@pytest.mark.clamav
def test_real_clamd_scans_the_largest_allowed_file_in_full() -> None:
    # NFR-SCALE-01, OQ-58 (2), (4): the largest file DropShare accepts gets an answer within the
    # timeout. Had clamd skipped any of it for a limit, AlertExceedsMax would report it.
    largest = _app_default_max_file_size()
    piece = b"\0" * (8 * 1024 * 1024)

    def stream() -> Iterator[bytes]:
        for _ in range(largest // len(piece)):
            yield piece
        yield b"\0" * (largest % len(piece))

    assert _real_scanner().scan(stream()).detection is None


@pytest.mark.clamav
def test_real_clamd_cant_scan_more_than_2_gib_of_a_file() -> None:
    # Why the limit is 2,000 MiB (OQ-58 (4)): a larger file is reported, never passed unscanned.
    piece = b"\0" * (64 * 1024 * 1024)

    def stream() -> Iterator[bytes]:
        for _ in range(2 * 1024**3 // len(piece) + 1):
            yield piece

    assert _real_scanner().scan(stream()).detection == "Heuristics.Limits.Exceeded.MaxFileSize"


def _app_default_max_file_size() -> int:
    default = Settings.model_fields["max_file_size_bytes"].default
    assert isinstance(default, int)
    return default
