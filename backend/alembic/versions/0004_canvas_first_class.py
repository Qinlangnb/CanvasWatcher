"""Canvas source identity, health, and safe account metadata.

Revision ID: 0004_canvas_first_class
Revises: 0003_timezone_deadline_integrity
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_canvas_first_class"
down_revision = "0003_timezone_deadline_integrity"
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
        "course_sources",
        [
            sa.Column("external_id", sa.String(length=255), nullable=True),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        ],
    )
    _add_missing(
        "credential_profiles",
        [sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}")],
    )
    if "course_source_id" not in _columns("source_items"):
        # SQLite cannot add a foreign-key constraint with plain ALTER TABLE.
        # Batch mode recreates the table while preserving every existing row.
        with op.batch_alter_table("source_items") as batch_op:
            batch_op.add_column(
                sa.Column("course_source_id", sa.Integer(), nullable=True)
            )
            batch_op.create_foreign_key(
                "fk_source_items_course_source_id_course_sources",
                "course_sources",
                ["course_source_id"],
                ["id"],
            )

    inspector = sa.inspect(op.get_bind())
    source_indexes = {index["name"] for index in inspector.get_indexes("course_sources")}
    if "ix_course_sources_external_id" not in source_indexes:
        op.create_index(
            "ix_course_sources_external_id", "course_sources", ["external_id"]
        )
    if "ix_course_sources_enabled" not in source_indexes:
        op.create_index("ix_course_sources_enabled", "course_sources", ["enabled"])
    item_indexes = {index["name"] for index in inspector.get_indexes("source_items")}
    if "ix_source_items_course_source_id" not in item_indexes:
        op.create_index(
            "ix_source_items_course_source_id", "source_items", ["course_source_id"]
        )


def downgrade() -> None:
    # V0-series migrations are intentionally forward-only to preserve local history.
    pass
