"""per-namespace change cursors and upload results

Revision ID: a1c3e5f70008
Revises: a1c3e5f70007
Create Date: 2026-10-26 11:00:00+00:00

The change cursor becomes a per-namespace sequence assigned under a namespace row lock, because
identity ids can commit out of order and a client would skip the late one (audit B1). Upload
sessions remember the revision they produced, so a repeated complete returns it (audit B2).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f70008"
down_revision: str | Sequence[str] | None = "a1c3e5f70007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "namespaces",
        sa.Column("last_change_seq", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.add_column("changes", sa.Column("seq", sa.BigInteger(), nullable=True))
    # Existing rows keep their id as their cursor, so stored client cursors stay valid.
    op.execute("UPDATE changes SET seq = id")
    op.execute(
        "UPDATE namespaces SET last_change_seq = "
        "(SELECT COALESCE(MAX(c.seq), 0) FROM changes c WHERE c.namespace_id = namespaces.id)"
    )
    op.alter_column("changes", "seq", existing_type=sa.BigInteger(), nullable=False)
    op.drop_index("ix_changes_namespace_id_id", table_name="changes")
    op.create_unique_constraint("uq_changes_namespace_id_seq", "changes", ["namespace_id", "seq"])
    op.add_column("upload_sessions", sa.Column("result_revision", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("upload_sessions", "result_revision")
    op.drop_constraint("uq_changes_namespace_id_seq", "changes", type_="unique")
    op.create_index("ix_changes_namespace_id_id", "changes", ["namespace_id", "id"])
    op.drop_column("changes", "seq")
    op.drop_column("namespaces", "last_change_seq")
