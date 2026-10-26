"""create nodes and file versions

Revision ID: a1c3e5f70005
Revises: a1c3e5f70004
Create Date: 2026-10-26 10:00:00+00:00

Files and folders (requirements §8), quota usage on users, and a root folder for every existing
namespace.
"""

import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f70005"
down_revision: str | Sequence[str] | None = "a1c3e5f70004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("used_bytes", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("namespace_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("name_normalized", sa.String(1000), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("modified_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("modified_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("trashed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("trashed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("trash_root_id", sa.Uuid(), nullable=True),
        sa.Column("live_key", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_nodes"),
        sa.ForeignKeyConstraint(
            ["namespace_id"], ["namespaces.id"], name="fk_nodes_namespace_id_namespaces"
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["nodes.id"], name="fk_nodes_parent_id_nodes"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name="fk_nodes_created_by_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["modified_by_user_id"], ["users.id"], name="fk_nodes_modified_by_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["trashed_by_user_id"], ["users.id"], name="fk_nodes_trashed_by_user_id_users"
        ),
        sa.UniqueConstraint("live_key", name="uq_nodes_live_key"),
    )
    op.create_index("ix_nodes_namespace_id", "nodes", ["namespace_id"])
    op.create_index("ix_nodes_parent_id", "nodes", ["parent_id"])
    op.create_index("ix_nodes_trash_root_id", "nodes", ["trash_root_id"])
    op.create_table(
        "file_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("blob_key", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_file_versions"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], name="fk_file_versions_node_id_nodes"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_file_versions_created_by_user_id_users",
        ),
        sa.UniqueConstraint("blob_key", name="uq_file_versions_blob_key"),
    )
    op.create_index("ix_file_versions_node_id", "file_versions", ["node_id"])
    _create_root_folders()


def _create_root_folders() -> None:
    """Every namespace has one root folder (no parent, no name); see models/node.py."""
    namespaces = sa.table(
        "namespaces", sa.column("id", sa.Uuid()), sa.column("owner_user_id", sa.Uuid())
    )
    nodes = sa.table(
        "nodes",
        *(
            sa.column(name, sa.Uuid())
            for name in ("id", "namespace_id", "created_by_user_id", "modified_by_user_id")
        ),
        sa.column("kind", sa.String(32)),
        sa.column("size_bytes", sa.BigInteger()),
        sa.column("revision", sa.Integer()),
        sa.column("created_at", sa.TIMESTAMP()),
        sa.column("modified_at", sa.TIMESTAMP()),
        sa.column("live_key", sa.String(64)),
    )
    connection = op.get_bind()
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        {
            "id": uuid.uuid4(),
            "namespace_id": namespace_id,
            "created_by_user_id": owner_id,
            "modified_by_user_id": owner_id,
            "kind": "folder",
            "size_bytes": 0,
            "revision": 1,
            "created_at": now,
            "modified_at": now,
            # Same formula as repositories.nodes.live_key for a root.
            "live_key": _root_live_key(namespace_id),
        }
        for namespace_id, owner_id in connection.execute(
            sa.select(namespaces.c.id, namespaces.c.owner_user_id)
        )
    ]
    if rows:
        connection.execute(sa.insert(nodes), rows)


def _root_live_key(namespace_id: uuid.UUID) -> str:
    return hashlib.sha256(f"{namespace_id}/root".encode()).hexdigest()


def downgrade() -> None:
    op.drop_index("ix_file_versions_node_id", table_name="file_versions")
    op.drop_table("file_versions")
    op.drop_index("ix_nodes_trash_root_id", table_name="nodes")
    op.drop_index("ix_nodes_parent_id", table_name="nodes")
    op.drop_index("ix_nodes_namespace_id", table_name="nodes")
    op.drop_table("nodes")
    op.drop_column("users", "used_bytes")
