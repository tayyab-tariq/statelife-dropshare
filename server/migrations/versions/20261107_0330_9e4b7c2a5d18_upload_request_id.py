"""upload request id

Revision ID: 9e4b7c2a5d18
Revises: 5c2e9a7d1b40
Create Date: 2026-11-07 03:30:00+00:00

The request that handed an upload to the scan worker, so the audit events the worker writes
later can be traced to it (FR-AUDIT-02; audit 2026-11-07-0309). Nullable: no backfill.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9e4b7c2a5d18"
down_revision: str | Sequence[str] | None = "5c2e9a7d1b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("upload_sessions", sa.Column("request_id", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_sessions", "request_id")
