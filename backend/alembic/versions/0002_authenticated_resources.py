"""Authenticated resource metadata and pending queue."""

import sqlalchemy as sa

from alembic import op

revision = "0002_authenticated_resources"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "credential_profiles" not in existing:
        op.create_table(
            "credential_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("credential_id", sa.String(120), nullable=False),
            sa.Column("auth_type", sa.String(24), nullable=False),
            sa.Column("username", sa.String(255)),
            sa.Column("display_name", sa.String(255)),
            sa.Column("probe_url", sa.Text(), nullable=False),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("last_verified_at", sa.DateTime(timezone=True)),
            sa.Column("last_failure_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_code", sa.String(80)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("credential_id"),
        )
        op.create_index(
            "ix_credential_profiles_credential_id",
            "credential_profiles",
            ["credential_id"],
            unique=True,
        )
        op.create_index(
            "ix_credential_profiles_state", "credential_profiles", ["state"]
        )
    if "pending_resources" not in existing:
        op.create_table(
            "pending_resources",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("course_id", sa.Integer(), sa.ForeignKey("courses.id"), nullable=False),
            sa.Column(
                "source_item_id", sa.Integer(), sa.ForeignKey("source_items.id")
            ),
            sa.Column("source_name", sa.String(120), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("title", sa.String(512), nullable=False),
            sa.Column("item_type", sa.String(40), nullable=False),
            sa.Column("credential_id", sa.String(120)),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
            sa.Column("last_error_code", sa.String(80)),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
        )
        op.create_index("ix_pending_resources_course_id", "pending_resources", ["course_id"])
        op.create_index(
            "ix_pending_resources_source_item_id", "pending_resources", ["source_item_id"]
        )
        op.create_index(
            "ix_pending_resources_credential_id", "pending_resources", ["credential_id"]
        )
        op.create_index("ix_pending_resources_state", "pending_resources", ["state"])


def downgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "pending_resources" in tables:
        op.drop_table("pending_resources")
    if "credential_profiles" in tables:
        op.drop_table("credential_profiles")
