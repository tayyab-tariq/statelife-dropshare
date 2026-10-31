"""changes remember mounts

Revision ID: d8b1f4a6c2e7
Revises: c4a7d2e9f310
Create Date: 2026-10-29 15:00:00+00:00

A journal row now carries the node's `mount_namespace_id` itself, so the change feed reads
`changes` alone. Joining `nodes` would drop the rows of purged items, which are exactly the
rows an offline client still has to read (FR-TRASH-04, audit 2026-10-29 B1). A node is a mount
from creation to purge, so the backfill copies the current value.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8b1f4a6c2e7"
down_revision: str | Sequence[str] | None = "c4a7d2e9f310"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("changes", sa.Column("mount_namespace_id", sa.Uuid(), nullable=True))
    # Portable correlated update (Oracle and PostgreSQL); only mounts have a value.
    op.execute(
        "UPDATE changes SET mount_namespace_id = ("
        "SELECT nodes.mount_namespace_id FROM nodes WHERE nodes.id = changes.node_id"
        ") WHERE node_id IN (SELECT id FROM nodes WHERE mount_namespace_id IS NOT NULL)"
    )


def downgrade() -> None:
    op.drop_column("changes", "mount_namespace_id")
