"""Separate user-owned completion from upstream submission facts."""

import sqlalchemy as sa
from alembic import op

revision = "0011_local_completion"
down_revision = "0010_ai_chat_term_cleanup"
branch_labels = None
depends_on = None


def upgrade():
    # The historical 0001 imports current Base.metadata. On a fresh database
    # these columns already exist; upgrade existing databases append-only.
    present = {row["name"] for row in sa.inspect(op.get_bind()).get_columns("tasks")}
    columns = [sa.Column("local_completed", sa.Boolean(), nullable=False, server_default=sa.false()),
               sa.Column("local_completed_at", sa.DateTime(timezone=True), nullable=True),
               sa.Column("completion_origin", sa.String(32), nullable=True),
               sa.Column("state_revision", sa.Integer(), nullable=False, server_default="0")]
    for column in columns:
        if column.name not in present:
            op.add_column("tasks", column)
    # Historical progress ownership/timestamps require a separate reviewed repair,
    # never infer a completion day from migration execution time.


def downgrade():
    for column in ("state_revision", "completion_origin", "local_completed_at", "local_completed"):
        op.drop_column("tasks", column)
