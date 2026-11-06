"""Durable group-keyword intake/outbox, separate from canonical CRM identity.

Revision ID: 0010_whatsapp_keywords
Revises: 0009_campaign_workspace
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_whatsapp_keywords"
down_revision = "0009_campaign_workspace"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("whatsapp_keyword_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("rule_revision", sa.Integer(), nullable=False),
        sa.Column("brand", sa.String(8), nullable=False),
        sa.Column("sender_sha256", sa.String(64), nullable=False),
        sa.Column("sender_phone", sa.String(15), nullable=False),
        sa.Column("sender_name", sa.String(200), nullable=False),
        sa.Column("sender_id", sa.String(40), nullable=False),
        sa.Column("group_id", sa.String(90), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("provider_message_id", sa.String(250)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("begun_at", sa.DateTime()), sa.Column("settled_at", sa.DateTime()),
        sa.UniqueConstraint("rule_id", "sender_sha256", name="uq_keyword_rule_sender"),
        sa.CheckConstraint("state IN ('pending','calling','sent','outcome_unknown','held')", name="ck_keyword_state"),
        sa.CheckConstraint("brand IN ('pp','mhp','kp','hnh','ordris')", name="ck_keyword_brand"))
    op.create_table("whatsapp_keyword_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("listener_id", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(200), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("whatsapp_keyword_deliveries.id", ondelete="RESTRICT")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("listener_id", "external_id", name="uq_keyword_external_event"))
    op.create_table("whatsapp_keyword_suppressions",
        sa.Column("sender_sha256", sa.String(64), primary_key=True),
        sa.Column("blocked", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table("whatsapp_keyword_suppressions")
    op.drop_table("whatsapp_keyword_events")
    op.drop_table("whatsapp_keyword_deliveries")
