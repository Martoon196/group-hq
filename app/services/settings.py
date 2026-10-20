"""Display settings and product facts the templates and connectors share.

Group HQ has no settings table (the plan fixes the table list), so these are code-level defaults with
environment overrides: GROUPHQ_SETTING_<KEY> replaces the value of <key>. Secrets never live here.
"""
from __future__ import annotations

import os

from .. import config

DEFAULTS: dict[str, object] = {
    "app_name": "Group HQ",
    "owner_name": "Martin Howard",
    "company_name": "MH196 2026 Limited",
    "hnh_company_name": "Hello New Home 2 Limited",
    "base_url": config.BASE_URL,
    "products": {
        "pp": {"name": "Property Predator", "base_url": config.PP_BASE_URL, "company": "MH196 2026 Limited"},
        "ordris": {"name": "Ordris", "base_url": config.ORDRIS_BASE_URL, "company": "MH196 2026 Limited"},
        "kp": {"name": "Key Properties", "base_url": config.KP_BASE_URL, "company": "MH196 2026 Limited"},
        "hnh": {"name": "Hello New Home", "base_url": config.HNH_BASE_URL, "company": "Hello New Home 2 Limited"},
    },
    "connectors_dry_run": config.CONNECTORS_DRY_RUN,
    "grace_days": 7,            # monthly payers keep access this long after period_end (plan, Phase 1)
    "full_term_months": 12,     # paid in full = this many months from start_date
}


def get(key: str):
    override = os.environ.get(f"GROUPHQ_SETTING_{key.upper()}")
    if override is not None and override != "":
        return override
    return DEFAULTS.get(key)


def get_all() -> dict:
    return {key: get(key) for key in DEFAULTS}


def product_name(code: str) -> str:
    products = DEFAULTS["products"]
    assert isinstance(products, dict)
    return products.get(code, {}).get("name", code)
