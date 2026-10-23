"""Channel rules for the money side: the Python twin of the sales_attribution view (alembic/versions/0007_growth.py).

The view resolves each purchase in this order: the promo code's promo_channel_map entry; else the last touch's
utm_source / utm_medium through UTM_RULES; else an affiliate reference; else the first touch the same way; else
direct. UTM_RULES and PLATFORM_ALIASES here must equal the migration's copies (a test checks it), and resolve() gives
the answer the view gives, so a screen or a test can ask without a query. Campaign names, product prefixes and the
UTM convention are TRACKING-SPEC.md 2.3 and 2.4.
"""
from __future__ import annotations

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
PLATFORM_ALIASES: dict[str, str] = {"facebook": "meta", "fb": "meta", "instagram": "meta", "ig": "meta",
                                    "twitter": "x"}
CAMPAIGN_PREFIXES = {"pp-": "pp", "kp-": "kp", "ord-": "ordris", "hnh-": "hnh"}


def _norm(value) -> str:
    return str(value or "").strip().lower()


def channel_for(source: str, medium: str) -> str:
    """The channel a utm_source / utm_medium pair names, or "" when both are blank (the touch says nothing)."""
    src, med = _norm(source), _norm(medium)
    if not src and not med:
        return ""
    for channel, sources, mediums in UTM_RULES:
        if (sources is None or src in sources) and (mediums is None or med in mediums):
            return channel
    return "other"


def platform_for(source: str) -> str:
    src = _norm(source)
    return PLATFORM_ALIASES.get(src, src)


def product_from_campaign(name: str) -> str:
    """pp-launch-202611-hmo -> pp; anything without a product prefix is group-wide."""
    lowered = _norm(name)
    for prefix, product in CAMPAIGN_PREFIXES.items():
        if lowered.startswith(prefix):
            return product
    return "group"


def resolve(*, promo_code: str = "", promo_map: dict | None = None, last_touch: dict | None = None,
            affiliate_ref: str = "", first_touch: dict | None = None) -> dict:
    """{attribution_basis, channel, platform, campaign_name} for one purchase, as the view resolves it. promo_map is
    the promo_channel_map entry for (product, code) as a dict, or None."""
    if promo_code and promo_map:
        return {"attribution_basis": "promo", "channel": promo_map.get("channel", ""),
                "platform": promo_map.get("platform", ""), "campaign_name": promo_map.get("campaign_name", "")}
    for basis, touch in (("last_touch", last_touch), ("first_touch", first_touch)):
        if basis == "first_touch" and (affiliate_ref or "").strip():
            return {"attribution_basis": "affiliate", "channel": "affiliate", "platform": "", "campaign_name": ""}
        touch = touch or {}
        channel = channel_for(touch.get("utm_source", ""), touch.get("utm_medium", ""))
        if channel:
            return {"attribution_basis": basis, "channel": channel, "platform": platform_for(touch.get("utm_source", "")),
                    "campaign_name": str(touch.get("utm_campaign") or "").strip()}
    return {"attribution_basis": "direct", "channel": "direct", "platform": "", "campaign_name": ""}
