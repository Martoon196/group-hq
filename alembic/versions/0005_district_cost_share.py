"""Phase 4, territory model: the share of its territory's fixed costs that a district's year-one figures carry.

The operating model (section 4.1) gives portals, ads and letters as fixed costs per AREA and the systems fee
as one fee per partner. The loader apportions both across the districts of a territory by units_y1 (the
entry in data/current_territories.json that lists the district, or the postcode area when none does) and
stores the share it used here, so the district screen can say "carries 23% of the territory's fixed costs
and systems fee" and recompute the stored figures on the same basis. NULL means the row was loaded before
this column existed; the screens read that as 1 (the whole area in one district), which is what the first
loader wrote.

Revision ID: 0005_district_cost_share
Revises: 0004_franchise_payments
Create Date: 2026-10-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_district_cost_share"
down_revision = "0004_franchise_payments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("postcode_districts", sa.Column("cost_share", sa.Numeric(6, 4), nullable=True))
    op.create_check_constraint("ck_postcode_districts_cost_share", "postcode_districts",
                               "cost_share IS NULL OR (cost_share >= 0 AND cost_share <= 1)")


def downgrade() -> None:
    op.drop_constraint("ck_postcode_districts_cost_share", "postcode_districts", type_="check")
    op.drop_column("postcode_districts", "cost_share")
