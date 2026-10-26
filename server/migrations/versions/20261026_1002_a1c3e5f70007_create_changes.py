"""create changes

Revision ID: a1c3e5f70007
Revises: a1c3e5f70006
Create Date: 2026-10-26 10:02:00+00:00

Append-only change journal per namespace; the id is the sync cursor (FR-SYNC-03, §10.1).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f70007"
down_revision: str | Sequence[str] | None = "a1c3e5f70006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "changes",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("namespace_id", sa.Uuid(), nullable=False),
        sa.Column("node_id", sa.Uuid(), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("modified_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("modified_by_user_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_changes"),
        sa.ForeignKeyConstraint(
            ["namespace_id"], ["namespaces.id"], name="fk_changes_namespace_id_namespaces"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], name="fk_changes_node_id_nodes"),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], name="fk_changes_actor_user_id_users"
        ),
    )
    op.create_index("ix_changes_namespace_id_id", "changes", ["namespace_id", "id"])
    op.create_index("ix_changes_node_id", "changes", ["node_id"])


def downgrade() -> None:
    op.drop_index("ix_changes_node_id", table_name="changes")
    op.drop_index("ix_changes_namespace_id_id", table_name="changes")
    op.drop_table("changes")
