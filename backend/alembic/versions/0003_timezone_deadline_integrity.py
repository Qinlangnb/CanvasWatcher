"""Timezone-safe deadlines and validated file export metadata.

Revision ID: 0003_timezone_deadline_integrity
Revises: 0002_authenticated_resources
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_timezone_deadline_integrity"
down_revision = "0002_authenticated_resources"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _add_missing(table: str, columns: list[sa.Column]) -> None:
    existing = _columns(table)
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add_missing(
        "downloaded_files",
        [
            sa.Column("integrity_status", sa.String(length=32), nullable=False, server_default="UNKNOWN"),
            sa.Column("validation_error", sa.Text(), nullable=True),
            sa.Column("export_name", sa.String(length=512), nullable=True),
            sa.Column("export_path", sa.Text(), nullable=True),
            sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("export_status", sa.String(length=32), nullable=False, server_default="NOT_CONFIGURED"),
            sa.Column("export_error", sa.Text(), nullable=True),
        ],
    )
    _add_missing(
        "tasks",
        [
            sa.Column("assigned_date_local", sa.Date(), nullable=True),
            sa.Column("due_date_local", sa.Date(), nullable=True),
            sa.Column("deadline_precision", sa.String(length=24), nullable=True),
            sa.Column("deadline_timezone", sa.String(length=80), nullable=True),
            sa.Column("source_deadline_text", sa.Text(), nullable=True),
            sa.Column("deadline_source_rank", sa.Integer(), nullable=True),
            sa.Column("source_key", sa.String(length=160), nullable=True),
        ],
    )
    _add_missing(
        "deadlines",
        [
            sa.Column("due_date_local", sa.Date(), nullable=True),
            sa.Column("deadline_precision", sa.String(length=24), nullable=True),
            sa.Column("deadline_timezone", sa.String(length=80), nullable=True),
            sa.Column("source_deadline_text", sa.Text(), nullable=True),
            sa.Column("source_rank", sa.Integer(), nullable=True),
        ],
    )

    inspector = sa.inspect(op.get_bind())
    task_indexes = {index["name"] for index in inspector.get_indexes("tasks")}
    task_uniques = {item["name"] for item in inspector.get_unique_constraints("tasks")}
    if "ix_tasks_due_date_local" not in task_indexes:
        op.create_index("ix_tasks_due_date_local", "tasks", ["due_date_local"])
    if "ix_tasks_source_key" not in task_indexes:
        op.create_index("ix_tasks_source_key", "tasks", ["source_key"])
    if "uq_task_course_source_key" not in task_indexes | task_uniques:
        op.create_index(
            "uq_task_course_source_key", "tasks", ["course_id", "source_key"], unique=True
        )
    file_indexes = {index["name"] for index in inspector.get_indexes("downloaded_files")}
    if "ix_downloaded_files_integrity_status" not in file_indexes:
        op.create_index(
            "ix_downloaded_files_integrity_status", "downloaded_files", ["integrity_status"]
        )

    due_at = next(column for column in sa.inspect(op.get_bind()).get_columns("deadlines") if column["name"] == "due_at")
    if not due_at["nullable"]:
        with op.batch_alter_table("deadlines") as batch:
            batch.alter_column("due_at", existing_type=sa.DateTime(), nullable=True)


def downgrade() -> None:
    # V0-series migrations are intentionally forward-only to protect local history.
    pass
