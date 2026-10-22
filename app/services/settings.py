"""Display settings and product facts the templates and connectors share, and the switches set in the app.

Display settings are code-level defaults with environment overrides: GROUPHQ_SETTING_<KEY> replaces the value
of <key>. Secrets never live here.

Switches are what the founder flips on the Settings screen and live in the app_settings table, so the web
service and the nightly job read the same answer. Today that is one Live / Dry run switch per product
(product_switch.<product>). The rule for whether a product is sent anything (connectors.product_dry_run):
CONNECTORS_DRY_RUN=1 holds every product in dry run whatever the switches say; otherwise a product's switch
decides once it has been set, and the per-product CONNECTORS_DRY_RUN list is only the default for a product
whose switch has never been set. A product without its token or base URL sends nothing whatever the switch
says (not configured).
"""
from __future__ import annotations

import os

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import config
from ..models import PRODUCTS, AppSetting, User, utcnow
from .audit import audit
from .connectors import SWITCH_MODES, product_mode

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

SWITCH_PREFIX = "product_switch."
SWITCH_LABELS = {"live": "Live", "dry_run": "Dry run"}


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


# ---- the product switches -------------------------------------------------------------------------------
def switch_key(product: str) -> str:
    return SWITCH_PREFIX + product


def product_switches(db: Session) -> dict[str, str]:
    """{product: "live" | "dry_run"} for every product whose switch has been set; a product never switched is
    absent and follows the environment default. Read fresh on every call, never cached, so a change on the
    Settings screen applies to the next request and the next nightly run alike."""
    rows = db.query(AppSetting).filter(AppSetting.key.like(SWITCH_PREFIX + "%")).all()
    out: dict[str, str] = {}
    for row in rows:
        product = row.key[len(SWITCH_PREFIX):]
        if product in PRODUCTS and row.value in SWITCH_MODES:
            out[product] = str(row.value)
    return out


def switch_rows(db: Session) -> dict[str, AppSetting]:
    """The stored switch rows by product, for who changed each one and when."""
    rows = db.query(AppSetting).filter(AppSetting.key.like(SWITCH_PREFIX + "%")).all()
    return {row.key[len(SWITCH_PREFIX):]: row for row in rows}


def product_modes(db: Session, order: tuple[str, ...] = PRODUCTS) -> list[dict]:
    """How each product is treated now, with its name: state is live, dry_run or not_configured, source says
    what decided it (floor, switch or environment). Every screen that shows a product's mode reads this."""
    switches = product_switches(db)
    return [{**product_mode(product, switches=switches), "name": product_name(product)} for product in order]


def _actor_label(actor: User | str | None) -> str:
    if isinstance(actor, User):
        return actor.email
    return str(actor or "system")


def set_product_switch(db: Session, product: str, mode: str, *, actor: User | str) -> bool:
    """Set a product's switch to "live" or "dry_run", or clear it with "" so the environment default applies
    again. Writes an audit row (settings.product_switch on product:<code>) with the switch and the state it
    gives before and after. Returns False when nothing changed. The caller commits."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown product {product!r}")
    if mode not in SWITCH_MODES and mode != "":
        raise ValueError(f"a switch is live or dry_run, not {mode!r}")
    key = switch_key(product)
    row = db.query(AppSetting).filter(AppSetting.key == key).with_for_update().first()
    current = str(row.value) if row is not None and row.value in SWITCH_MODES else ""
    if current == mode:
        return False
    switches = product_switches(db)
    before = {"switch": current or None, "state": product_mode(product, switches=switches)["state"]}
    if mode:
        statement = insert(AppSetting).values(key=key, value=mode, updated_at=utcnow(), updated_by=_actor_label(actor))
        db.execute(statement.on_conflict_do_update(index_elements=[AppSetting.key], set_={
            "value": statement.excluded.value, "updated_at": statement.excluded.updated_at,
            "updated_by": statement.excluded.updated_by}))
        switches[product] = mode
    else:
        db.query(AppSetting).filter(AppSetting.key == key).delete(synchronize_session=False)
        switches.pop(product, None)
    if row is not None:
        db.expire(row)      # written by a core statement: the loaded row must not answer with the old value
    after = {"switch": mode or None, "state": product_mode(product, switches=switches)["state"],
             "floor": bool(config.CONNECTORS_DRY_RUN) or None}
    audit(db, actor, "settings.product_switch", f"product:{product}", before=before, after=after)
    return True
