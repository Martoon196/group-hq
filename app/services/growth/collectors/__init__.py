"""The growth collectors and the registry the job and the Sources screen pick them from.

    collector = COLLECTORS["ga4"].from_config(dry_run=source_dry_run(args.dry_run))
    result = collector.pull(since, until)        # PullResult(rows, plans, notes)

SOURCE_INFO describes every source in models.SOURCES, built or not: its name, the products it covers, and for the
ones not built yet (Google Ads, Meta, LinkedIn, TikTok) what they wait on. Every source starts Off: its switch is the
app_settings row growth_source.<source>, set on the Sources screen, and a source that is On must be configured in the
environment of whatever runs it, or the job fails its run (exit 1) and says which variable is missing.
"""
from __future__ import annotations

from typing import Mapping

from .... import config
from ....models import SOURCES
from .base import MetricCollector, PullResult, PurchaseRow, SnapshotRow, SpendRow, scrub, source_dry_run
from .brevo import BrevoCollector
from .clerk import ClerkOrdrisCollector
from .ga4 import GA4Collector
from .neon import NeonCollector
from .products import ProductHNHCollector, ProductKPCollector, ProductOrdrisCollector, ProductPPCollector
from .search_console import SearchConsoleCollector
from .stripe_read import StripeHNHCollector, StripeMH196Collector
from .zernio import ZernioCollector

COLLECTORS: dict[str, type[MetricCollector]] = {
    "ga4": GA4Collector,
    "search_console": SearchConsoleCollector,
    "stripe_mh196": StripeMH196Collector,
    "stripe_hnh": StripeHNHCollector,
    "brevo": BrevoCollector,
    "zernio": ZernioCollector,
    "clerk_ordris": ClerkOrdrisCollector,
    "product_pp": ProductPPCollector,
    "product_kp": ProductKPCollector,
    "product_ordris": ProductOrdrisCollector,
    "product_hnh": ProductHNHCollector,
    "neon": NeonCollector,
}

SWITCH_MODES = ("on", "off")
DEFAULT_SWITCH = "off"

SOURCE_INFO: dict[str, dict] = {
    "ga4": {"name": "Google Analytics 4", "scope": "all four sites", "tier": "daily"},
    "search_console": {"name": "Search Console", "scope": "all four sites", "tier": "daily"},
    "google_ads": {"name": "Google Ads", "scope": "paid search", "tier": "daily",
                   "waiting": "packet 3: waits on the Google Ads API Explorer access; until then import a CSV below"},
    "meta_ads": {"name": "Meta ads", "scope": "Facebook and Instagram ads", "tier": "daily",
                 "waiting": "packet 2: Meta ads insights; until then import a CSV below"},
    "meta_pages_instagram": {"name": "Meta Pages and Instagram", "scope": "organic", "tier": "daily",
                             "waiting": "packet 2, optional: Zernio already covers these accounts"},
    "linkedin": {"name": "LinkedIn", "scope": "ads and the company page", "tier": "daily",
                 "waiting": "awaiting LinkedIn Advertising API approval (packet 4); until then import a Campaign Manager CSV below"},
    "tiktok": {"name": "TikTok ads", "scope": "paid social", "tier": "daily",
               "waiting": "packet 4: waits on TikTok Marketing API review; until then Zernio's ad timeline or a CSV"},
    "stripe_mh196": {"name": "Stripe, MH196 2026 Ltd", "scope": "Property Predator, Ordris, Key Properties", "tier": "hourly"},
    "stripe_hnh": {"name": "Stripe, Hello New Home 2 Ltd", "scope": "Hello New Home (no account yet)", "tier": "hourly"},
    "brevo": {"name": "Brevo", "scope": "email", "tier": "daily"},
    "zernio": {"name": "Zernio", "scope": "organic social, all accounts", "tier": "daily"},
    "clerk_ordris": {"name": "Clerk (Ordris sign-ups)", "scope": "Ordris", "tier": "daily",
                     "warning": "Not recommended: the key can delete Ordris users. Use only until Ordris's own metrics "
                                "endpoint answers, then switch it off and remove the key."},
    "product_pp": {"name": "Property Predator ledger", "scope": "Property Predator", "tier": "hourly"},
    "product_kp": {"name": "Key Properties ledger", "scope": "Key Properties", "tier": "hourly"},
    "product_ordris": {"name": "Ordris ledger", "scope": "Ordris", "tier": "hourly"},
    "product_hnh": {"name": "Hello New Home ledger", "scope": "Hello New Home", "tier": "hourly"},
    "neon": {"name": "Neon (running costs)", "scope": "every database in the Neon organisation", "tier": "daily"},
}
assert set(SOURCE_INFO) == set(SOURCES), "every source needs its SOURCE_INFO entry"

# Env names for the sources not built yet, so the Sources screen can say what they will need.
PLANNED_ENV: dict[str, tuple[str, ...]] = {
    "google_ads": ("GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET", "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_CUSTOMER_ID"),
    "meta_ads": ("META_READ_TOKEN", "META_AD_ACCOUNT_IDS", "META_API_VERSION"),
    "meta_pages_instagram": ("META_READ_TOKEN", "META_PAGE_IDS", "META_IG_USER_IDS"),
    "linkedin": ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET", "LINKEDIN_ACCESS_TOKEN", "LINKEDIN_REFRESH_TOKEN",
                 "LINKEDIN_AD_ACCOUNT_ID", "LINKEDIN_ORG_ID", "LINKEDIN_API_VERSION"),
    "tiktok": ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_ADVERTISER_ID"),
}


def env_names(source: str) -> tuple[str, ...]:
    cls = COLLECTORS.get(source)
    return cls.env_names if cls is not None else PLANNED_ENV.get(source, ())


def tier_of(source: str) -> str:
    return SOURCE_INFO[source]["tier"]


def runs_in(source: str, tier: str) -> bool:
    """Does the job's tier run this source? "all" runs every built source on its own tier."""
    cls = COLLECTORS.get(source)
    if cls is None:
        return False
    return tier == "all" or tier in cls.tiers


def _switched(source: str, switches: Mapping[str, str] | None) -> str:
    value = (switches or {}).get(source, "")
    return value if value in SWITCH_MODES else ""


def source_mode(source: str, *, switches: Mapping[str, str] | None = None) -> dict:
    """How a source is treated now, for the screens and the job. state is not_built, off, not_configured, dry_run or
    live; switch is "on" or "off" (DEFAULT_SWITCH when never set); missing lists the env names this server lacks."""
    switch = _switched(source, switches) or DEFAULT_SWITCH
    cls = COLLECTORS.get(source)
    if cls is None:
        return {"source": source, "state": "not_built", "switch": switch, "configured": False, "missing": [],
                "set_switch": _switched(source, switches)}
    collector = cls.from_config(dry_run=True)          # no client is opened until a request
    configured = collector.configured
    if switch == "off":
        state = "off"
    elif not configured:
        state = "not_configured"
    elif config.CONNECTORS_DRY_RUN:
        state = "dry_run"
    else:
        state = "live"
    return {"source": source, "state": state, "switch": switch, "configured": configured,
            "missing": collector.missing_env(), "set_switch": _switched(source, switches)}


__all__ = ["COLLECTORS", "DEFAULT_SWITCH", "MetricCollector", "PLANNED_ENV", "PullResult", "PurchaseRow", "SOURCE_INFO",
           "SWITCH_MODES", "SnapshotRow", "SpendRow", "env_names", "runs_in", "scrub", "source_dry_run", "source_mode",
           "tier_of"]
