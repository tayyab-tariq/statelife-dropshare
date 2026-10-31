"""What the sync engine needs from the server (implemented by services.files.RemoteFiles)."""

import threading
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from dropshare_client.services.files import Progress, SourceFile, UploadSessions, UploadTarget
from dropshare_shared.schemas.nodes import NamespaceOut, NodeOut
from dropshare_shared.schemas.sync import ChangesResponse


class FileServer(Protocol):
    def namespaces(self) -> list[NamespaceOut]: ...

    def changes(self, namespace_id: uuid.UUID, cursor: int) -> ChangesResponse: ...

    def wait(self, cursors: Mapping[uuid.UUID, int]) -> list[uuid.UUID]: ...

    def get_node(self, node_id: uuid.UUID) -> NodeOut: ...

    def create_folder(self, parent_id: uuid.UUID, name: str) -> NodeOut: ...

    def trash(
        self,
        node_id: uuid.UUID,
        revision: int,
        cursor: int | None = None,
        mount_cursor: int | None = None,
    ) -> None: ...

    def move(
        self,
        node_id: uuid.UUID,
        revision: int,
        *,
        parent_id: uuid.UUID | None = None,
        name: str | None = None,
    ) -> NodeOut: ...

    def upload(
        self,
        source: SourceFile,
        target: UploadTarget,
        stop: threading.Event,
        progress: Progress = ...,
        *,
        sessions: UploadSessions | None = ...,
    ) -> NodeOut: ...

    def download(
        self,
        node_id: uuid.UUID,
        sha256: str,
        size: int,
        part: Path,
        stop: threading.Event,
        progress: Progress = ...,
    ) -> None: ...
