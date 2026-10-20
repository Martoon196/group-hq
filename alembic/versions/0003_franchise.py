"""Phase 4: territory model, franchise pipeline and seller commission ledger.

- model_runs, postcode_districts, assumptions: the territory model. One row per postcode district with
  the public data behind it (ONS Postcode Directory, Census 2021 TS054, ONS Price Index of Private Rents,
  PP address points), the modelled economics for its band, and who holds it (open, reserved, core, hq).
  Every model input lives in assumptions, seeded here from the plan and the operating model with
  confirmed = false, so the screens badge each one [CONFIRM] until the founder signs it off.
- franchise_opportunities, activities, tasks: the pipeline and the CRM minimum.
- sellers, commission_rules, commission_entries, statements: who sold a territory and what they are owed.
  commission_rules version 1 is the plan's proposal (mentees and franchisees 20% of the upfront fee plus
  10% of months 1 to 12 of the monthly fee; outsiders 10% of the upfront only), seeded with confirmed =
  false and "confirm=false" in the note: no pack may carry it until the founder confirms it.

The vocabularies and the assumption defaults are mirrored in app/models.py and
app/services/territory_rules.py; tests/test_territory_rules.py checks that this file agrees with them.

Revision ID: 0003_franchise
Revises: 0002_review_fixes
Create Date: 2026-10-20
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_franchise"
down_revision = "0002_review_fixes"
branch_labels = None
depends_on = None

NATIONS = ("england", "wales", "scotland", "northern_ireland")
BANDS = ("london", "major_city", "town", "rural")
DISTRICT_STATES = ("open", "reserved", "core", "hq")
LICENSING_SCHEMES = ("unknown", "mandatory_only", "additional", "selective", "selective_additional")
OPPORTUNITY_STAGES = ("enquiry", "discovery", "pack_sent", "follow_up", "agreement", "paid", "onboarding", "lost")
ACTIVITY_KINDS = ("call", "email", "note", "task_done")
TASK_STATES = ("open", "done", "skipped")
SELLER_KINDS = ("mentee", "franchisee", "outsider")
COMMISSION_BASES = ("upfront", "monthly")

# (key, value, note). Every one is a proposal: confirmed = false on insert. Mirrored by
# app.services.territory_rules.DEFAULT_ASSUMPTIONS, which is the copy the pure functions read.
ASSUMPTIONS: list[tuple[str, object, str]] = [
    ("capture_y1", 0.003, "Share of a district's private-rented households under management by the end of year one. "
                          "Plan, Phase 4: units_y1..y3 = prs_households x capture with capture 0.3% / 0.7% / 1.2% [CONFIRM]."),
    ("capture_y2", 0.007, "Share under management by the end of year two. Plan, Phase 4 [CONFIRM]."),
    ("capture_y3", 0.012, "Share under management by the end of year three. Plan, Phase 4 [CONFIRM]."),
    ("fee_cap", 150, "Full Management fee cap, pounds a month. The fee sheet confirmed on 19 October 2026 (12% capped at "
                     "150 a month) is a fact; using it as the achievable fee per managed unit in this model is the "
                     "proposal [CONFIRM]."),
    ("management_pct", 0.12, "Full Management fee as a share of rent. Fee sheet confirmed on 19 October 2026; its use as "
                             "the modelled fee per unit is the proposal [CONFIRM]."),
    ("cost_to_serve", 25, "HQ cost to serve per unit per month outside London. Operating model section 4.1: staff time for "
                          "rent, maintenance desk, statements, compliance. Replace with Swansea actuals [CONFIRM]."),
    ("cost_to_serve_london", 35, "HQ cost to serve per unit per month in London. Operating model section 4.1: higher for "
                                 "trades prices and tenant expectations [CONFIRM]."),
    ("portals", 550, "Area-attributable portal cost per month. Operating model section 4.1 [VERIFY: Rightmove's average "
                     "branch spend was 1,636 a month in its H1 2026 report; virtual-branch pricing is not published] [CONFIRM]."),
    ("ads", 760, "Ads per month at launch outside London (25 a day), rising to 1,000 at 60 units. Operating model "
                 "section 4.1. Year one uses the launch figure throughout; the rise at 60 units is not modelled [CONFIRM]."),
    ("ads_london", 1520, "Ads per month at launch in London (50 a day), rising to 2,000. Operating model section 4.1. Year one "
                         "uses the launch figure throughout; the rise at 60 units is not modelled [CONFIRM]."),
    ("letters", 300, "Ongoing letters and Land Registry lookups per month (100 letters, 7 a title). Operating model "
                     "section 4.1 [CONFIRM]."),
    ("systems_fee", 99, "Partner systems and brand fee per month, received by HQ. Offer architecture section 2C and "
                        "operating model section 4.1. The banded monthly fee replaces it for paying franchisees once the "
                        "pricing bands are confirmed [CONFIRM]."),
    ("systems_fee_from_month", 4, "The first month the systems fee is charged. Offer architecture section 2C: from month 4. The "
                                  "heads of terms (section 6) count from the fourth full calendar month after the effective date, "
                                  "which is month 5 for a mid-month start; the founder to pick one [CONFIRM]."),
    ("partner_split_own", 0.5, "Partner share of the net management fee on their own instructions. Operating model "
                               "section 4.1 and prospectus section 3 [CONFIRM]."),
    ("partner_split_hq_leads", 0.25, "Partner share on HQ leads in their territory that they service. Operating model "
                                     "section 4.1 and prospectus section 3 [CONFIRM]."),
    ("partner_own_lead_share", 0.8, "Share of a partner's units that are their own instructions; the rest are HQ leads at "
                                    "the lower split. Operating model section 4.1: assumed 80% of units, a 45% blend [CONFIRM]."),
    ("town_min_prs_households", 3000, "A district with at least this many private-rented households is a town; below it is "
                                      "rural, unless it is London or a core city. Plan, Phase 4 [CONFIRM]."),
    ("upfront_units", 30, "The portfolio size the upfront fee is priced at. Plan, Phase 4: 6 x modelled partner net monthly "
                          "earnings at 30 units [CONFIRM]."),
    ("upfront_multiplier", 6, "Months of modelled partner earnings the upfront fee represents. Plan, Phase 4 [CONFIRM]."),
    ("upfront_round_to", 500, "The upfront fee is rounded to the nearest 500. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_rural", 3000, "Minimum upfront fee, rural band. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_town", 6000, "Minimum upfront fee, town band. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_major_city", 10000, "Minimum upfront fee, major city band. Plan, Phase 4 [CONFIRM]."),
    ("upfront_floor_london", 15000, "Minimum upfront fee, London band. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_rural", 49, "Monthly fee, rural band, replacing the flat 99 systems fee for paying franchisees. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_town", 79, "Monthly fee, town band. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_major_city", 99, "Monthly fee, major city band. Plan, Phase 4 [CONFIRM]."),
    ("monthly_fee_london", 149, "Monthly fee, London band. Plan, Phase 4 [CONFIRM]."),
]

COMMISSION_RULES_V1_NOTE = ("confirm=false. Proposal from the plan (Phase 4, seller commission ledger), not approved by the "
                            "founder: no pack, link or conversation may quote it until it is confirmed.")
# (seller_kind, upfront_pct, trail_pct, trail_months)
COMMISSION_RULES_V1: list[tuple[str, str, str, int]] = [
    ("mentee", "0.2000", "0.1000", 12),
    ("franchisee", "0.2000", "0.1000", 12),
    ("outsider", "0.1000", "0.0000", 0),
]
COMMISSION_RULES_V1_ACTIVE_FROM = date(2026, 10, 20)


def _one_of(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    ]


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def upgrade() -> None:
    op.create_table(
        "model_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ran_at", sa.DateTime(), nullable=False),
        sa.Column("source_files", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("assumptions", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("districts", sa.Integer(), nullable=False, server_default="0"),
    )

    op.create_table(
        "postcode_districts",
        sa.Column("district", sa.String(4), primary_key=True),
        sa.Column("area", sa.String(2), nullable=False),
        sa.Column("nation", sa.String(20), nullable=False),
        sa.Column("la_gss", sa.String(9), nullable=True),
        sa.Column("la_name", sa.String(120), nullable=False, server_default=""),
        sa.Column("dwellings", sa.Integer(), nullable=True),
        sa.Column("prs_households", sa.Integer(), nullable=True),
        sa.Column("prs_share", sa.Numeric(6, 4), nullable=True),
        sa.Column("rent_1bed", sa.Numeric(8, 2), nullable=True),
        sa.Column("rent_2bed", sa.Numeric(8, 2), nullable=True),
        sa.Column("rent_3bed", sa.Numeric(8, 2), nullable=True),
        sa.Column("rent_date", sa.Date(), nullable=True),
        sa.Column("licensing", sa.String(24), nullable=False, server_default="unknown"),
        sa.Column("licensing_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("band", sa.String(12), nullable=False),
        sa.Column("achievable_fee", sa.Numeric(8, 2), nullable=True),
        sa.Column("cost_to_serve", sa.Numeric(8, 2), nullable=True),
        sa.Column("units_y1", sa.Integer(), nullable=True),
        sa.Column("units_y2", sa.Integer(), nullable=True),
        sa.Column("units_y3", sa.Integer(), nullable=True),
        sa.Column("partner_net_y1", sa.Numeric(10, 2), nullable=True),
        sa.Column("hq_contribution_y1", sa.Numeric(10, 2), nullable=True),
        sa.Column("state", sa.String(10), nullable=False, server_default="open"),
        sa.Column("partner_id", sa.Integer(), nullable=True),
        sa.Column("reserved_until", sa.Date(), nullable=True),
        sa.Column("model_run_id", sa.Integer(), sa.ForeignKey("model_runs.id", ondelete="SET NULL"), nullable=True),
        *_timestamps(),
        _one_of("nation", NATIONS, "ck_postcode_districts_nation"),
        _one_of("band", BANDS, "ck_postcode_districts_band"),
        _one_of("state", DISTRICT_STATES, "ck_postcode_districts_state"),
        _one_of("licensing", LICENSING_SCHEMES, "ck_postcode_districts_licensing"),
        sa.CheckConstraint("dwellings IS NULL OR dwellings >= 0", name="ck_postcode_districts_dwellings"),
        sa.CheckConstraint("prs_households IS NULL OR prs_households >= 0", name="ck_postcode_districts_prs_households"),
        sa.CheckConstraint("prs_share IS NULL OR (prs_share >= 0 AND prs_share <= 1)", name="ck_postcode_districts_prs_share"),
    )
    op.create_index("ix_postcode_districts_area", "postcode_districts", ["area"])
    op.create_index("ix_postcode_districts_state", "postcode_districts", ["state"])
    op.create_index("ix_postcode_districts_band", "postcode_districts", ["band"])
    op.create_index("ix_postcode_districts_partner_id", "postcode_districts", ["partner_id"])
    op.create_index("ix_postcode_districts_model_run_id", "postcode_districts", ["model_run_id"])

    assumptions = op.create_table(
        "assumptions",
        sa.Column("key", sa.String(60), primary_key=True),
        sa.Column("value", _jsonb(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("confirmed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "sellers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("code", sa.String(24), nullable=False),
        sa.Column("bank_reference", sa.String(120), nullable=False, server_default=""),
        sa.Column("self_billing_agreed_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_timestamps(),
        _one_of("kind", SELLER_KINDS, "ck_sellers_kind"),
        sa.UniqueConstraint("person_id", name="uq_sellers_person_id"),
    )
    op.create_index("ux_sellers_code_ci", "sellers", [sa.text("lower(code)")], unique=True)

    commission_rules = op.create_table(
        "commission_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("seller_kind", sa.String(12), nullable=False),
        sa.Column("upfront_pct", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("trail_pct", sa.Numeric(5, 4), nullable=False, server_default="0"),
        sa.Column("trail_months", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active_from", sa.Date(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _one_of("seller_kind", SELLER_KINDS, "ck_commission_rules_seller_kind"),
        sa.CheckConstraint("upfront_pct >= 0 AND upfront_pct <= 1", name="ck_commission_rules_upfront_pct"),
        sa.CheckConstraint("trail_pct >= 0 AND trail_pct <= 1", name="ck_commission_rules_trail_pct"),
        sa.CheckConstraint("trail_months >= 0", name="ck_commission_rules_trail_months"),
        sa.UniqueConstraint("version", "seller_kind", name="uq_commission_rules_version_kind"),
    )

    op.create_table(
        "statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("seller_id", sa.Integer(), sa.ForeignKey("sellers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("total_pence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("period_end >= period_start", name="ck_statements_period"),
        sa.UniqueConstraint("seller_id", "period_start", "period_end", name="uq_statements_seller_period"),
    )
    op.create_index("ix_statements_seller_id", "statements", ["seller_id"])

    op.create_table(
        "franchise_opportunities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("person_id", sa.Integer(), sa.ForeignKey("people.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("stage", sa.String(12), nullable=False, server_default="enquiry"),
        sa.Column("districts", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("band", sa.String(12), nullable=True),
        sa.Column("quoted_upfront_pence", sa.Integer(), nullable=True),
        sa.Column("quoted_monthly_pence", sa.Integer(), nullable=True),
        sa.Column("seller_id", sa.Integer(), sa.ForeignKey("sellers.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(60), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("stage_changed_at", sa.DateTime(), nullable=False),
        *_timestamps(),
        _one_of("stage", OPPORTUNITY_STAGES, "ck_franchise_opportunities_stage"),
        _one_of("band", BANDS, "ck_franchise_opportunities_band"),
        sa.CheckConstraint("quoted_upfront_pence IS NULL OR quoted_upfront_pence >= 0", name="ck_franchise_opportunities_upfront"),
        sa.CheckConstraint("quoted_monthly_pence IS NULL OR quoted_monthly_pence >= 0", name="ck_franchise_opportunities_monthly"),
    )
    op.create_index("ix_franchise_opportunities_person_id", "franchise_opportunities", ["person_id"])
    op.create_index("ix_franchise_opportunities_stage", "franchise_opportunities", ["stage"])
    op.create_index("ix_franchise_opportunities_seller_id", "franchise_opportunities", ["seller_id"])

    op.create_table(
        "activities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("franchise_opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("at", sa.DateTime(), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False, server_default=""),
        _one_of("kind", ACTIVITY_KINDS, "ck_activities_kind"),
    )
    op.create_index("ix_activities_opportunity_id", "activities", ["opportunity_id"])

    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("franchise_opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("due_on", sa.Date(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("template_key", sa.String(60), nullable=False, server_default=""),
        sa.Column("state", sa.String(10), nullable=False, server_default="open"),
        sa.Column("done_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _one_of("state", TASK_STATES, "ck_tasks_state"),
    )
    op.create_index("ix_tasks_opportunity_id", "tasks", ["opportunity_id"])
    op.create_index("ix_tasks_state_due_on", "tasks", ["state", "due_on"])

    op.create_table(
        "commission_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("opportunity_id", sa.Integer(), sa.ForeignKey("franchise_opportunities.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("seller_id", sa.Integer(), sa.ForeignKey("sellers.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("commission_rules.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("basis", sa.String(10), nullable=False),
        sa.Column("trail_month", sa.Integer(), nullable=True),
        sa.Column("basis_pence", sa.Integer(), nullable=False),
        sa.Column("amount_pence", sa.Integer(), nullable=False),
        sa.Column("earned_at", sa.DateTime(), nullable=False),
        sa.Column("cleared_at", sa.DateTime(), nullable=True),
        sa.Column("paid_at", sa.DateTime(), nullable=True),
        sa.Column("clawback_of", sa.Integer(), sa.ForeignKey("commission_entries.id", ondelete="SET NULL"), nullable=True),
        sa.Column("statement_id", sa.Integer(), sa.ForeignKey("statements.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        _one_of("basis", COMMISSION_BASES, "ck_commission_entries_basis"),
        sa.CheckConstraint("basis_pence >= 0", name="ck_commission_entries_basis_pence"),
        sa.CheckConstraint("(clawback_of IS NULL AND amount_pence >= 0) OR (clawback_of IS NOT NULL AND amount_pence <= 0)",
                           name="ck_commission_entries_clawback_sign"),
        sa.CheckConstraint("(basis = 'upfront' AND trail_month IS NULL) OR "
                           "(basis = 'monthly' AND trail_month IS NOT NULL AND trail_month >= 1)",
                           name="ck_commission_entries_trail_month"),
    )
    op.create_index("ix_commission_entries_opportunity_id", "commission_entries", ["opportunity_id"])
    op.create_index("ix_commission_entries_seller_id", "commission_entries", ["seller_id"])
    op.create_index("ix_commission_entries_statement_id", "commission_entries", ["statement_id"])
    op.create_index("ux_commission_entries_once", "commission_entries",
                    ["opportunity_id", "seller_id", "basis", sa.text("coalesce(trail_month, 0)")],
                    unique=True, postgresql_where=sa.text("clawback_of IS NULL"))

    now = _now()
    op.bulk_insert(assumptions, [
        {"key": key, "value": value, "note": note, "confirmed": False, "confirmed_at": None, "updated_at": now}
        for key, value, note in ASSUMPTIONS
    ])
    op.bulk_insert(commission_rules, [
        {"version": 1, "seller_kind": kind, "upfront_pct": upfront, "trail_pct": trail, "trail_months": months,
         "active_from": COMMISSION_RULES_V1_ACTIVE_FROM, "note": COMMISSION_RULES_V1_NOTE, "confirmed": False,
         "created_at": now}
        for kind, upfront, trail, months in COMMISSION_RULES_V1
    ])


def downgrade() -> None:
    op.drop_index("ux_commission_entries_once", table_name="commission_entries")
    op.drop_table("commission_entries")
    op.drop_table("tasks")
    op.drop_table("activities")
    op.drop_table("franchise_opportunities")
    op.drop_table("statements")
    op.drop_table("commission_rules")
    op.drop_index("ux_sellers_code_ci", table_name="sellers")
    op.drop_table("sellers")
    op.drop_table("assumptions")
    op.drop_table("postcode_districts")
    op.drop_table("model_runs")
