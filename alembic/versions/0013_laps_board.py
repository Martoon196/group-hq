"""LAPS board: one row per prospect with the moment it reached each stage. Additive; no existing table changes.

Revision ID: 0013_laps_board
Revises: 0012_inbox_history
Create Date: 2026-11-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_laps_board"
down_revision = "0012_inbox_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "laps_deals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("contact", sa.String(200), nullable=False, server_default=""),
        sa.Column("brand", sa.String(8), nullable=False),
        sa.Column("offer", sa.String(120), nullable=False, server_default=""),
        sa.Column("source", sa.String(100), nullable=False, server_default=""),
        sa.Column("stage", sa.String(12), nullable=False, server_default="lead"),
        sa.Column("value_pence", sa.Integer()),
        sa.Column("lead_at", sa.DateTime(), nullable=False),
        sa.Column("appointment_at", sa.DateTime()),
        sa.Column("presentation_at", sa.DateTime()),
        sa.Column("sale_at", sa.DateTime()),
        sa.Column("lost_at", sa.DateTime()),
        sa.Column("next_step", sa.String(200), nullable=False, server_default=""),
        sa.Column("next_step_on", sa.Date()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("origin", sa.String(80)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("stage IN ('lead', 'appointment', 'presentation', 'sale', 'lost')", name="ck_laps_deals_stage"),
        sa.CheckConstraint("brand IN ('mhp', 'pp', 'kp', 'piuk', 'hnh', 'ordris')", name="ck_laps_deals_brand"),
        sa.CheckConstraint("value_pence IS NULL OR value_pence >= 0", name="ck_laps_deals_value"),
    )
    op.create_index("ix_laps_deals_stage", "laps_deals", ["stage"])
    op.create_index("ix_laps_deals_lead_at", "laps_deals", ["lead_at"])
    op.create_index("ux_laps_deals_origin", "laps_deals", ["origin"], unique=True,
                    postgresql_where=sa.text("origin IS NOT NULL"))


def downgrade() -> None:
    raise RuntimeError("Keep the additive LAPS table when rolling application code back.")
