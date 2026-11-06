"""Owner reply receipts. Additive; recovery must retain this revision in its migration registry."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "0011_unified_inbox"
down_revision = "0010_whatsapp_keywords"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("inbox_replies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("account_key", sa.String(64), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("thread_sha256", sa.String(64), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(500)),
        sa.Column("body", sa.Text()),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(500)),
        sa.Column("reason", sa.String(60)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime()),
        sa.CheckConstraint("state IN ('queued','calling','accepted','bridge_accepted','rejected','outcome_unknown')", name="ck_inbox_reply_state"))
    op.create_index("ix_inbox_reply_thread", "inbox_replies", ["account_key", "thread_sha256", "created_at"])
    op.create_table("inbox_threads",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("thread_sha256", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("binding_sha256", sa.String(64), nullable=False),
        sa.Column("thread_id", sa.String(500), nullable=False),
        sa.Column("participant_id", sa.String(500), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("preview", sa.Text(), nullable=False),
        sa.Column("provider_updated_at", sa.String(100), nullable=False),
        sa.Column("messages", postgresql.JSONB(), nullable=False),
        sa.Column("has_more", sa.Boolean(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False))
    op.create_table("inbox_crm_links",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("thread_sha256", sa.String(64), primary_key=True),
        sa.Column("brand", sa.String(8), primary_key=True),
        sa.Column("account_identity_sha256", sa.String(64), nullable=False),
        sa.Column("participant_id", sa.String(500), nullable=False),
        sa.Column("contact_id", sa.String(36), nullable=False),
        sa.Column("workspace_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_error", sa.String(400)))
    op.create_table("inbox_crm_activity_receipts",
        sa.Column("account_key", sa.String(64), primary_key=True),
        sa.Column("thread_sha256", sa.String(64), primary_key=True),
        sa.Column("brand", sa.String(8), primary_key=True),
        sa.Column("message_sha256", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table("inbox_crm_activity_receipts")
    op.drop_table("inbox_crm_links")
    op.drop_table("inbox_threads")
    op.drop_table("inbox_replies")
