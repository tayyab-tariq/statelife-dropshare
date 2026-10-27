"""The local SQLite index (algorithm.md §2). Only the sync engine's thread writes to it.

It is a cache of what was last in sync, so a schema change rebuilds it rather than migrating:
with no base, the engine links identical items and keeps both copies of different ones, and
never deletes anything (reconciler.py).
"""

import json
import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from sqlite3 import Connection as SqliteConnection

from pydantic import BaseModel, TypeAdapter
from sqlalchemy import (
    BigInteger,
    Dialect,
    Integer,
    String,
    Text,
    Uuid,
    create_engine,
    delete,
    event,
    func,
    or_,
    select,
)
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator

from dropshare_client.store.records import (
    Activity,
    Binding,
    Entry,
    JournalItem,
    NamespaceCursor,
    Problem,
    RemoteNode,
)
from dropshare_shared.schemas.nodes import NodeKind

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"
_ACTIVITY_KEPT = 1000
_SIDE_FILES = ("-wal", "-shm", "-journal")
_JOURNAL_DATA = TypeAdapter(dict[str, str])


class _BindingData(BaseModel):
    server_url: str
    user_id: uuid.UUID
    sync_root: str


class _UtcDateTime(TypeDecorator[datetime]):
    """Aware UTC datetimes as ISO text: SQLite has no time zone support."""

    impl = String(40)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        return None if value is None else value.astimezone(UTC).isoformat()

    def process_result_value(self, value: str | None, dialect: Dialect) -> datetime | None:
        return None if value is None else datetime.fromisoformat(value)


class _Base(DeclarativeBase):
    pass


class _Meta(_Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class _Entry(_Base):
    __tablename__ = "entries"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    file_id: Mapped[str | None] = mapped_column(String(64))


class _RemoteNode(_Base):
    __tablename__ = "remote_nodes"
    node_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    namespace_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    name: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16))
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str | None] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(BigInteger)
    modified_at: Mapped[datetime] = mapped_column(_UtcDateTime)


class _Cursor(_Base):
    __tablename__ = "cursors"
    namespace_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    root_node_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    cursor: Mapped[int] = mapped_column(BigInteger)


class _Journal(_Base):
    __tablename__ = "journal"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(Text)
    data: Mapped[str] = mapped_column(Text)


class _Activity(_Base):
    __tablename__ = "activity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(_UtcDateTime)
    kind: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)


class _Problem(_Base):
    __tablename__ = "problems"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


def _pragmas(connection: SqliteConnection, _: ConnectionPoolEntry) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=FULL")  # an index update survives a power cut
    cursor.close()


def _under(column: Mapped[str], key: str) -> ColumnElement[bool]:
    """`key` itself and everything below it. "" is the whole sync folder. Keys use "/" and
    "0" sorts right after it, so the range holds exactly the keys starting with `key/`."""
    if not key:
        return column.is_not(None)
    return or_(column == key, (column >= f"{key}/") & (column < f"{key}0"))


def _entry(row: _Entry) -> Entry:
    return Entry(
        key=row.key,
        path=row.path,
        node_id=row.node_id,
        kind=NodeKind(row.kind),
        revision=row.revision,
        sha256=row.sha256,
        size=row.size,
        mtime_ns=row.mtime_ns,
        file_id=row.file_id,
    )


def _remote(row: _RemoteNode) -> RemoteNode:
    return RemoteNode(
        node_id=row.node_id,
        namespace_id=row.namespace_id,
        parent_id=row.parent_id,
        name=row.name,
        kind=NodeKind(row.kind),
        size=row.size,
        sha256=row.sha256,
        revision=row.revision,
        modified_at=row.modified_at,
    )


def _remote_row(node: RemoteNode) -> _RemoteNode:
    return _RemoteNode(
        node_id=node.node_id,
        namespace_id=node.namespace_id,
        parent_id=node.parent_id,
        name=node.name,
        kind=node.kind.value,
        size=node.size,
        sha256=node.sha256,
        revision=node.revision,
        modified_at=node.modified_at,
    )


class Index:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(
            URL.create("sqlite", database=str(path)),
            connect_args={"check_same_thread": False},
        )
        event.listen(self._engine, "connect", _pragmas)
        self._sessions = sessionmaker(self._engine, expire_on_commit=False)
        self._prepare()

    @staticmethod
    def destroy(path: Path) -> None:
        """Remove an index file (signing out with the files removed). Our own files only."""
        for candidate in (path, *(path.with_name(path.name + side) for side in _SIDE_FILES)):
            candidate.unlink(missing_ok=True)

    def close(self) -> None:
        self._engine.dispose()

    def _prepare(self) -> None:
        _Base.metadata.create_all(self._engine)
        with self._sessions.begin() as session:
            version = session.get(_Meta, "schema")
            if version is not None and version.value == _SCHEMA_VERSION:
                return
        if version is not None:
            logger.warning("Rebuilding the local index (schema %s)", version.value)
        _Base.metadata.drop_all(self._engine)
        _Base.metadata.create_all(self._engine)
        with self._sessions.begin() as session:
            session.add(_Meta(key="schema", value=_SCHEMA_VERSION))

    # --- binding -------------------------------------------------------------------------

    def binding(self) -> Binding | None:
        with self._sessions() as session:
            row = session.get(_Meta, "binding")
            if row is None:
                return None
            data = _BindingData.model_validate_json(row.value)
            return Binding(data.server_url, data.user_id, data.sync_root)

    def reset(self, binding: Binding) -> None:
        """Forget everything and describe `binding`'s files from now on."""
        with self._sessions.begin() as session:
            for table in (_Entry, _RemoteNode, _Cursor, _Journal, _Activity, _Problem):
                session.execute(delete(table))
            value = _BindingData(
                server_url=binding.server_url,
                user_id=binding.user_id,
                sync_root=binding.sync_root,
            ).model_dump_json()
            session.merge(_Meta(key="binding", value=value))

    # --- entries (the base) --------------------------------------------------------------

    def entry(self, key: str) -> Entry | None:
        with self._sessions() as session:
            row = session.get(_Entry, key)
            return None if row is None else _entry(row)

    def entries_under(self, key: str) -> dict[str, Entry]:
        with self._sessions() as session:
            rows = session.scalars(select(_Entry).where(_under(_Entry.key, key)))
            return {row.key: _entry(row) for row in rows}

    def put_entry(self, entry: Entry) -> None:
        with self._sessions.begin() as session:
            session.merge(
                _Entry(
                    key=entry.key,
                    path=entry.path,
                    node_id=entry.node_id,
                    kind=entry.kind.value,
                    revision=entry.revision,
                    sha256=entry.sha256,
                    size=entry.size,
                    mtime_ns=entry.mtime_ns,
                    file_id=entry.file_id,
                )
            )

    def remove_entries(self, key: str, *, subtree: bool) -> None:
        with self._sessions.begin() as session:
            condition = _under(_Entry.key, key) if subtree else _Entry.key == key
            session.execute(delete(_Entry).where(condition))

    def file_count(self) -> int:
        with self._sessions() as session:
            count = session.scalar(
                select(func.count()).select_from(_Entry).where(_Entry.kind == NodeKind.FILE.value)
            )
            return count or 0

    # --- remote tree and cursors ---------------------------------------------------------

    def cursors(self) -> list[NamespaceCursor]:
        with self._sessions() as session:
            return [
                NamespaceCursor(row.namespace_id, row.root_node_id, row.cursor)
                for row in session.scalars(select(_Cursor))
            ]

    def start_namespace(self, namespace_id: uuid.UUID, root_node_id: uuid.UUID) -> None:
        """Follow a namespace from the start of its journal, dropping anything known of it."""
        with self._sessions.begin() as session:
            session.execute(delete(_RemoteNode).where(_RemoteNode.namespace_id == namespace_id))
            session.merge(_Cursor(namespace_id=namespace_id, root_node_id=root_node_id, cursor=0))

    def remote_nodes(self) -> list[RemoteNode]:
        with self._sessions() as session:
            return [_remote(row) for row in session.scalars(select(_RemoteNode))]

    def save_remote(
        self,
        upserts: Iterable[RemoteNode],
        removed: Iterable[uuid.UUID],
        cursor: NamespaceCursor | None = None,
    ) -> None:
        """Record changes to the remote tree, with the cursor they bring us to (if any), in
        one transaction: after a crash the tree and cursor always agree."""
        with self._sessions.begin() as session:
            removed_ids = list(removed)
            if removed_ids:
                session.execute(delete(_RemoteNode).where(_RemoteNode.node_id.in_(removed_ids)))
            for node in upserts:
                session.merge(_remote_row(node))
            if cursor is not None:
                session.merge(
                    _Cursor(
                        namespace_id=cursor.namespace_id,
                        root_node_id=cursor.root_node_id,
                        cursor=cursor.cursor,
                    )
                )

    # --- journal -------------------------------------------------------------------------

    def begin(self, kind: str, key: str, data: dict[str, str]) -> int:
        with self._sessions.begin() as session:
            row = _Journal(kind=kind, key=key, data=json.dumps(data))
            session.add(row)
            session.flush()
            return row.id

    def finish(self, journal_id: int) -> None:
        with self._sessions.begin() as session:
            session.execute(delete(_Journal).where(_Journal.id == journal_id))

    def journal(self) -> list[JournalItem]:
        with self._sessions() as session:
            return [
                JournalItem(row.id, row.kind, row.key, _JOURNAL_DATA.validate_json(row.data))
                for row in session.scalars(select(_Journal).order_by(_Journal.id))
            ]

    # --- activity and problems -----------------------------------------------------------

    def add_activity(self, kind: str, path: str, detail: str = "") -> None:
        with self._sessions.begin() as session:
            session.add(_Activity(at=datetime.now(UTC), kind=kind, path=path, detail=detail))
            session.flush()
            oldest_kept = session.scalar(
                select(_Activity.id).order_by(_Activity.id.desc()).offset(_ACTIVITY_KEPT - 1)
            )
            if oldest_kept is not None:
                session.execute(delete(_Activity).where(_Activity.id < oldest_kept))

    def recent_activity(self, limit: int = 100) -> list[Activity]:
        with self._sessions() as session:
            rows = session.scalars(select(_Activity).order_by(_Activity.id.desc()).limit(limit))
            return [Activity(row.at, row.kind, row.path, row.detail) for row in rows]

    def set_problem(self, problem: Problem) -> None:
        with self._sessions.begin() as session:
            session.merge(_Problem(key=problem.key, path=problem.path, reason=problem.reason))

    def clear_problems(self, key: str) -> None:
        """Forget the problems at and below `key`: they are looked at again."""
        with self._sessions.begin() as session:
            session.execute(delete(_Problem).where(_under(_Problem.key, key)))

    def problems(self) -> list[Problem]:
        with self._sessions() as session:
            return [
                Problem(row.key, row.path, row.reason)
                for row in session.scalars(select(_Problem).order_by(_Problem.key))
            ]
