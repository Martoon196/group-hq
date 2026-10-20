"""Phase 4, franchise pipeline: money received for a territory sale.

The payments table of Phase 1 belongs to a mentee (payments.mentee_id is NOT NULL), and a territory buyer
need not be a mentee, so a territory's money has its own table. One row per fee received: the upfront fee
(basis upfront) or one month of the monthly fee (basis monthly, trail_month 1 upwards). As with mentee
payments, only a row with cleared_at set counts: the seller commission ledger (app.services.commission,
packet C) accrues on cleared rows and nothing else. refunded_at is set when the fee is paid back, which is
what a clawback inside 90 days hangs off (docs/commission-rules.md, section 4).

Amounts are an integer number of pence, as the opportunity's quote is (quoted_upfront_pence), so the
ledger never converts. At most one live upfront row and one live row per trail month per opportunity.

Revision ID: 0004_franchise_payments
Revises: 0003_franchise
Create Date: 2026-10-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_franchise_payments"
down_revision = "0003_franchise"
branch_labels = None
depends_on = None

FRANCHISE_PAYMENT_BASES = ("upfront", "monthly")
PAYMENT_METHODS = ("manual", "stripe_link", "invoice")


def _one_of(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


def upgrade() -> None:
    op.create_table(
        "franchise_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("franchise_opportunities.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("basis", sa.String(10), nullable=False),
        sa.Column("trail_month", sa.Integer(), nullable=True),
        sa.Column("amount_pence", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("refunded_at", sa.DateTime(), nullable=True),
        sa.Column("method", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("reference", sa.String(120), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        _one_of("basis", FRANCHISE_PAYMENT_BASES, "ck_franchise_payments_basis"),
        _one_of("method", PAYMENT_METHODS, "ck_franchise_payments_method"),
        sa.CheckConstraint("amount_pence >= 0", name="ck_franchise_payments_amount"),
        # As on commission_entries: the IS NOT NULL is needed because a CHECK that evaluates to NULL passes.
        sa.CheckConstraint("(basis = 'upfront' AND trail_month IS NULL) OR "
                           "(basis = 'monthly' AND trail_month IS NOT NULL AND trail_month >= 1)",
                           name="ck_franchise_payments_trail_month"),
    )
    op.create_index("ix_franchise_payments_opportunity_id", "franchise_payments", ["opportunity_id"])
    op.create_index("ux_franchise_payments_upfront_once", "franchise_payments", ["opportunity_id"], unique=True,
                    postgresql_where=sa.text("basis = 'upfront' AND refunded_at IS NULL"))
    op.create_index("ux_franchise_payments_month_once", "franchise_payments", ["opportunity_id", "trail_month"], unique=True,
                    postgresql_where=sa.text("basis = 'monthly' AND refunded_at IS NULL"))


def downgrade() -> None:
    op.drop_index("ux_franchise_payments_month_once", table_name="franchise_payments")
    op.drop_index("ux_franchise_payments_upfront_once", table_name="franchise_payments")
    op.drop_index("ix_franchise_payments_opportunity_id", table_name="franchise_payments")
    op.drop_table("franchise_payments")
