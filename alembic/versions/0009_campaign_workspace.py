"""Founder campaign projections and internal action queue; no provider effects.

Revision ID: 0009_campaign_workspace
Revises: 0008_mentee_earnings
Create Date: 2026-10-30
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_campaign_workspace"
down_revision = "0008_mentee_earnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("brand", sa.String(8), nullable=False),
        sa.Column("source_system", sa.String(30), nullable=False),
        sa.Column("external_id", sa.String(80), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("source", sa.String(100)),
        sa.Column("lifecycle", sa.String(20), nullable=False),
        sa.Column("stage", sa.String(100)),
        sa.Column("next_task_at", sa.String(50)),
        sa.Column("last_activity_at", sa.String(50)),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("brand IN ('pp', 'mhp')", name="ck_campaign_leads_brand"),
        sa.UniqueConstraint("brand", "source_system", "external_id", name="uq_campaign_leads_source"),
    )
    op.create_index("ix_campaign_leads_brand_synced", "campaign_leads", ["brand", "synced_at"])
    op.create_table(
        "campaign_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("lead_id", sa.Integer(), sa.ForeignKey("campaign_leads.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("blueprint_key", sa.String(60), nullable=False),
        sa.Column("blueprint_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(12), nullable=False),
        sa.Column("hold_reason", sa.String(500), nullable=False),
        sa.Column("held_at", sa.DateTime()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("state IN ('active', 'held', 'completed')", name="ck_campaign_plans_state"),
        sa.UniqueConstraint("lead_id", "blueprint_key", "blueprint_version", name="uq_campaign_plans_version"),
    )
    op.create_index("ix_campaign_plans_state", "campaign_plans", ["state"])
    op.create_table(
        "campaign_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("campaign_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_key", sa.String(60), nullable=False),
        sa.Column("channel", sa.String(10), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("guidance", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(10), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("completed_by", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("channel IN ('call', 'email', 'sms', 'whatsapp')", name="ck_campaign_actions_channel"),
        sa.CheckConstraint("state IN ('open', 'done', 'skipped')", name="ck_campaign_actions_state"),
        sa.UniqueConstraint("plan_id", "step_key", name="uq_campaign_actions_step"),
    )
    op.create_index("ix_campaign_actions_state_due", "campaign_actions", ["state", "due_at"])
    op.create_table(
        "campaign_activities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("plan_id", sa.Integer(), sa.ForeignKey("campaign_plans.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_id", sa.Integer(), sa.ForeignKey("campaign_actions.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_campaign_activities_plan_at", "campaign_activities", ["plan_id", "at"])


def downgrade() -> None:
    op.drop_index("ix_campaign_activities_plan_at", table_name="campaign_activities")
    op.drop_table("campaign_activities")
    op.drop_index("ix_campaign_actions_state_due", table_name="campaign_actions")
    op.drop_table("campaign_actions")
    op.drop_index("ix_campaign_plans_state", table_name="campaign_plans")
    op.drop_table("campaign_plans")
    op.drop_index("ix_campaign_leads_brand_synced", table_name="campaign_leads")
    op.drop_table("campaign_leads")
