"""Course term display fields and local archive lifecycle.

Revision ID: 0005_course_terms_archiving
Revises: 0004_canvas_first_class
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_course_terms_archiving"
down_revision = "0004_canvas_first_class"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("courses")
    }


def upgrade() -> None:
    existing = _columns()
    columns = [
            sa.Column(
                "lifecycle_state",
                sa.String(length=16),
                nullable=False,
                server_default="ACTIVE",
            ),
            sa.Column("archived_at", sa.DateTime(timezone=True)),
            sa.Column("term_id", sa.String(length=255)),
            sa.Column("term_name", sa.String(length=120)),
            sa.Column("term_start_at", sa.DateTime(timezone=True)),
            sa.Column("term_end_at", sa.DateTime(timezone=True)),
            sa.Column(
                "term_sort_key",
                sa.String(length=160),
                nullable=False,
                server_default="0000-0",
            ),
            sa.Column("display_course_code", sa.String(length=120)),
            sa.Column("display_name", sa.String(length=512)),
            sa.Column("section", sa.String(length=255)),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("courses", column)

    indexes = {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes("courses")
    }
    if "ix_courses_lifecycle_state" not in indexes:
        op.create_index(
            "ix_courses_lifecycle_state", "courses", ["lifecycle_state"]
        )
    if "ix_courses_term_id" not in indexes:
        op.create_index("ix_courses_term_id", "courses", ["term_id"])

    op.execute(
        "UPDATE courses SET lifecycle_state = CASE WHEN active = 1 "
        "THEN 'ACTIVE' ELSE 'ARCHIVED' END, "
        "term_id = term, term_name = term, "
        "display_course_code = course_code, display_name = name"
    )


def downgrade() -> None:
    with op.batch_alter_table("courses") as batch:
        batch.drop_index("ix_courses_term_id")
        batch.drop_index("ix_courses_lifecycle_state")
        batch.drop_column("section")
        batch.drop_column("display_name")
        batch.drop_column("display_course_code")
        batch.drop_column("term_sort_key")
        batch.drop_column("term_end_at")
        batch.drop_column("term_start_at")
        batch.drop_column("term_name")
        batch.drop_column("term_id")
        batch.drop_column("archived_at")
        batch.drop_column("lifecycle_state")
