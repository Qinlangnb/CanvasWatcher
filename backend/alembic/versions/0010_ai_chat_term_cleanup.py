"""AI chat persistence, client snapshots, and exact term cleanup.

Revision ID: 0010_ai_chat_term_cleanup
Revises: 0009_google_calendar_task_ignore
"""

from collections import defaultdict

import sqlalchemy as sa

from alembic import op

revision = "0010_ai_chat_term_cleanup"
down_revision = "0009_google_calendar_task_ignore"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _merge_exact_term_labels() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, term_id, term_name, term_start_at, term_end_at, term_sort_key "
            "FROM courses WHERE term_name IS NOT NULL"
        )
    ).mappings()
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        label = str(row["term_name"] or "").strip()
        if label:
            grouped[label].append(dict(row))
    for label, values in grouped.items():
        identifiers = {str(row["term_id"] or "") for row in values}
        if len(identifiers) < 2:
            continue
        canonical = min(
            values,
            key=lambda row: (
                not str(row["term_id"] or "").isdigit(),
                int(row["term_id"]) if str(row["term_id"] or "").isdigit() else 2**31,
                row["id"],
            ),
        )
        connection.execute(
            sa.text(
                "UPDATE courses SET term_id=:term_id, term_name=:term_name, "
                "term_start_at=COALESCE(:term_start_at, term_start_at), "
                "term_end_at=COALESCE(:term_end_at, term_end_at), "
                "term_sort_key=COALESCE(:term_sort_key, term_sort_key), term=:term_name "
                "WHERE trim(term_name)=:term_name"
            ),
            {
                "term_id": canonical["term_id"],
                "term_name": label,
                "term_start_at": canonical["term_start_at"],
                "term_end_at": canonical["term_end_at"],
                "term_sort_key": canonical["term_sort_key"],
            },
        )


def upgrade() -> None:
    tables = _tables()
    if "tasks" in tables:
        columns = _columns("tasks")
        if "last_client_remaining_minutes" not in columns:
            op.add_column("tasks", sa.Column("last_client_remaining_minutes", sa.Integer()))
        if "last_client_reported_at" not in columns:
            op.add_column("tasks", sa.Column("last_client_reported_at", sa.DateTime(timezone=True)))

    if "chat_conversations" not in tables:
        op.create_table(
            "chat_conversations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "chat_messages" not in tables:
        op.create_table(
            "chat_messages",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("role", sa.String(16), nullable=False),
            sa.Column("content", sa.Text(), nullable=False, server_default=""),
            sa.Column("kind", sa.String(24), nullable=False, server_default="message"),
            sa.Column("tool_name", sa.String(80)),
            sa.Column("payload_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_chat_messages_conversation_id", "chat_messages", ["conversation_id"])
    if "ai_tool_confirmations" not in tables:
        op.create_table(
            "ai_tool_confirmations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("tool_name", sa.String(80), nullable=False),
            sa.Column("arguments_json", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True)),
            sa.Column("result_json", sa.JSON(), nullable=False, server_default="{}"),
        )
        op.create_index("ix_ai_tool_confirmations_conversation_id", "ai_tool_confirmations", ["conversation_id"])
        op.create_index("ix_ai_tool_confirmations_tool_name", "ai_tool_confirmations", ["tool_name"])
        op.create_index("ix_ai_tool_confirmations_status", "ai_tool_confirmations", ["status"])
    _merge_exact_term_labels()


def downgrade() -> None:
    pass
