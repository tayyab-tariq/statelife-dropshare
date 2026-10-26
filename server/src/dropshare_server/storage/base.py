"""The only interface through which file bytes are read or written (NFR-COMPAT-05)."""

from typing import Protocol


class StorageBackend(Protocol):
    def health_check(self) -> None:
        """Raise OSError if the storage can't currently be written."""
