"""The metric registry: every metric name a collector may write to metric_snapshots, its unit and which sources write it.

The store refuses a row whose metric is not registered here, or whose source is not one of the metric's sources, so a
typo in a collector fails its test instead of writing a name no screen reads. Money metrics end in _pence and hold
pence. The Neon metrics keep Neon's own units (CU-seconds, byte-months, bytes, branch-hours, exactly as the
consumption API returns them); NEON_UNITS turns them into the billing units the Costs screen prices.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str            # count, pence, ratio, position, or a Neon unit
    description: str
    sources: tuple[str, ...]


PRODUCT_SOURCES = ("product_pp", "product_kp", "product_ordris", "product_hnh")
STRIPE = ("stripe_mh196", "stripe_hnh")
SOCIAL = ("zernio", "meta_pages_instagram", "linkedin")
ADS = ("google_ads", "meta_ads", "linkedin", "tiktok", "zernio")

METRICS: tuple[Metric, ...] = (
    # Visitors (GA4)
    Metric("visitors", "count", "GA4 activeUsers by day (a daily figure: days do not sum to unique people)", ("ga4",)),
    Metric("sessions", "count", "GA4 sessions", ("ga4",)),
    Metric("new_users", "count", "GA4 newUsers", ("ga4",)),
    Metric("engaged_sessions", "count", "GA4 engagedSessions", ("ga4",)),
    Metric("realtime_users", "count", "GA4 activeUsers in the last 30 minutes (hourly tier only)", ("ga4",)),
    Metric("quota_remaining", "count", "GA4 Data API tokens left today for the property", ("ga4",)),
    # The product's own ledger (and the pixel's view beside it)
    Metric("sign_ups", "count", "accounts created that day, from the product's own records", PRODUCT_SOURCES + ("clerk_ordris",)),
    Metric("sign_ups_pixel", "count", "GA4 key event sign_up (consenting visitors only)", ("ga4",)),
    Metric("affiliate_sign_ups", "count", "sign-ups that arrived with an affiliate reference", PRODUCT_SOURCES),
    Metric("leads", "count", "leads that day, from the product's own records", PRODUCT_SOURCES),
    Metric("leads_pixel", "count", "GA4 key event generate_lead", ("ga4",)),
    Metric("trials_started", "count", "trials started that day (the product's ledger, or GA4's trial_start)", PRODUCT_SOURCES + ("ga4",)),
    Metric("trials_active", "count", "trials running at the end of the day", PRODUCT_SOURCES + STRIPE),
    Metric("paying_active", "count", "subscriptions active or past due at the end of the day", PRODUCT_SOURCES + STRIPE),
    Metric("paying_new", "count", "subscriptions that started paying that day", PRODUCT_SOURCES + STRIPE),
    Metric("cancelled", "count", "subscriptions cancelled that day", PRODUCT_SOURCES + STRIPE),
    Metric("mrr_pence", "pence", "monthly recurring revenue at the end of the day", PRODUCT_SOURCES + STRIPE),
    Metric("revenue_pence", "pence", "money taken that day (Stripe: gross charges; GA4: purchaseRevenue the pixel saw)",
           PRODUCT_SOURCES + STRIPE + ("ga4",)),
    Metric("refunds_pence", "pence", "money refunded that day", PRODUCT_SOURCES + STRIPE),
    Metric("purchases", "count", "purchases that day (the product's ledger, or completed Stripe Checkout Sessions)",
           PRODUCT_SOURCES + STRIPE),
    Metric("purchases_pixel", "count", "GA4 key event purchase", ("ga4",)),
    Metric("promo_redemptions", "count", "promo codes redeemed that day, dimension promo:<CODE>", PRODUCT_SOURCES),
    Metric("promo_times_redeemed", "count", "a Stripe promotion code's times_redeemed so far (cumulative)", STRIPE),
    Metric("reservations", "count", "Key Properties deposits paid that day", PRODUCT_SOURCES),
    Metric("instructions", "count", "instructions that day (Hello New Home landlords; Ordris instruction fees)", PRODUCT_SOURCES),
    # Paid media (campaign grain lives in ad_spend; these are for platforms that only give account totals)
    Metric("spend_pence", "pence", "ad spend", ADS),
    Metric("impressions", "count", "ad impressions", ADS),
    Metric("clicks", "count", "ad clicks", ADS),
    Metric("conversions", "count", "platform-reported conversions", ADS),
    Metric("conversion_value_pence", "pence", "platform-reported conversion value", ADS),
    # Search (Search Console; ctr and position are recomputed from clicks and impressions on roll-up)
    Metric("search_clicks", "count", "clicks from Google Search", ("search_console",)),
    Metric("search_impressions", "count", "impressions in Google Search", ("search_console",)),
    Metric("search_ctr", "ratio", "clicks / impressions for the row, as Google gives it", ("search_console",)),
    Metric("search_position", "position", "average position for the row, as Google gives it", ("search_console",)),
    # Email (Brevo counts only, never per person)
    Metric("email_contacts", "count", "contacts on a list (uniqueSubscribers)", ("brevo",)),
    Metric("email_contacts_new", "count", "contacts created that day", ("brevo",)),
    Metric("email_unsubscribed", "count", "unsubscribes", ("brevo",)),
    Metric("email_sent", "count", "emails sent (campaigns) or requested (transactional)", ("brevo",)),
    Metric("email_delivered", "count", "emails delivered", ("brevo",)),
    Metric("email_opens_unique", "count", "unique opens", ("brevo",)),
    Metric("email_clicks_unique", "count", "unique clicks", ("brevo",)),
    Metric("email_bounces", "count", "hard and soft bounces", ("brevo",)),
    Metric("email_spam_reports", "count", "spam complaints", ("brevo",)),
    # Social (Zernio; Meta and LinkedIn when their collectors run)
    Metric("followers", "count", "followers at the end of the day", SOCIAL),
    Metric("followers_new", "count", "net new followers that day", SOCIAL),
    Metric("posts", "count", "posts published that day", SOCIAL),
    Metric("post_impressions", "count", "impressions", SOCIAL),
    Metric("post_reach", "count", "reach (not additive across days)", SOCIAL),
    Metric("post_engagements", "count", "likes, comments, shares and saves", SOCIAL),
    Metric("post_clicks", "count", "link clicks", SOCIAL),
    Metric("video_views", "count", "video views", SOCIAL),
    # Running costs (Neon consumption, per project per day, Neon's own units)
    Metric("neon_compute_unit_seconds", "cu_seconds", "compute, CU-seconds", ("neon",)),
    Metric("neon_root_branch_bytes_month", "byte_months", "root branch storage, byte-months", ("neon",)),
    Metric("neon_child_branch_bytes_month", "byte_months", "child branch storage, byte-months", ("neon",)),
    Metric("neon_instant_restore_bytes_month", "byte_months", "instant restore (history) storage, byte-months", ("neon",)),
    Metric("neon_snapshot_storage_bytes_month", "byte_months", "snapshot storage, byte-months", ("neon",)),
    Metric("neon_public_network_transfer_bytes", "bytes", "public network transfer, bytes", ("neon",)),
    Metric("neon_private_network_transfer_bytes", "bytes", "private network transfer, bytes", ("neon",)),
    Metric("neon_extra_branches_month", "branch_hours", "branches beyond the plan's allowance, branch-hours", ("neon",)),
)

BY_NAME: dict[str, Metric] = {m.name: m for m in METRICS}


@dataclass(frozen=True)
class NeonUnit:
    """How one Neon consumption metric becomes the unit Neon bills it in (neon.com/docs/introduction/usage-calculations)."""
    metric: str          # our metric name
    api_name: str        # the consumption API's metric_name
    billing_unit: str    # what a rate is per
    divisor: Decimal     # raw value / divisor = billing units
    label: str


NEON_UNITS: tuple[NeonUnit, ...] = (
    NeonUnit("neon_compute_unit_seconds", "compute_unit_seconds", "CU-hour", Decimal(3600), "Compute"),
    NeonUnit("neon_root_branch_bytes_month", "root_branch_bytes_month", "GB-month", Decimal(10) ** 9, "Storage, root branches"),
    NeonUnit("neon_child_branch_bytes_month", "child_branch_bytes_month", "GB-month", Decimal(10) ** 9, "Storage, child branches"),
    NeonUnit("neon_instant_restore_bytes_month", "instant_restore_bytes_month", "GB-month", Decimal(10) ** 9, "Instant restore"),
    NeonUnit("neon_snapshot_storage_bytes_month", "snapshot_storage_bytes_month", "GB-month", Decimal(10) ** 9, "Snapshots"),
    NeonUnit("neon_public_network_transfer_bytes", "public_network_transfer_bytes", "GB", Decimal(10) ** 9, "Public network transfer"),
    NeonUnit("neon_private_network_transfer_bytes", "private_network_transfer_bytes", "GB", Decimal(10) ** 9, "Private network transfer"),
    NeonUnit("neon_extra_branches_month", "extra_branches_month", "branch-month", Decimal(744), "Extra branches"),
)
NEON_BY_API: dict[str, NeonUnit] = {u.api_name: u for u in NEON_UNITS}
NEON_BY_METRIC: dict[str, NeonUnit] = {u.metric: u for u in NEON_UNITS}


def is_registered(metric: str, source: str | None = None) -> bool:
    entry = BY_NAME.get(metric)
    if entry is None:
        return False
    return source is None or source in entry.sources


def unit_of(metric: str) -> str:
    entry = BY_NAME.get(metric)
    return entry.unit if entry else ""


def is_money(metric: str) -> bool:
    return metric.endswith("_pence")
