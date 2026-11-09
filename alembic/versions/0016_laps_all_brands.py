"""CRM leads for every connected business, what the CRM knows about each one's deal, and LAPS cards for one person
under several businesses. Additive: new nullable columns and a wider brand check.

Revision ID: 0016_laps_all_brands
Revises: 0015_acquisition_threads
Create Date: 2026-11-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_laps_all_brands"
down_revision = "0015_acquisition_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_campaign_leads_brand", "campaign_leads", type_="check")
    op.create_check_constraint("ck_campaign_leads_brand", "campaign_leads", "brand IN ('pp', 'mhp', 'kp', 'piuk')")
    op.add_column("campaign_leads", sa.Column("source_created_at", sa.String(50)))
    op.add_column("campaign_leads", sa.Column("opportunity_status", sa.String(10)))
    op.add_column("campaign_leads", sa.Column("value_minor", sa.BigInteger()))
    op.add_column("campaign_leads", sa.Column("stage_changed_at", sa.String(50)))
    op.add_column("laps_deals", sa.Column("party", sa.String(36)))
    op.create_index("ix_laps_deals_party", "laps_deals", ["party"], postgresql_where=sa.text("party IS NOT NULL"))


def downgrade() -> None:
    raise RuntimeError("Keep the additive columns when rolling application code back; rows for the newer brands "
                       "would fail the narrower check.")
