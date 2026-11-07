"""The only interface to the malware scanner (FR-SAFE-01, OQ-57 (7))."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class ScannerUnavailableError(Exception):
    """The scanner couldn't give an answer (down, timed out, or replied unintelligibly). Never
    read as clean: the upload waits and is scanned again (fail closed, OQ-55)."""


@dataclass(frozen=True)
class ScannerVersion:
    signature: str  # the signature database version, e.g. "27450"
    published_at: datetime | None  # when those signatures were published (UTC)


@dataclass(frozen=True)
class ScanResult:
    detection: str | None  # the scanner's name for what it found; None when clean
    signature: str  # the signature version the scan used


class Scanner(Protocol):
    def scan(self, chunks: Iterable[bytes]) -> ScanResult:
        """Scan a stream of bytes without holding it in memory; raise ScannerUnavailableError
        if there is no answer."""

    def version(self) -> ScannerVersion:
        """The scanner's signature version; raise ScannerUnavailableError if it doesn't answer."""
