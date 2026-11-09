"""Property deals: one row per property, portfolio or site we may buy, stamped at each stage. Additive.

Revision ID: 0014_acquisitions
Revises: 0013_laps_board
Create Date: 2026-11-09
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014_acquisitions"
down_revision = "0013_laps_board"
branch_labels = None
depends_on = None

STAGES = ("lead", "appraisal", "offer", "agreed", "legals", "exchanged", "completed", "lost")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.create_table(
        "acquisitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("postcode", sa.String(10), nullable=False, server_default=""),
        sa.Column("kind", sa.String(12), nullable=False, server_default="other"),
        sa.Column("units", sa.Integer()),
        sa.Column("plan", sa.String(12), nullable=False, server_default="undecided"),
        sa.Column("seller_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("seller_contact", sa.String(200), nullable=False, server_default=""),
        sa.Column("seller_type", sa.String(20), nullable=False, server_default="unknown"),
        sa.Column("situation", sa.String(300), nullable=False, server_default=""),
        sa.Column("source", sa.String(100), nullable=False, server_default=""),
        sa.Column("stage", sa.String(12), nullable=False, server_default="lead"),
        *[sa.Column(name, sa.BigInteger()) for name in
          ("asking_pence", "offer_pence", "agreed_pence", "value_pence", "works_pence")],
        sa.Column("lead_at", sa.DateTime(), nullable=False),
        *[sa.Column(stage + "_at", sa.DateTime()) for stage in STAGES[1:]],
        sa.Column("lost_reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("target_completion", sa.Date()),
        sa.Column("solicitor", sa.String(200), nullable=False, server_default=""),
        sa.Column("next_step", sa.String(200), nullable=False, server_default=""),
        sa.Column("next_step_on", sa.Date()),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("checks", JSONB(), nullable=False, server_default="{}"),
        sa.Column("origin", sa.String(80)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_one_of("stage", STAGES), name="ck_acquisitions_stage"),
        sa.CheckConstraint(_one_of("kind", ("house", "flat", "hmo", "block", "portfolio", "commercial", "land", "other")),
                           name="ck_acquisitions_kind"),
        sa.CheckConstraint(_one_of("plan", ("undecided", "btl", "brr", "flip", "hmo", "development", "sourcing")),
                           name="ck_acquisitions_plan"),
        sa.CheckConstraint(_one_of("seller_type", ("unknown", "landlord", "portfolio_landlord", "owner", "developer",
                                                   "agent", "probate")), name="ck_acquisitions_seller_type"),
        sa.CheckConstraint("units IS NULL OR units >= 1", name="ck_acquisitions_units"),
    )
    op.create_index("ix_acquisitions_stage", "acquisitions", ["stage"])
    op.create_index("ux_acquisitions_origin", "acquisitions", ["origin"], unique=True,
                    postgresql_where=sa.text("origin IS NOT NULL"))


def downgrade() -> None:
    raise RuntimeError("Keep the additive property deals table when rolling application code back.")
