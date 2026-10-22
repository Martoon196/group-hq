"""Admin switches: a Live / Dry run switch per product, and a manual hold on a grant or on all of a person's access.

- app_settings: what the founder switches on the Settings screen, keyed by name with a JSON value. The only keys
  today are product_switch.<product> ("live" or "dry_run"). A product with no row follows CONNECTORS_DRY_RUN as
  before, and CONNECTORS_DRY_RUN=1 still holds every product in dry run whatever a row says.
- entitlement_grants.held_at / held_by / held_reason: Pause on a grant. The grant is revoked in its product and
  the recompute keeps it revoked until Resume clears the hold.
- people.access_held_at / access_held_by / access_held_reason: Pause all access on the mentee screen. Every grant
  the person holds, and any the rule would create, is kept revoked until it is resumed.

Every new column is nullable or has a default, so existing rows read as switched by the environment and not held.

Revision ID: 0006_admin_switches
Revises: 0005_district_cost_share
Create Date: 2026-10-22
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_admin_switches"
down_revision = "0005_district_cost_share"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False, server_default=""),
    )
    op.add_column("entitlement_grants", sa.Column("held_at", sa.DateTime(), nullable=True))
    op.add_column("entitlement_grants", sa.Column("held_by", sa.String(200), nullable=False, server_default=""))
    op.add_column("entitlement_grants", sa.Column("held_reason", sa.String(200), nullable=False, server_default=""))
    op.add_column("people", sa.Column("access_held_at", sa.DateTime(), nullable=True))
    op.add_column("people", sa.Column("access_held_by", sa.String(200), nullable=False, server_default=""))
    op.add_column("people", sa.Column("access_held_reason", sa.String(200), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("people", "access_held_reason")
    op.drop_column("people", "access_held_by")
    op.drop_column("people", "access_held_at")
    op.drop_column("entitlement_grants", "held_reason")
    op.drop_column("entitlement_grants", "held_by")
    op.drop_column("entitlement_grants", "held_at")
    op.drop_table("app_settings")
