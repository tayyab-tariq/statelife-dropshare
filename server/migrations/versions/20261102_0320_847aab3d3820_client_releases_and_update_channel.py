"""client releases and update channel

Revision ID: 847aab3d3820
Revises: d8b1f4a6c2e7
Create Date: 2026-11-02 03:20:59+00:00

Published client installers (FR-UPD-05) and the channel each user is offered (OQ-49). Existing
users start on stable. Versions are also stored as integer parts so they sort numerically.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "847aab3d3820"
down_revision: str | Sequence[str] | None = "d8b1f4a6c2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("version_major", sa.Integer(), nullable=False),
        sa.Column("version_minor", sa.Integer(), nullable=False),
        sa.Column("version_patch", sa.Integer(), nullable=False),
        sa.Column("min_supported_version", sa.String(16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("blob_key", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("published_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("withdrawn_at", sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_client_releases")),
        sa.UniqueConstraint("blob_key", name=op.f("uq_client_releases_blob_key")),
        sa.UniqueConstraint(
            "platform",
            "channel",
            "version",
            name=op.f("uq_client_releases_platform_channel_version"),
        ),
    )
    op.create_index(
        "ix_client_releases_latest",
        "client_releases",
        ["platform", "channel", "version_major", "version_minor", "version_patch"],
    )
    op.add_column(
        "users",
        sa.Column("update_channel", sa.String(32), nullable=False, server_default="stable"),
    )


def downgrade() -> None:
    op.drop_column("users", "update_channel")
    op.drop_index("ix_client_releases_latest", table_name="client_releases")
    op.drop_table("client_releases")
