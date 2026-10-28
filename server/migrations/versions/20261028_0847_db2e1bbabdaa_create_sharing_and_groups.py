"""create sharing and groups

Revision ID: db2e1bbabdaa
Revises: a1c3e5f70008
Create Date: 2026-10-28 08:47:57+00:00

Groups, members of shared folders' namespaces, single-file shares (FR-SHARE-01/03), and mounts:
a mount is a folder node pointing at a shared namespace (OQ-31), so `nodes` gains
`mount_namespace_id` and `mount_key`. One user or group per folder or file is enforced with a
hashed `grantee_key`, not a composite unique index over nullable columns (database.md §5).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.schema import SchemaItem

revision: str = "db2e1bbabdaa"
down_revision: str | Sequence[str] | None = "a1c3e5f70008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ONE_GRANTEE = (
    "(user_id IS NOT NULL AND group_id IS NULL) OR (user_id IS NULL AND group_id IS NOT NULL)"
)


def _share_columns(table: str) -> list[SchemaItem]:
    """The columns and constraints namespace_members and file_shares have in common."""
    return [
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("grantee_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("grantee_key", name=f"uq_{table}_grantee_key"),
        # op.f: the name is final; the naming convention would prefix it again.
        sa.CheckConstraint(_ONE_GRANTEE, name=op.f(f"ck_{table}_one_grantee")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=f"fk_{table}_user_id_users"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], name=f"fk_{table}_group_id_groups"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], name=f"fk_{table}_created_by_user_id_users"
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("name_normalized", sa.String(1000), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_groups"),
        sa.UniqueConstraint("name_normalized", name="uq_groups_name_normalized"),
    )
    op.create_table(
        "group_members",
        sa.Column("group_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("group_id", "user_id", name="pk_group_members"),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name="fk_group_members_group_id_groups"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_group_members_user_id_users"),
    )
    op.create_index("ix_group_members_user_id", "group_members", ["user_id"])

    op.create_table(
        "namespace_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("namespace_id", sa.Uuid(), nullable=False),
        *_share_columns("namespace_members"),
        sa.ForeignKeyConstraint(
            ["namespace_id"], ["namespaces.id"], name="fk_namespace_members_namespace_id_namespaces"
        ),
    )
    for column in ("namespace_id", "user_id", "group_id"):
        op.create_index(f"ix_namespace_members_{column}", "namespace_members", [column])

    op.create_table(
        "file_shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        *_share_columns("file_shares"),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], name="fk_file_shares_node_id_nodes"),
    )
    for column in ("node_id", "user_id", "group_id"):
        op.create_index(f"ix_file_shares_{column}", "file_shares", [column])

    op.add_column("nodes", sa.Column("mount_namespace_id", sa.Uuid(), nullable=True))
    op.add_column("nodes", sa.Column("mount_key", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_nodes_mount_namespace_id_namespaces",
        "nodes",
        "namespaces",
        ["mount_namespace_id"],
        ["id"],
    )
    op.create_index("ix_nodes_mount_namespace_id", "nodes", ["mount_namespace_id"])
    op.create_unique_constraint("uq_nodes_mount_key", "nodes", ["mount_key"])


def downgrade() -> None:
    op.drop_constraint("uq_nodes_mount_key", "nodes", type_="unique")
    op.drop_index("ix_nodes_mount_namespace_id", table_name="nodes")
    op.drop_constraint("fk_nodes_mount_namespace_id_namespaces", "nodes", type_="foreignkey")
    op.drop_column("nodes", "mount_key")
    op.drop_column("nodes", "mount_namespace_id")
    for column in ("node_id", "user_id", "group_id"):
        op.drop_index(f"ix_file_shares_{column}", table_name="file_shares")
    op.drop_table("file_shares")
    for column in ("namespace_id", "user_id", "group_id"):
        op.drop_index(f"ix_namespace_members_{column}", table_name="namespace_members")
    op.drop_table("namespace_members")
    op.drop_index("ix_group_members_user_id", table_name="group_members")
    op.drop_table("group_members")
    op.drop_table("groups")
