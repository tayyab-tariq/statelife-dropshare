"""A fake server with several users and shared folders, for the engine's sharing tests.

It behaves like the real one where the engine depends on it (services/sharing.py on the
server): sharing a folder makes it its own namespace with the same node IDs and mounts it in
place for the owner (OQ-30); a recipient's mount is a folder node in their own namespace
(OQ-31); moving between namespaces writes REMOVED there and CREATED here; removing someone
trashes their mount; viewers get 403 on writes; people without access get 404.
Each user talks to it through their own `Account`, which is the engine's FileServer.
"""

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sync_fakes import Tree, sha

from dropshare_client.services.errors import ApiError, RemoteContentChangedError, SourceChangedError
from dropshare_client.services.files import NewFile, Progress, SourceFile, UploadTarget, no_progress
from dropshare_shared.errors import ErrorCode
from dropshare_shared.naming import normalize_name
from dropshare_shared.schemas.nodes import (
    AccessRole,
    NamespaceKind,
    NamespaceOut,
    NodeKind,
    NodeOut,
)
from dropshare_shared.schemas.sync import ChangeOut, ChangesMeta, ChangesResponse, ChangeType


@dataclass
class CloudNode:
    id: uuid.UUID
    namespace_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str | None
    kind: NodeKind
    content: bytes = b""
    revision: int = 1
    trashed: bool = False
    trash_root: uuid.UUID | None = None  # the item deleted with it (server: trash_root_id)
    mount_namespace_id: uuid.UUID | None = None
    modified_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def out(self) -> NodeOut:
        return NodeOut(
            id=self.id,
            namespace_id=self.namespace_id,
            parent_id=self.parent_id,
            name=self.name,
            kind=self.kind,
            size_bytes=len(self.content) if self.kind == NodeKind.FILE else 0,
            content_hash=sha(self.content) if self.kind == NodeKind.FILE else None,
            revision=self.revision,
            created_at=self.modified_at,
            modified_at=self.modified_at,
            modified_by_user_id=uuid.UUID(int=0),
            mount_namespace_id=self.mount_namespace_id,
        )


@dataclass
class CloudNamespace:
    id: uuid.UUID
    kind: NamespaceKind
    owner: str
    root_id: uuid.UUID
    members: dict[str, AccessRole] = field(default_factory=dict)
    journal: list[ChangeOut] = field(default_factory=list)


def _not_found() -> ApiError:
    return ApiError(404, ErrorCode.NOT_FOUND, "Not found")


class FakeCloud:
    def __init__(self) -> None:
        self.namespaces: dict[uuid.UUID, CloudNamespace] = {}
        self.nodes: dict[uuid.UUID, CloudNode] = {}
        self.personal: dict[str, uuid.UUID] = {}
        self.lock = threading.RLock()

    def account(self, user: str) -> "Account":
        with self.lock:
            if user not in self.personal:
                namespace = self._namespace(NamespaceKind.PERSONAL, user)
                self.personal[user] = namespace.id
            return Account(self, user)

    def _namespace(
        self, kind: NamespaceKind, owner: str, root_id: uuid.UUID | None = None
    ) -> CloudNamespace:
        namespace_id = uuid.uuid4()
        if root_id is None:
            root = CloudNode(uuid.uuid4(), namespace_id, None, None, NodeKind.FOLDER)
            self.nodes[root.id] = root
            root_id = root.id
        namespace = CloudNamespace(namespace_id, kind, owner, root_id)
        self.namespaces[namespace_id] = namespace
        return namespace

    # --- the journal ---------------------------------------------------------------------

    def record(self, kind: ChangeType, node: CloudNode) -> None:
        journal = self.namespaces[node.namespace_id].journal
        journal.append(
            ChangeOut(
                cursor=len(journal) + 1,
                type=kind,
                occurred_at=datetime.now(UTC),
                node_id=node.id,
                parent_id=node.parent_id,
                name=node.name,
                kind=node.kind,
                size_bytes=len(node.content) if node.kind == NodeKind.FILE else 0,
                content_hash=sha(node.content) if node.kind == NodeKind.FILE else None,
                revision=node.revision,
                modified_at=node.modified_at,
                modified_by_user_id=uuid.UUID(int=0),
                mount_namespace_id=node.mount_namespace_id,
            )
        )

    # --- tree helpers --------------------------------------------------------------------

    def child(self, parent_id: uuid.UUID, name: str) -> CloudNode | None:
        key = normalize_name(name)
        return next(
            (
                n
                for n in self.nodes.values()
                if n.parent_id == parent_id
                and not n.trashed
                and normalize_name(n.name or "") == key
            ),
            None,
        )

    def below(self, node: CloudNode, *, live: bool = True) -> list[CloudNode]:
        """Everything below `node`, parents first."""
        found: list[CloudNode] = []
        level = [node.id]
        while level:
            children = [
                n
                for n in self.nodes.values()
                if n.parent_id in level and (not live or not n.trashed)
            ]
            found.extend(children)
            level = [n.id for n in children]
        return found

    def role(self, user: str, namespace_id: uuid.UUID) -> AccessRole | None:
        namespace = self.namespaces.get(namespace_id)
        if namespace is None:
            return None
        if namespace.owner == user:
            return AccessRole.OWNER
        return namespace.members.get(user)

    def live_mount(self, user: str, namespace_id: uuid.UUID) -> CloudNode | None:
        personal = self.personal[user]
        return next(
            (
                n
                for n in self.nodes.values()
                if n.namespace_id == personal
                and n.mount_namespace_id == namespace_id
                and not n.trashed
            ),
            None,
        )

    def trash(self, node: CloudNode) -> None:
        node.revision += 1
        node.trashed, node.trash_root = True, node.id
        for item in self.below(node):
            if item.namespace_id == node.namespace_id:
                item.trashed, item.trash_root = True, node.id
        shared = self.namespaces.get(node.mount_namespace_id) if node.mount_namespace_id else None
        if shared is not None and shared.owner == self.namespaces[node.namespace_id].owner:
            # The owner's own shared folder: its contents go with it, with no rows of their own.
            for item in self.below(self.nodes[shared.root_id]):
                item.trashed, item.trash_root = True, node.id
        self.record(ChangeType.TRASHED, node)

    def restore(self, node: CloudNode) -> None:
        """Restore from the trash as the server does (FR-TRASH-02): what was deleted with it and
        nothing deleted earlier on its own; a deleted parent recreated by name; a taken name
        "(restored)"; each item with a new revision and a CREATED row, parents first."""
        with self.lock:
            group = [n for n in self.nodes.values() if n.trash_root == node.id]
            parent_id = self._live_parent(node)
            assert node.name is not None
            name = node.name
            if self.child(parent_id, name) is not None:
                stem, dot, extension = name.rpartition(".")
                name = f"{stem} (restored).{extension}" if dot and stem else f"{name} (restored)"
            node.parent_id, node.name = parent_id, name
            for item in group:
                item.trashed, item.trash_root = False, None
                item.revision += 1
            ordered = [node] + [n for n in self.below(node) if n in group]
            if node.mount_namespace_id is not None:
                root = self.nodes[self.namespaces[node.mount_namespace_id].root_id]
                ordered += [n for n in self.below(root) if n in group]
            for item in ordered:
                self.record(ChangeType.CREATED, item)

    def _live_parent(self, node: CloudNode) -> uuid.UUID:
        """The live folder above `node`, recreating deleted ones by name on the way down."""
        missing: list[CloudNode] = []
        assert node.parent_id is not None
        parent = self.nodes[node.parent_id]
        while parent.trashed:
            missing.append(parent)
            assert parent.parent_id is not None
            parent = self.nodes[parent.parent_id]
        for gone in reversed(missing):
            assert gone.name is not None
            existing = self.child(parent.id, gone.name)
            if existing is None or existing.kind != NodeKind.FOLDER:
                existing = CloudNode(
                    uuid.uuid4(), parent.namespace_id, parent.id, gone.name, NodeKind.FOLDER
                )
                self.nodes[existing.id] = existing
                self.record(ChangeType.CREATED, existing)
            parent = existing
        return parent.id

    # --- other people's actions (server side) ------------------------------------------

    def share(self, owner: str, path: str, user: str, role: AccessRole) -> uuid.UUID:
        """`owner` shares their folder at `path` with `user`; returns the shared namespace."""
        with self.lock:
            folder = Account(self, owner).lookup(path)
            if (
                folder.parent_id is None
                and self.namespaces[folder.namespace_id].kind == NamespaceKind.SHARED
            ):
                self.namespaces[folder.namespace_id].members[user] = role
                return folder.namespace_id
            if folder.mount_namespace_id is not None:
                self.namespaces[folder.mount_namespace_id].members[user] = role
                return folder.mount_namespace_id
            descendants = self.below(folder, live=False)
            parent_id, name = folder.parent_id, folder.name
            assert parent_id is not None
            folder.revision += 1
            self.record(ChangeType.REMOVED, folder)
            shared = self._namespace(NamespaceKind.SHARED, owner, root_id=folder.id)
            folder.namespace_id, folder.parent_id, folder.name = shared.id, None, None
            for item in descendants:
                item.namespace_id = shared.id
            for item in [folder, *(d for d in descendants if not d.trashed)]:
                self.record(ChangeType.CREATED, item)
            mount = CloudNode(
                uuid.uuid4(),
                self.personal[owner],
                parent_id,
                name,
                NodeKind.FOLDER,
                mount_namespace_id=shared.id,
            )
            self.nodes[mount.id] = mount
            self.record(ChangeType.CREATED, mount)
            shared.members[user] = role
            return shared.id

    def set_role(self, namespace_id: uuid.UUID, user: str, role: AccessRole) -> None:
        with self.lock:
            self.namespaces[namespace_id].members[user] = role

    def unshare(self, namespace_id: uuid.UUID, user: str) -> None:
        """The owner removes `user`: their mount goes to the trash."""
        with self.lock:
            del self.namespaces[namespace_id].members[user]
            mount = self.live_mount(user, namespace_id)
            if mount is not None:
                self.trash(mount)


class Account:
    """One user's view of the cloud: the sync engine's FileServer."""

    def __init__(self, cloud: FakeCloud, user: str) -> None:
        self.cloud = cloud
        self.user = user
        self.calls: list[str] = []

    @property
    def personal(self) -> CloudNamespace:
        return self.cloud.namespaces[self.cloud.personal[self.user]]

    # --- test helpers: this user's actions through the app or another device -------------

    def lookup(self, path: str) -> CloudNode:
        """The node at `path` in this user's tree, through mounts."""
        node = self.cloud.nodes[self.personal.root_id]
        for part in path.split("/") if path else []:
            node = self._contents(node)
            child = self.cloud.child(node.id, part)
            assert child is not None, f"no {path} for {self.user}"
            node = child
        return node

    def _contents(self, node: CloudNode) -> CloudNode:
        """A mount's contents are its namespace's root's."""
        if node.mount_namespace_id is None:
            return node
        return self.cloud.nodes[self.cloud.namespaces[node.mount_namespace_id].root_id]

    def put(self, path: str, content: bytes | None = None) -> CloudNode:
        """Another of this user's devices creates or overwrites `path`."""
        with self.cloud.lock:
            parent = self._contents(self.lookup(path.rpartition("/")[0]))
            name = path.rpartition("/")[2]
            existing = self.cloud.child(parent.id, name)
            if existing is not None:
                assert content is not None
                existing.content, existing.revision = content, existing.revision + 1
                self.cloud.record(ChangeType.MODIFIED, existing)
                return existing
            kind = NodeKind.FOLDER if content is None else NodeKind.FILE
            node = CloudNode(
                uuid.uuid4(), parent.namespace_id, parent.id, name, kind, content or b""
            )
            self.cloud.nodes[node.id] = node
            self.cloud.record(ChangeType.CREATED, node)
            return node

    def accept(self, namespace_id: uuid.UUID) -> CloudNode:
        """POST /mounts: the shared folder at the top of this user's tree."""
        with self.cloud.lock:
            namespace = self.cloud.namespaces[namespace_id]
            owner_mount = self.cloud.live_mount(namespace.owner, namespace_id)
            name = owner_mount.name if owner_mount and owner_mount.name else "Shared"
            root = self.personal.root_id
            candidate, number = name, 0
            while self.cloud.child(root, candidate) is not None:
                number += 1
                candidate = f"{name} ({number})"
            mount = CloudNode(
                uuid.uuid4(),
                self.personal.id,
                root,
                candidate,
                NodeKind.FOLDER,
                mount_namespace_id=namespace_id,
            )
            self.cloud.nodes[mount.id] = mount
            self.cloud.record(ChangeType.CREATED, mount)
            return mount

    def tree(self) -> Tree:
        """This user's whole tree through their mounts, as the app would show it."""
        result: Tree = {}
        pending = [(self.cloud.nodes[self.personal.root_id], "")]
        while pending:
            node, path = pending.pop()
            for child in [
                n
                for n in self.cloud.nodes.values()
                if n.parent_id == self._contents(node).id and not n.trashed
            ]:
                child_path = f"{path}/{child.name}" if path else child.name or ""
                result[child_path] = child.content if child.kind == NodeKind.FILE else None
                if child.kind == NodeKind.FOLDER:
                    pending.append((child, child_path))
        return result

    # --- FileServer ------------------------------------------------------------------------

    def _role(self, namespace_id: uuid.UUID) -> AccessRole:
        role = self.cloud.role(self.user, namespace_id)
        if role is None:
            raise _not_found()
        return role

    def _node(self, node_id: uuid.UUID, *, write: bool = False, follow: bool = False) -> CloudNode:
        node = self.cloud.nodes.get(node_id)
        if node is None or node.trashed:
            raise _not_found()
        role = self._role(node.namespace_id)
        if follow and node.mount_namespace_id is not None:
            role = self._role(node.mount_namespace_id)
            node = self._contents(node)
        if write and role == AccessRole.VIEWER:
            raise ApiError(403, ErrorCode.FORBIDDEN, "You can view this item but not change it.")
        return node

    def namespaces(self) -> list[NamespaceOut]:
        with self.cloud.lock:
            self.calls.append("namespaces")
            result = [
                NamespaceOut(
                    id=self.personal.id,
                    kind=NamespaceKind.PERSONAL,
                    root_node_id=self.personal.root_id,
                    role=AccessRole.OWNER,
                )
            ]
            for node in self.cloud.nodes.values():
                if (
                    node.namespace_id != self.personal.id
                    or node.trashed
                    or node.mount_namespace_id is None
                ):
                    continue
                role = self.cloud.role(self.user, node.mount_namespace_id)
                if role is not None:
                    shared = self.cloud.namespaces[node.mount_namespace_id]
                    result.append(
                        NamespaceOut(
                            id=shared.id,
                            kind=NamespaceKind.SHARED,
                            root_node_id=shared.root_id,
                            role=role,
                        )
                    )
            return result

    def changes(self, namespace_id: uuid.UUID, cursor: int) -> ChangesResponse:
        with self.cloud.lock:
            self.calls.append("changes")
            self._role(namespace_id)
            later = [c for c in self.cloud.namespaces[namespace_id].journal if c.cursor > cursor]
            last = later[-1].cursor if later else cursor
            return ChangesResponse(data=later, meta=ChangesMeta(cursor=last, has_more=False))

    def wait(self, cursors: Mapping[uuid.UUID, int]) -> list[uuid.UUID]:
        with self.cloud.lock:
            return [
                namespace_id
                for namespace_id, cursor in cursors.items()
                if self.cloud.role(self.user, namespace_id) is None
                or len(self.cloud.namespaces[namespace_id].journal) > cursor
            ]

    def get_node(self, node_id: uuid.UUID) -> NodeOut:
        with self.cloud.lock:
            return self._node(node_id).out()

    def create_folder(self, parent_id: uuid.UUID, name: str) -> NodeOut:
        with self.cloud.lock:
            self.calls.append("create_folder")
            parent = self._node(parent_id, write=True, follow=True)
            if self.cloud.child(parent.id, name) is not None:
                raise ApiError(409, ErrorCode.NAME_CONFLICT, "Taken")
            node = CloudNode(uuid.uuid4(), parent.namespace_id, parent.id, name, NodeKind.FOLDER)
            self.cloud.nodes[node.id] = node
            self.cloud.record(ChangeType.CREATED, node)
            return node.out()

    def trash(
        self,
        node_id: uuid.UUID,
        revision: int,
        cursor: int | None = None,
        mount_cursor: int | None = None,
    ) -> None:
        with self.cloud.lock:
            self.calls.append("trash")
            node = self._node(node_id, write=True)
            if node.revision != revision:
                raise ApiError(409, ErrorCode.REVISION_CONFLICT, "Changed")
            for item in [node, *self.cloud.below(node)]:
                if item.mount_namespace_id is None:
                    continue
                shared = self.cloud.namespaces[item.mount_namespace_id]
                if shared.owner != self.user:
                    continue
                if item is not node or shared.members:  # as the server (OQ-36)
                    raise ApiError(409, ErrorCode.SHARE_NOT_ALLOWED, "Shared folder inside")
                if mount_cursor is not None and len(shared.journal) > mount_cursor:
                    raise ApiError(409, ErrorCode.REVISION_CONFLICT, "Changed inside")
            self.cloud.trash(node)

    def move(
        self,
        node_id: uuid.UUID,
        revision: int,
        *,
        parent_id: uuid.UUID | None = None,
        name: str | None = None,
    ) -> NodeOut:
        with self.cloud.lock:
            self.calls.append("move")
            node = self._node(node_id, write=True)
            assert node.parent_id is not None
            parent = self._node(
                parent_id or node.parent_id, write=True, follow=parent_id is not None
            )
            new_name = name or node.name or ""
            if (parent.id, new_name) == (node.parent_id, node.name) and node.revision in (
                revision,
                revision + 1,
            ):
                return node.out()
            if node.revision != revision:
                raise ApiError(409, ErrorCode.REVISION_CONFLICT, "Changed")
            existing = self.cloud.child(parent.id, new_name)
            if existing is not None and existing.id != node.id:
                raise ApiError(409, ErrorCode.NAME_CONFLICT, "Taken")
            node.revision += 1
            if parent.namespace_id == node.namespace_id:
                node.parent_id, node.name = parent.id, new_name
                self.cloud.record(ChangeType.MOVED, node)
                return node.out()
            source = self.cloud.namespaces[node.namespace_id]
            if source.kind == NamespaceKind.SHARED and source.owner != self.user:  # OQ-35
                node.revision -= 1
                raise ApiError(403, ErrorCode.FORBIDDEN, "Only the owner can move items out.")
            self.cloud.record(ChangeType.REMOVED, node)
            moved = [node, *self.cloud.below(node)]
            node.parent_id, node.name = parent.id, new_name
            for item in moved:
                item.namespace_id = parent.namespace_id
                self.cloud.record(ChangeType.CREATED, item)
            return node.out()

    def upload(
        self,
        source: SourceFile,
        target: UploadTarget,
        stop: threading.Event,
        progress: Progress = no_progress,
    ) -> NodeOut:
        with self.cloud.lock:
            self.calls.append("upload")
            content = source.path.read_bytes()
            if sha(content) != source.sha256:
                raise SourceChangedError(str(source.path))
            if isinstance(target, NewFile):
                parent = self._node(target.parent_id, write=True, follow=True)
                if self.cloud.child(parent.id, target.name) is not None:
                    raise ApiError(409, ErrorCode.NAME_CONFLICT, "Taken")
                node = CloudNode(
                    uuid.uuid4(),
                    parent.namespace_id,
                    parent.id,
                    target.name,
                    NodeKind.FILE,
                    content,
                )
                self.cloud.nodes[node.id] = node
                self.cloud.record(ChangeType.CREATED, node)
            else:
                node = self._node(target.node_id, write=True)
                if node.revision != target.base_revision:
                    raise ApiError(409, ErrorCode.REVISION_CONFLICT, "Changed")
                node.content, node.revision = content, node.revision + 1
                self.cloud.record(ChangeType.MODIFIED, node)
            progress(len(content))
            return node.out()

    def download(
        self,
        node_id: uuid.UUID,
        sha256: str,
        size: int,
        part: Path,
        stop: threading.Event,
        progress: Progress = no_progress,
    ) -> None:
        with self.cloud.lock:
            self.calls.append("download")
            node = self._node(node_id)
            if sha(node.content) != sha256:
                raise RemoteContentChangedError(str(node_id))
            part.write_bytes(node.content)
            progress(len(node.content))
