"""Growth: the tables the Growth screens read, and the sales_attribution view.

- metric_sources: one health row per collector (seeded by app.seed, never overwritten once edited). The on/off
  switch itself is the app_settings row growth_source.<source>, like product_switch.<product>.
- metric_snapshots: daily grain, one row per (date, source, product, metric, dimension); the unique key makes a
  re-pull an upsert of the same row, so collecting twice leaves the same rows.
- ad_spend: campaign-grain cost and platform-reported results, one row per platform campaign per day.
- product_purchases: one row per purchase as each product's partner API reports it (GET .../purchases).
- promo_channel_map: promo code -> channel, platform and campaign, edited on the Sources screen; seeded empty.
- metric_runs: the health history, one row per collector run.
- sales_attribution (a view): each purchase (with its status: the screens count paid ones as sales) with one
  resolved channel, in this precedence: the promo code's map entry, else the last touch's utm_source/utm_medium,
  else an affiliate reference, else the first touch, else direct. The utm rules are UTM_RULES below, mirrored by app.services.growth.attribution (a test checks they
  agree), compiled into one CASE expression so the view needs nothing outside the database.

Every row is an aggregate, a campaign name, a hashed account key or a platform id: no email, no IP address.
Vocabularies are mirrored in app/models.py. Every NOT NULL column has a server default, so a row written outside
the ORM is still whole. downgrade() drops the view first, then the tables.

Revision ID: 0007_growth
Revises: 0006_admin_switches
Create Date: 2026-10-23
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_growth"
down_revision = "0006_admin_switches"
branch_labels = None
depends_on = None

# ---- vocabularies (mirrored in app/models.py) ----
SOURCES = ("ga4", "search_console", "google_ads", "meta_ads", "meta_pages_instagram", "linkedin", "tiktok",
           "stripe_mh196", "stripe_hnh", "brevo", "zernio", "clerk_ordris",
           "product_pp", "product_kp", "product_ordris", "product_hnh", "neon")
SOURCE_STATUSES = ("ok", "failing", "not_configured", "off", "never_run")
SOURCE_TIERS = ("hourly", "daily")
PRODUCTS = ("pp", "ordris", "kp", "hnh")
METRIC_PRODUCTS = ("pp", "kp", "ordris", "hnh", "group")
AD_PLATFORMS = ("google", "meta", "tiktok", "linkedin", "x")
PURCHASE_KINDS = ("subscription", "one_off", "renewal", "reservation", "instruction", "refund")
PURCHASE_STATUSES = ("paid", "refunded", "trialing")
CHANNELS = ("paid_social", "paid_search", "social", "email", "affiliate", "partner", "direct", "other")

# ---- the utm rules of the view, in order: (channel, utm_source values or None for any, utm_medium values or None).
# The first rule whose lists both match wins; a touch with a source or a medium that no rule matches is "other".
# Values are compared lower-cased and trimmed. Mirrored by app.services.growth.attribution.UTM_RULES.
UTM_RULES: tuple[tuple[str, tuple[str, ...] | None, tuple[str, ...] | None], ...] = (
    ("affiliate", ("affiliate",), None),
    ("affiliate", None, ("affiliate",)),
    ("partner", ("kpuk", "propertypredator", "hnh", "ordris"), None),
    ("paid_social", ("meta", "facebook", "fb", "instagram", "ig", "tiktok", "linkedin", "x", "twitter"),
     ("paid_social", "cpc", "ppc", "paid", "banner")),
    ("paid_search", None, ("cpc", "ppc", "paid_search")),
    ("paid_social", None, ("paid_social",)),
    ("email", None, ("email",)),
    ("email", ("brevo",), None),
    ("social", None, ("social", "organic_social")),
    ("social", ("zernio",), None),
)
# utm_source spellings that name the same platform as the ad_spend rows do.
PLATFORM_ALIASES: dict[str, str] = {"facebook": "meta", "fb": "meta", "instagram": "meta", "ig": "meta",
                                    "twitter": "x"}


def _one_of(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


def _jsonb() -> postgresql.JSONB:
    return postgresql.JSONB(astext_type=sa.Text())


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def channel_case(src: str, med: str) -> str:
    """UTM_RULES as one SQL CASE over two lower-cased, trimmed text expressions."""
    whens = []
    for channel, sources, mediums in UTM_RULES:
        parts = []
        if sources is not None:
            parts.append(f"{src} IN ({_sql_list(sources)})")
        if mediums is not None:
            parts.append(f"{med} IN ({_sql_list(mediums)})")
        whens.append(f"WHEN {' AND '.join(parts)} THEN '{channel}'")
    return "CASE " + " ".join(whens) + " ELSE 'other' END"


def platform_case(src: str) -> str:
    whens = " ".join(f"WHEN '{alias}' THEN '{name}'" for alias, name in PLATFORM_ALIASES.items())
    return f"CASE {src} {whens} ELSE {src} END"


def view_sql() -> str:
    lt_src, lt_med = "t.lt_src", "t.lt_med"
    ft_src, ft_med = "t.ft_src", "t.ft_med"
    return f"""
CREATE VIEW sales_attribution AS
WITH t AS (
    SELECT p.product, p.purchase_ref, p.occurred_at, p.kind, p.plan, p.amount_pence, p.status, p.promo_code,
           p.affiliate_ref,
           lower(btrim(coalesce(p.last_touch ->> 'utm_source', ''))) AS lt_src,
           lower(btrim(coalesce(p.last_touch ->> 'utm_medium', ''))) AS lt_med,
           btrim(coalesce(p.last_touch ->> 'utm_campaign', '')) AS lt_campaign,
           lower(btrim(coalesce(p.first_touch ->> 'utm_source', ''))) AS ft_src,
           lower(btrim(coalesce(p.first_touch ->> 'utm_medium', ''))) AS ft_med,
           btrim(coalesce(p.first_touch ->> 'utm_campaign', '')) AS ft_campaign,
           m.channel AS map_channel, m.platform AS map_platform, m.campaign_name AS map_campaign
    FROM product_purchases p
    LEFT JOIN promo_channel_map m ON m.product = p.product AND m.code = p.promo_code AND p.promo_code <> ''
), b AS (
    SELECT t.*,
           CASE WHEN t.map_channel IS NOT NULL THEN 'promo'
                WHEN {lt_src} <> '' OR {lt_med} <> '' THEN 'last_touch'
                WHEN t.affiliate_ref <> '' THEN 'affiliate'
                WHEN {ft_src} <> '' OR {ft_med} <> '' THEN 'first_touch'
                ELSE 'direct' END AS attribution_basis
    FROM t
)
SELECT b.product, b.purchase_ref, b.occurred_at, b.kind, b.plan, b.amount_pence, b.status, b.promo_code,
       b.attribution_basis,
       CASE b.attribution_basis
            WHEN 'promo' THEN b.map_channel
            WHEN 'last_touch' THEN {channel_case('b.lt_src', 'b.lt_med')}
            WHEN 'affiliate' THEN 'affiliate'
            WHEN 'first_touch' THEN {channel_case('b.ft_src', 'b.ft_med')}
            ELSE 'direct' END AS channel,
       CASE b.attribution_basis
            WHEN 'promo' THEN b.map_platform
            WHEN 'last_touch' THEN {platform_case('b.lt_src')}
            WHEN 'first_touch' THEN {platform_case('b.ft_src')}
            ELSE '' END AS platform,
       CASE b.attribution_basis
            WHEN 'promo' THEN b.map_campaign
            WHEN 'last_touch' THEN b.lt_campaign
            WHEN 'first_touch' THEN b.ft_campaign
            ELSE '' END AS campaign_name
FROM b
"""


def upgrade() -> None:
    op.create_table(
        "metric_sources",
        sa.Column("source", sa.String(40), primary_key=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="never_run"),
        sa.Column("tier", sa.String(10), nullable=False, server_default="daily"),
        sa.Column("credential_env", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("config", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_ok_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=False, server_default=""),
        sa.Column("last_rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_due_at", sa.DateTime(), nullable=True),
        sa.Column("updated_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        _one_of("source", SOURCES, "ck_metric_sources_source"),
        _one_of("status", SOURCE_STATUSES, "ck_metric_sources_status"),
        _one_of("tier", SOURCE_TIERS, "ck_metric_sources_tier"),
    )

    op.create_table(
        "metric_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("product", sa.String(10), nullable=False),
        sa.Column("metric", sa.String(60), nullable=False),
        sa.Column("dimension", sa.String(200), nullable=False, server_default=""),
        sa.Column("value", sa.Numeric(18, 4), nullable=False, server_default="0"),
        sa.Column("meta", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("pulled_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        _one_of("source", SOURCES, "ck_metric_snapshots_source"),
        _one_of("product", METRIC_PRODUCTS, "ck_metric_snapshots_product"),
        sa.UniqueConstraint("date", "source", "product", "metric", "dimension", name="uq_metric_snapshots_key"),
    )
    op.create_index("ix_metric_snapshots_product_metric_date", "metric_snapshots", ["product", "metric", "date"])
    op.create_index("ix_metric_snapshots_source_date", "metric_snapshots", ["source", "date"])

    op.create_table(
        "ad_spend",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False),
        sa.Column("account_id", sa.String(60), nullable=False, server_default=""),
        sa.Column("campaign_id", sa.String(80), nullable=False, server_default=""),
        sa.Column("campaign_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("product", sa.String(10), nullable=False, server_default="group"),
        sa.Column("spend_pence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("impressions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conversions", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("conversions_sign_up", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("conversions_lead", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("conversions_purchase", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("conversions_trial", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("conversion_value_pence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("pulled_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        _one_of("platform", AD_PLATFORMS, "ck_ad_spend_platform"),
        _one_of("product", METRIC_PRODUCTS, "ck_ad_spend_product"),
        sa.UniqueConstraint("platform", "account_id", "campaign_id", "date", name="uq_ad_spend_key"),
    )
    op.create_index("ix_ad_spend_product_date", "ad_spend", ["product", "date"])
    op.create_index("ix_ad_spend_campaign_name_date", "ad_spend", ["campaign_name", "date"])

    op.create_table(
        "product_purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product", sa.String(10), nullable=False),
        sa.Column("purchase_ref", sa.String(120), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("plan", sa.String(80), nullable=False, server_default=""),
        sa.Column("amount_pence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="gbp"),
        sa.Column("status", sa.String(20), nullable=False, server_default="paid"),
        sa.Column("promo_code", sa.String(40), nullable=False, server_default=""),
        sa.Column("affiliate_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("first_touch", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_touch", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("account_key", sa.String(64), nullable=False, server_default=""),
        sa.Column("stripe_customer_id", sa.String(60), nullable=False, server_default=""),
        sa.Column("raw", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("pulled_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        _one_of("product", PRODUCTS, "ck_product_purchases_product"),
        _one_of("kind", PURCHASE_KINDS, "ck_product_purchases_kind"),
        _one_of("status", PURCHASE_STATUSES, "ck_product_purchases_status"),
        sa.UniqueConstraint("product", "purchase_ref", name="uq_product_purchases_ref"),
    )
    op.create_index("ix_product_purchases_product_occurred_at", "product_purchases", ["product", "occurred_at"])

    op.create_table(
        "promo_channel_map",
        sa.Column("code", sa.String(40), primary_key=True),
        sa.Column("product", sa.String(10), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("platform", sa.String(20), nullable=False, server_default=""),
        sa.Column("campaign_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("note", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        _one_of("product", PRODUCTS, "ck_promo_channel_map_product"),
        _one_of("channel", CHANNELS, "ck_promo_channel_map_channel"),
    )

    op.create_table(
        "metric_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("tier", sa.String(10), nullable=False, server_default="daily"),
        sa.Column("started_at", sa.DateTime(), nullable=False, server_default=sa.text("(now() at time zone 'utc')")),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(500), nullable=False, server_default=""),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("actor", sa.String(200), nullable=False, server_default=""),
        sa.Column("plan", _jsonb(), nullable=True),
        _one_of("source", SOURCES, "ck_metric_runs_source"),
        _one_of("tier", SOURCE_TIERS, "ck_metric_runs_tier"),
    )
    op.create_index("ix_metric_runs_source_started_at", "metric_runs", ["source", "started_at"])

    op.execute(view_sql())


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS sales_attribution")
    op.drop_index("ix_metric_runs_source_started_at", table_name="metric_runs")
    op.drop_table("metric_runs")
    op.drop_table("promo_channel_map")
    op.drop_index("ix_product_purchases_product_occurred_at", table_name="product_purchases")
    op.drop_table("product_purchases")
    op.drop_index("ix_ad_spend_campaign_name_date", table_name="ad_spend")
    op.drop_index("ix_ad_spend_product_date", table_name="ad_spend")
    op.drop_table("ad_spend")
    op.drop_index("ix_metric_snapshots_source_date", table_name="metric_snapshots")
    op.drop_index("ix_metric_snapshots_product_metric_date", table_name="metric_snapshots")
    op.drop_table("metric_snapshots")
    op.drop_table("metric_sources")
