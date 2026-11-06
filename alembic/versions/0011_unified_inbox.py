"""Owner reply receipts. Additive; recovery must retain this revision in its migration registry."""
import sqlalchemy as sa
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
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(500)),
        sa.Column("reason", sa.String(60)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime()),
        sa.CheckConstraint("state IN ('calling','accepted','bridge_accepted','rejected','outcome_unknown')", name="ck_inbox_reply_state"))
    op.create_index("ix_inbox_reply_thread", "inbox_replies", ["account_key", "thread_sha256", "created_at"])


def downgrade():
    op.drop_table("inbox_replies")
