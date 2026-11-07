"""ClamAV's clamd over TCP (OQ-57 (7)): the INSTREAM and VERSION commands, standard library only.

Protocol: https://docs.clamav.net/manual/Usage/Scanning.html#clamd. Each command is sent with a
"z" prefix and a NUL terminator; INSTREAM then takes chunks, each preceded by its length as a
4-byte big-endian integer, and a zero length to end. The reply is "stream: OK", "stream: <name>
FOUND", or "<message> ERROR", NUL-terminated.
"""

import logging
import socket
import time
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime

from dropshare_server.scanner.base import (
    ScannerUnavailableError,
    ScannerVersion,
    ScanResult,
)

logger = logging.getLogger(__name__)

# clamd refuses a single chunk larger than StreamMaxLength; small pieces keep memory flat too.
_PIECE_BYTES = 1024 * 1024
_MAX_REPLY_BYTES = 4096
# Reported when a stream is larger than clamd may scan in full (OQ-58 (2)): refused, never
# accepted unscanned. The same name clamd uses for its own limits with AlertExceedsMax.
LIMITS_EXCEEDED = "Heuristics.Limits.Exceeded"


class ClamdScanner:
    def __init__(
        self, host: str, port: int, *, connect_timeout: float, reply_timeout: float
    ) -> None:
        self._address = (host, port)
        self._connect_timeout = connect_timeout
        self._reply_timeout = reply_timeout

    def version(self) -> ScannerVersion:
        reply = self._command(b"zVERSION\0", ())
        return parse_version(reply)

    def scan(self, chunks: Iterable[bytes]) -> ScanResult:
        # Asked on its own connection just before: if freshclam reloads in between, the version
        # recorded can be one behind the one that scanned. It is a record, not a guarantee.
        signature = self.version().signature
        reply = self._command(b"zINSTREAM\0", _framed(chunks))
        return ScanResult(detection=parse_scan_reply(reply), signature=signature)

    def _command(self, command: bytes, body: Iterable[bytes]) -> str:
        """One command on its own connection, all of it within `reply_timeout` seconds: a total,
        not per socket call, so a stalled clamd can't hold the caller longer."""
        deadline = time.monotonic() + self._reply_timeout
        try:
            with socket.create_connection(self._address, timeout=self._connect_timeout) as conn:
                _within(conn, deadline)
                conn.sendall(command)
                try:
                    for piece in body:
                        _within(conn, deadline)
                        conn.sendall(piece)
                except (BrokenPipeError, ConnectionResetError):
                    # clamd stops reading once a stream passes its limit, then replies.
                    logger.info("clamd closed the stream early; reading its reply")
                return _read_reply(conn, deadline)
        except TimeoutError as exc:
            raise ScannerUnavailableError("clamd didn't answer in time") from exc
        except OSError as exc:
            raise ScannerUnavailableError(f"clamd is unreachable: {exc}") from exc


def _within(conn: socket.socket, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    conn.settimeout(remaining)


def _framed(chunks: Iterable[bytes]) -> Iterator[bytes]:
    for chunk in chunks:
        for start in range(0, len(chunk), _PIECE_BYTES):
            piece = chunk[start : start + _PIECE_BYTES]
            yield len(piece).to_bytes(4, "big") + piece
    yield b"\0\0\0\0"


def _read_reply(conn: socket.socket, deadline: float) -> str:
    received = bytearray()
    while b"\0" not in received:
        _within(conn, deadline)
        data = conn.recv(_MAX_REPLY_BYTES)
        if not data:
            break
        received += data
        if len(received) > _MAX_REPLY_BYTES:
            raise ScannerUnavailableError("clamd's reply is too long")
    if not received:
        raise ScannerUnavailableError("clamd closed the connection without a reply")
    return received.split(b"\0", 1)[0].decode("utf-8", errors="replace").strip()


def parse_scan_reply(reply: str) -> str | None:
    """The detection name, None when clean; ScannerUnavailableError for anything else."""
    if reply == "stream: OK":
        return None
    if reply.startswith("stream: ") and reply.endswith(" FOUND"):
        detection = reply.removeprefix("stream: ").removesuffix(" FOUND").strip()
        if detection:
            return detection
    if reply.endswith("ERROR") and "size limit exceeded" in reply.lower():
        return LIMITS_EXCEEDED
    raise ScannerUnavailableError(f"clamd replied {reply!r}")


def parse_version(reply: str) -> ScannerVersion:
    """'ClamAV 1.4.1/27450/Tue Nov  4 08:30:00 2026' -> signature 27450 and its date."""
    parts = reply.split("/")
    if len(parts) != 3 or not parts[0].startswith("ClamAV ") or not parts[1].isdigit():
        raise ScannerUnavailableError(f"clamd's version reply is unexpected: {reply!r}")
    try:
        # clamd prints the date in its host's local time, like ctime(): UTC in the container.
        published = datetime.strptime(
            " ".join(parts[2].split()) + " +0000", "%a %b %d %H:%M:%S %Y %z"
        )
    except ValueError:
        return ScannerVersion(signature=parts[1], published_at=None)
    return ScannerVersion(signature=parts[1], published_at=published.astimezone(UTC))
