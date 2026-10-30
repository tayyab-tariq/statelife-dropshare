"""journal outlives purged nodes

Revision ID: c4a7d2e9f310
Revises: db2e1bbabdaa
Create Date: 2026-10-29 09:00:00+00:00

Purging an item from the trash (FR-TRASH-03/04) deletes its node row, but the change journal is
append-only: a client that was offline for longer than the trash retention still has to read
the item's `trashed` row. So `changes.node_id` stops referencing `nodes`; the row's snapshot
already carries everything a client needs.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4a7d2e9f310"
down_revision: str | Sequence[str] | None = "db2e1bbabdaa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("fk_changes_node_id_nodes", "changes", type_="foreignkey")


def downgrade() -> None:
    # Fails if items were purged since: their journal rows point at nodes that no longer exist.
    op.create_foreign_key("fk_changes_node_id_nodes", "changes", "nodes", ["node_id"], ["id"])
