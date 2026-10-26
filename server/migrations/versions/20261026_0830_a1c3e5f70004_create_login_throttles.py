"""create login throttles

Revision ID: a1c3e5f70004
Revises: a1c3e5f70003
Create Date: 2026-10-26 08:30:00+00:00

Failed sign-in counting for logins that match no account (FR-AUTH-01/06, audit decision D1).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f70004"
down_revision: str | Sequence[str] | None = "a1c3e5f70003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_throttles",
        sa.Column("login_hash", sa.String(64), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("login_hash", name="pk_login_throttles"),
    )
    op.create_index("ix_login_throttles_updated_at", "login_throttles", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_login_throttles_updated_at", table_name="login_throttles")
    op.drop_table("login_throttles")
