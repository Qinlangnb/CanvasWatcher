"""Today execution and asynchronous structured analysis.

Revision ID: 0007_today_ai_analysis
Revises: 0006_changes_read_file_types
"""

import sqlalchemy as sa

from alembic import op

revision = "0007_today_ai_analysis"
down_revision = "0006_changes_read_file_types"
branch_labels = None
depends_on = None


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    change_columns = _columns("change_events")
    if "semantic_key" not in change_columns:
        op.add_column("change_events", sa.Column("semantic_key", sa.String(64)))
    change_indexes = {
        row["name"] for row in sa.inspect(op.get_bind()).get_indexes("change_events")
    }
    if "ix_change_events_semantic_key" not in change_indexes:
        op.create_index(
            "ix_change_events_semantic_key",
            "change_events",
            ["semantic_key"],
            unique=True,
        )

    task_columns = _columns("task_analyses")
    additions = [
        ("analysis_status", sa.String(16)),
        ("input_hash", sa.String(64)),
        ("ai_estimated_minutes", sa.Integer()),
        ("calibration_factor", sa.Float()),
        ("effective_estimated_minutes", sa.Integer()),
    ]
    for name, column_type in additions:
        if name not in task_columns:
            op.add_column("task_analyses", sa.Column(name, column_type))
    op.execute(
        "UPDATE task_analyses SET analysis_status = 'READY' "
        "WHERE analysis_status IS NULL"
    )
    op.execute(
        "UPDATE task_analyses SET calibration_factor = 1.0 "
        "WHERE calibration_factor IS NULL"
    )
    op.execute(
        "UPDATE task_analyses SET ai_estimated_minutes = "
        "CAST(ROUND(estimated_effort_hours * 60) AS INTEGER) "
        "WHERE ai_estimated_minutes IS NULL"
    )
    op.execute(
        "UPDATE task_analyses SET effective_estimated_minutes = "
        "CAST(ROUND(estimated_effort_hours * 60) AS INTEGER) "
        "WHERE effective_estimated_minutes IS NULL"
    )
    task_indexes = {
        row["name"] for row in sa.inspect(op.get_bind()).get_indexes("task_analyses")
    }
    for name, columns in (
        ("ix_task_analyses_analysis_status", ["analysis_status"]),
        ("ix_task_analyses_input_hash", ["input_hash"]),
    ):
        if name not in task_indexes:
            op.create_index(name, "task_analyses", columns)

    tables = _tables()
    if "change_analyses" not in tables:
        op.create_table(
            "change_analyses",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("change_event_id", sa.Integer(), sa.ForeignKey("change_events.id"), nullable=False),
            sa.Column("analysis_status", sa.String(16), nullable=False, server_default="PENDING"),
            sa.Column("input_hash", sa.String(64), nullable=False),
            sa.Column("analysis_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("importance_score", sa.Float(), nullable=False, server_default="0"),
            sa.Column("severity", sa.String(16), nullable=False, server_default="low"),
            sa.Column("requires_action", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("recommended_action", sa.Text()),
            sa.Column("affected_task_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("replan_required", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("change_event_id"),
        )
        op.create_index("ix_change_analyses_change_event_id", "change_analyses", ["change_event_id"], unique=True)
        op.create_index("ix_change_analyses_analysis_status", "change_analyses", ["analysis_status"])
        op.create_index("ix_change_analyses_input_hash", "change_analyses", ["input_hash"])
        op.create_index("ix_change_analyses_importance_score", "change_analyses", ["importance_score"])
        op.create_index("ix_change_analyses_requires_action", "change_analyses", ["requires_action"])

    if "analysis_jobs" not in tables:
        op.create_table(
            "analysis_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("input_hash", sa.String(64), nullable=False),
            sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("kind", "entity_id", "input_hash", name="uq_analysis_job_identity"),
        )
        op.create_index("ix_analysis_jobs_kind", "analysis_jobs", ["kind"])
        op.create_index("ix_analysis_jobs_entity_id", "analysis_jobs", ["entity_id"])
        op.create_index("ix_analysis_jobs_state", "analysis_jobs", ["state"])


def downgrade() -> None:
    pass
