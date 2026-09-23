"""Work tracking, calendar capacity, settings, and source connections.

Revision ID: 0008_work_calendar_settings_sources
Revises: 0007_today_ai_analysis
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_work_calendar_settings_sources"
down_revision = "0007_today_ai_analysis"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "commute_minutes" not in _columns("courses"):
        op.add_column(
            "courses",
            sa.Column("commute_minutes", sa.Integer(), nullable=False, server_default="0"),
        )
    if "manual_time_adjustment_minutes" not in _columns("tasks"):
        op.add_column(
            "tasks",
            sa.Column(
                "manual_time_adjustment_minutes",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )

    tables = _tables()
    if "task_work_sessions" not in tables:
        op.create_table(
            "task_work_sessions",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id"), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ended_at", sa.DateTime(timezone=True)),
            sa.Column("duration_seconds", sa.Integer()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("source", sa.String(24), nullable=False, server_default="timer"),
        )
        op.create_index("ix_task_work_sessions_task_id", "task_work_sessions", ["task_id"])
        op.create_index("ix_task_work_sessions_started_at", "task_work_sessions", ["started_at"])
        op.create_index("ix_task_work_sessions_ended_at", "task_work_sessions", ["ended_at"])
        # SQLite expression index enforces one open timer for this single-process app.
        op.execute(
            "CREATE UNIQUE INDEX uq_task_work_sessions_one_open "
            "ON task_work_sessions ((1)) WHERE ended_at IS NULL"
        )

    # Revision 0001 intentionally creates the current metadata on brand-new
    # installations. In that path this table already exists by the time 0008
    # runs, so ensure the SQLite expression index independently as well.
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_task_work_sessions_one_open "
        "ON task_work_sessions ((1)) WHERE ended_at IS NULL"
    )

    if "calendar_events" not in tables:
        op.create_table(
            "calendar_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("calendar_id", sa.String(120), nullable=False, server_default="manual"),
            sa.Column("external_id", sa.String(255)),
            sa.Column("source", sa.String(32), nullable=False, server_default="manual"),
            sa.Column("summary", sa.String(512), nullable=False),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("location", sa.String(512)),
            sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("timezone", sa.String(80), nullable=False, server_default="America/Chicago"),
            sa.Column("recurrence_rule", sa.Text()),
            sa.Column("recurring_event_id", sa.String(255)),
            sa.Column("status", sa.String(24), nullable=False, server_default="confirmed"),
            sa.Column("event_type", sa.String(24), nullable=False, server_default="other"),
            sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        for name, columns in (
            ("ix_calendar_events_calendar_id", ["calendar_id"]),
            ("ix_calendar_events_external_id", ["external_id"]),
            ("ix_calendar_events_source", ["source"]),
            ("ix_calendar_events_start_at", ["start_at"]),
            ("ix_calendar_events_end_at", ["end_at"]),
            ("ix_calendar_events_recurring_event_id", ["recurring_event_id"]),
            ("ix_calendar_events_status", ["status"]),
            ("ix_calendar_events_event_type", ["event_type"]),
            ("ix_calendar_events_course_id", ["course_id"]),
        ):
            op.create_index(name, "calendar_events", columns)

    if "study_availability_rules" not in tables:
        op.create_table(
            "study_availability_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("weekday", sa.Integer(), nullable=False),
            sa.Column("start_local_time", sa.String(5), nullable=False),
            sa.Column("end_local_time", sa.String(5), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_study_availability_rules_weekday", "study_availability_rules", ["weekday"])

    if "study_availability_overrides" not in tables:
        op.create_table(
            "study_availability_overrides",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column("available_intervals", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("date"),
        )
        op.create_index("ix_study_availability_overrides_date", "study_availability_overrides", ["date"], unique=True)

    if "app_settings" not in tables:
        op.create_table(
            "app_settings",
            sa.Column("key", sa.String(120), primary_key=True),
            sa.Column("value_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )

    if "source_connections" not in tables:
        op.create_table(
            "source_connections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("external_key", sa.String(512), nullable=False),
            sa.Column("base_url", sa.Text()),
            sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id")),
            sa.Column("credential_id", sa.String(120)),
            sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("state", sa.String(24), nullable=False, server_default="READY"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("external_key"),
        )
        op.create_index("ix_source_connections_source_type", "source_connections", ["source_type"])
        op.create_index("ix_source_connections_external_key", "source_connections", ["external_key"], unique=True)
        op.create_index("ix_source_connections_course_id", "source_connections", ["course_id"])
        op.create_index("ix_source_connections_credential_id", "source_connections", ["credential_id"])
        op.create_index("ix_source_connections_state", "source_connections", ["state"])


def downgrade() -> None:
    pass
