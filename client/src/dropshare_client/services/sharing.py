"""Sharing on the server (FR-SHARE-01 … FR-SHARE-08): who has access, Shared with me, adding
and leaving shared folders, finding people. Blocking calls: controllers run them on the pool."""

import os
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from dropshare_client.platform import rename_no_replace
from dropshare_client.services.api_client import ApiClient
from dropshare_client.services.files import NewVersion, RemoteFiles, SourceFile, file_sha256
from dropshare_shared.schemas.envelope import PAGE_LIMIT_MAX, DataResponse, ListResponse
from dropshare_shared.schemas.nodes import NodeOut
from dropshare_shared.schemas.sharing import (
    GroupHit,
    MemberIn,
    MemberOut,
    MemberUpdateIn,
    MountIn,
    SharedItemOut,
    ShareRole,
    SharingOut,
    UserHit,
)


class Sharing:
    def __init__(self, api: ApiClient, files: RemoteFiles) -> None:
        self._api = api
        self._files = files

    def find_people(self, text: str) -> list[UserHit]:
        return self._api.envelope(
            "GET", "/api/v1/users/search", DataResponse[list[UserHit]], params={"q": text}
        ).data

    def find_groups(self, text: str) -> list[GroupHit]:
        return self._api.envelope(
            "GET", "/api/v1/groups/search", DataResponse[list[GroupHit]], params={"q": text}
        ).data

    def sharing(self, node_id: uuid.UUID) -> SharingOut:
        return self._api.data("GET", f"/api/v1/nodes/{node_id}/members", SharingOut)

    def add(
        self,
        node_id: uuid.UUID,
        role: ShareRole,
        *,
        user_id: uuid.UUID | None = None,
        group_id: uuid.UUID | None = None,
    ) -> MemberOut:
        body = MemberIn(user_id=user_id, group_id=group_id, role=role)
        return self._api.data(
            "POST",
            f"/api/v1/nodes/{node_id}/members",
            MemberOut,
            json=body.model_dump(mode="json", exclude_none=True),
        )

    def change(self, member_id: uuid.UUID, role: ShareRole) -> MemberOut:
        body = MemberUpdateIn(role=role).model_dump(mode="json")
        return self._api.data("PATCH", f"/api/v1/members/{member_id}", MemberOut, json=body)

    def remove(self, member_id: uuid.UUID) -> None:
        self._api.no_content("DELETE", f"/api/v1/members/{member_id}")

    def shared_with_me(self) -> list[SharedItemOut]:
        """Everything shared with the user, page by page."""
        items: list[SharedItemOut] = []
        page = 1
        while True:
            response = self._api.envelope(
                "GET",
                "/api/v1/shared-with-me",
                ListResponse[SharedItemOut],
                params={"page": page, "limit": PAGE_LIMIT_MAX},
            )
            items.extend(response.data)
            if not response.data or len(items) >= response.meta.total:
                return items
            page += 1

    def add_to_dropshare(self, namespace_id: uuid.UUID) -> NodeOut:
        body = MountIn(namespace_id=namespace_id).model_dump(mode="json", exclude_none=True)
        return self._api.data("POST", "/api/v1/mounts", NodeOut, json=body)

    def leave(self, namespace_id: uuid.UUID) -> None:
        self._api.no_content("DELETE", f"/api/v1/mounts/{namespace_id}")

    # --- single shared files (FR-SHARE-03, OQ-32): not in the sync folder -------------------

    def save_copy(
        self,
        node_id: uuid.UUID,
        target: Path,
        stop: threading.Event,
        progress: Callable[[int, int], None] = lambda done, total: None,
    ) -> None:
        """Download the file to `target`, which must not exist: verified in a temp file next
        to it, then moved into place without replacing anything."""
        node = self._files.get_node(node_id)
        if node.content_hash is None:
            raise ValueError("Only files can be saved")
        part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
        try:
            self._files.download(
                node.id,
                node.content_hash,
                node.size_bytes,
                part,
                stop,
                lambda done: progress(done, node.size_bytes),
            )
            try:
                os.link(part, target)  # FileExistsError rather than replacing a file there
            except FileExistsError:
                raise
            except OSError:  # no hard links on this drive (FAT): a rename that won't replace
                rename_no_replace(part, target)
        finally:
            part.unlink(missing_ok=True)

    def upload_version(
        self,
        node_id: uuid.UUID,
        source: Path,
        stop: threading.Event,
        progress: Callable[[int, int], None] = lambda done, total: None,
    ) -> NodeOut:
        """A new version of a file shared with the user as editor (OQ-32). It replaces the
        current version; earlier versions are kept on the server (G6)."""
        node = self._files.get_node(node_id)
        info = source.stat()
        return self._files.upload(
            SourceFile(source, file_sha256(source, stop), info.st_size, info.st_mtime_ns),
            NewVersion(node.id, node.revision),
            stop,
            lambda done: progress(done, info.st_size),
        )
