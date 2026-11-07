"""A scanner for tests (FR-SAFE-01): clean unless the content holds a marker it was told about."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from dropshare_server.scanner.base import ScannerUnavailableError, ScannerVersion, ScanResult

SIGNATURE = "27450"


class FakeScanner:
    def __init__(self) -> None:
        self.down = False
        self.signature_age = timedelta(hours=2)
        self.scanned: list[bytes] = []
        self._detections: dict[bytes, str] = {}
        self._failing: set[bytes] = set()

    def detect(self, marker: bytes, name: str) -> None:
        """Report `name` for any content containing `marker`."""
        self._detections[marker] = name

    def fail_on(self, marker: bytes) -> None:
        """Fail to scan any content containing `marker`, while answering otherwise."""
        self._failing.add(marker)

    def scan(self, chunks: Iterable[bytes]) -> ScanResult:
        if self.down:
            raise ScannerUnavailableError("the test scanner is down")
        content = b"".join(chunks)
        self.scanned.append(content)
        if any(marker in content for marker in self._failing):
            raise ScannerUnavailableError("the test scanner failed on this content")
        found = next((name for marker, name in self._detections.items() if marker in content), None)
        return ScanResult(detection=found, signature=SIGNATURE)

    def version(self) -> ScannerVersion:
        if self.down:
            raise ScannerUnavailableError("the test scanner is down")
        return ScannerVersion(SIGNATURE, datetime.now(UTC) - self.signature_age)
