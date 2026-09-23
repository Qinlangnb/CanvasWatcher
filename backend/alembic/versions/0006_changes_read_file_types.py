"""Change read state and persistent file-type overrides.

Revision ID: 0006_changes_read_file_types
Revises: 0005_course_terms_archiving
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_changes_read_file_types"
down_revision = "0005_course_terms_archiving"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table)
    }


def upgrade() -> None:
    change_columns = _columns("change_events")
    if "read_at" not in change_columns:
        op.add_column(
            "change_events", sa.Column("read_at", sa.DateTime(timezone=True))
        )
    op.execute("UPDATE change_events SET read_at = detected_at WHERE read_at IS NULL")

    file_columns = _columns("downloaded_files")
    if "source_detected_type" not in file_columns:
        op.add_column(
            "downloaded_files", sa.Column("source_detected_type", sa.String(40))
        )
    if "user_override_type" not in file_columns:
        op.add_column(
            "downloaded_files", sa.Column("user_override_type", sa.String(40))
        )
    op.execute(
        "UPDATE downloaded_files SET source_detected_type = category "
        "WHERE source_detected_type IS NULL"
    )

    inspector = sa.inspect(op.get_bind())
    change_indexes = {row["name"] for row in inspector.get_indexes("change_events")}
    if "ix_change_events_read_at" not in change_indexes:
        op.create_index("ix_change_events_read_at", "change_events", ["read_at"])
    file_indexes = {row["name"] for row in inspector.get_indexes("downloaded_files")}
    if "ix_downloaded_files_user_override_type" not in file_indexes:
        op.create_index(
            "ix_downloaded_files_user_override_type",
            "downloaded_files",
            ["user_override_type"],
        )


def downgrade() -> None:
    pass
