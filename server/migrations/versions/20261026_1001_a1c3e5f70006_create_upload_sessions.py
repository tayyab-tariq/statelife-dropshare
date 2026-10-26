"""create upload sessions

Revision ID: a1c3e5f70006
Revises: a1c3e5f70005
Create Date: 2026-10-26 10:01:00+00:00

Resumable chunked uploads (FR-XFER-01).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f70006"
down_revision: str | Sequence[str] | None = "a1c3e5f70005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("namespace_id", sa.Uuid(), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("node_id", sa.Uuid(), nullable=True),
        sa.Column("base_revision", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("chunk_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("completed_at", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_upload_sessions"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_upload_sessions_user_id_users"),
        sa.ForeignKeyConstraint(
            ["namespace_id"], ["namespaces.id"], name="fk_upload_sessions_namespace_id_namespaces"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["nodes.id"], name="fk_upload_sessions_parent_id_nodes"
        ),
        sa.ForeignKeyConstraint(["node_id"], ["nodes.id"], name="fk_upload_sessions_node_id_nodes"),
    )
    op.create_index("ix_upload_sessions_user_id", "upload_sessions", ["user_id"])
    op.create_index("ix_upload_sessions_namespace_id", "upload_sessions", ["namespace_id"])
    op.create_index("ix_upload_sessions_status", "upload_sessions", ["status"])
    op.create_index("ix_upload_sessions_expires_at", "upload_sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_upload_sessions_expires_at", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_status", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_namespace_id", table_name="upload_sessions")
    op.drop_index("ix_upload_sessions_user_id", table_name="upload_sessions")
    op.drop_table("upload_sessions")
