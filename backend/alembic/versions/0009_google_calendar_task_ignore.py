"""Google/ICS calendar imports, notification dismissal, and task ignore.

Revision ID: 0009_google_calendar_task_ignore
Revises: 0008_work_calendar_settings_sources
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_google_calendar_task_ignore"
down_revision = "0008_work_calendar_settings_sources"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {row["name"] for row in sa.inspect(op.get_bind()).get_indexes(table)}


def _add_column(table: str, column: sa.Column) -> None:
    if column.name not in _columns(table):
        op.add_column(table, column)


def upgrade() -> None:
    tables = _tables()
    if "google_calendar_connections" not in tables:
        op.create_table(
            "google_calendar_connections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("state", sa.String(24), nullable=False, server_default="DISCONNECTED"),
            sa.Column("connected_account", sa.String(320)),
            sa.Column("connected_at", sa.DateTime(timezone=True)),
            sa.Column("last_successful_sync", sa.DateTime(timezone=True)),
            sa.Column("last_error", sa.Text()),
            sa.Column("refresh_token_ciphertext", sa.Text()),
            sa.Column("oauth_state_hash", sa.String(64)),
            sa.Column("oauth_state_expires_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_google_calendar_connections_state", "google_calendar_connections", ["state"]
        )

    tables = _tables()
    if "google_calendar_selections" not in tables:
        op.create_table(
            "google_calendar_selections",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "connection_id",
                sa.Integer(),
                sa.ForeignKey("google_calendar_connections.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("calendar_id", sa.String(512), nullable=False),
            sa.Column("summary", sa.String(512), nullable=False),
            sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("selected", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("timezone", sa.String(80)),
            sa.Column("access_role", sa.String(40)),
            sa.Column("sync_token", sa.Text()),
            sa.Column("last_sync_at", sa.DateTime(timezone=True)),
            sa.Column("state", sa.String(24), nullable=False, server_default="READY"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "connection_id", "calendar_id", name="uq_google_calendar_selection"
            ),
        )
        for name, columns, unique in (
            ("ix_google_calendar_selections_connection_id", ["connection_id"], False),
            ("ix_google_calendar_selections_calendar_id", ["calendar_id"], False),
            ("ix_google_calendar_selections_selected", ["selected"], False),
        ):
            op.create_index(name, "google_calendar_selections", columns, unique=unique)

    tables = _tables()
    if "ics_calendar_import_sources" not in tables:
        op.create_table(
            "ics_calendar_import_sources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("original_filename", sa.String(512), nullable=False),
            sa.Column("file_sha256", sa.String(64), nullable=False),
            sa.Column("calendar_name", sa.String(512), nullable=False),
            sa.Column("calendar_timezone", sa.String(80)),
            sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_reimported_at", sa.DateTime(timezone=True)),
            sa.Column("event_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("recurring_series_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("date_range_start", sa.Date()),
            sa.Column("date_range_end", sa.Date()),
            sa.Column("state", sa.String(24), nullable=False, server_default="IMPORTED"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_ics_calendar_import_sources_file_sha256",
            "ics_calendar_import_sources",
            ["file_sha256"],
        )
        op.create_index(
            "ix_ics_calendar_import_sources_state",
            "ics_calendar_import_sources",
            ["state"],
        )

    _add_column("tasks", sa.Column("ignored_at", sa.DateTime(timezone=True)))
    _add_column("notifications", sa.Column("dismissed_at", sa.DateTime(timezone=True)))
    for column in (
        sa.Column("import_source_id", sa.Integer()),
        sa.Column("original_start_at", sa.DateTime(timezone=True)),
        sa.Column("start_date", sa.Date()),
        sa.Column("end_date", sa.Date()),
        sa.Column("transparency", sa.String(16), nullable=False, server_default="opaque"),
        sa.Column("read_only", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_updated_at", sa.DateTime(timezone=True)),
    ):
        _add_column("calendar_events", column)

    for table, name, columns in (
        ("tasks", "ix_tasks_ignored_at", ["ignored_at"]),
        ("notifications", "ix_notifications_dismissed_at", ["dismissed_at"]),
        ("calendar_events", "ix_calendar_events_import_source_id", ["import_source_id"]),
    ):
        if name not in _indexes(table):
            op.create_index(name, table, columns)
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_calendar_event_external_identity "
        "ON calendar_events (source, calendar_id, external_id) WHERE external_id IS NOT NULL"
    )


def downgrade() -> None:
    pass
