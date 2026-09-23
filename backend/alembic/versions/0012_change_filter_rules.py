"""Bounded notification filters and append-only mutation audit."""
import sqlalchemy as sa
from alembic import op

revision = "0012_change_filter_rules"
down_revision = "0011_local_completion"
branch_labels = None
depends_on = None


def upgrade():
    present = set(sa.inspect(op.get_bind()).get_table_names())
    if "change_filter_rules" not in present:
        op.create_table("change_filter_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("identity_key", sa.String(64), nullable=False, unique=True),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("removed", sa.Boolean(), nullable=False),
            sa.Column("rule_json", sa.JSON(), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("created_by", sa.String(16), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("match_count", sa.Integer(), nullable=False),
            sa.Column("last_matched_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    if "change_filter_audits" not in present:
        op.create_table("change_filter_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("rule_id", sa.Integer(), sa.ForeignKey("change_filter_rules.id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("actor", sa.String(16), nullable=False),
            sa.Column("action", sa.String(16), nullable=False),
            sa.Column("reason", sa.String(500), nullable=False),
            sa.Column("before_json", sa.JSON(), nullable=False),
            sa.Column("after_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
        op.create_index("ix_change_filter_audits_rule_id", "change_filter_audits", ["rule_id"])


def downgrade():
    op.drop_table("change_filter_audits")
    op.drop_table("change_filter_rules")
