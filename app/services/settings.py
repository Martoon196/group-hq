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

The Growth module adds two kinds of row (app.services.growth): growth_source.<source> ("on" or "off", every source
starting Off) and running_cost_rate.neon.<metric> (what the founder's Neon plan charges per unit, blank until set:
Group HQ never invents a price). Both are audited like the product switches.
"""
from __future__ import annotations

import os
from decimal import Decimal, InvalidOperation

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


# ---- the growth source switches (Growth > Sources) ------------------------------------------------------------
# growth_source.<source> is "on" or "off". Every source starts Off: a source switched On is collected by the growth
# crons and by Pull now, and must then be configured where it runs, or the job fails its run and names the variable.
GROWTH_PREFIX = "growth_source."


def growth_switch_key(source: str) -> str:
    return GROWTH_PREFIX + source


def growth_source_switches(db: Session) -> dict[str, str]:
    """{source: "on" | "off"} for every source whose switch has been set; read fresh on every call, like the product
    switches, so the web service and the crons agree."""
    from ..models import SOURCES
    from .growth.collectors import SWITCH_MODES as GROWTH_MODES

    rows = db.query(AppSetting).filter(AppSetting.key.like(GROWTH_PREFIX + "%")).all()
    out: dict[str, str] = {}
    for row in rows:
        source = row.key[len(GROWTH_PREFIX):]
        if source in SOURCES and row.value in GROWTH_MODES:
            out[source] = str(row.value)
    return out


def set_growth_source_switch(db: Session, source: str, mode: str, *, actor: User | str) -> bool:
    """Switch a growth source on or off. Writes the app_settings row, moves the source's health row to "off" (or back
    from it), and one audit row (growth.source_switch on source:<name>) with the switch and state before and after.
    Returns False when nothing changed. The caller commits."""
    from ..models import SOURCES
    from .growth.collectors import SWITCH_MODES as GROWTH_MODES
    from .growth.collectors import source_mode
    from .growth.runner import source_row

    if source not in SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if mode not in GROWTH_MODES:
        raise ValueError(f"a growth switch is on or off, not {mode!r}")
    switches = growth_source_switches(db)
    before = source_mode(source, switches=switches)
    if before["switch"] == mode:
        return False
    key = growth_switch_key(source)
    statement = insert(AppSetting).values(key=key, value=mode, updated_at=utcnow(), updated_by=_actor_label(actor))
    db.execute(statement.on_conflict_do_update(index_elements=[AppSetting.key], set_={
        "value": statement.excluded.value, "updated_at": statement.excluded.updated_at,
        "updated_by": statement.excluded.updated_by}))
    row = db.get(AppSetting, key)
    if row is not None:
        db.expire(row)
    switches[source] = mode
    after = source_mode(source, switches=switches)
    health = source_row(db, source)
    if mode == "off":
        health.status = "off"
    elif health.status == "off":
        health.status = "never_run" if health.last_run_at is None else (
            "ok" if health.last_ok_at and health.last_ok_at >= health.last_run_at else "failing")
    health.updated_by = _actor_label(actor)[:200]
    audit(db, actor, "growth.source_switch", f"source:{source}",
          before={"switch": before["switch"], "state": before["state"]},
          after={"switch": mode, "state": after["state"], "floor": bool(config.CONNECTORS_DRY_RUN) or None})
    return True


# ---- running-cost rates (Settings > Running costs) --------------------------------------------------------------
# What the founder's Neon plan charges per unit, typed on the Settings screen. Every rate starts blank: Group HQ never
# invents a price, and the Costs screen shows usage without an estimate until a rate is set. Rates are in US dollars,
# the currency Neon prices in.
RATE_PREFIX = "running_cost_rate."
RATE_MAX = Decimal("10000")


def rate_fields() -> list[dict]:
    from .growth.registry import NEON_UNITS
    return [{"key": f"neon.{u.api_name}", "label": u.label, "unit": u.billing_unit, "metric": u.metric} for u in NEON_UNITS]


def running_cost_rates(db: Session) -> dict[str, Decimal | None]:
    """{rate key: dollars per billing unit, or None when not set}."""
    stored = {row.key[len(RATE_PREFIX):]: row.value
              for row in db.query(AppSetting).filter(AppSetting.key.like(RATE_PREFIX + "%")).all()}
    out: dict[str, Decimal | None] = {}
    for field in rate_fields():
        raw = stored.get(field["key"])
        try:
            out[field["key"]] = Decimal(str(raw)) if raw not in (None, "") else None
        except (InvalidOperation, ValueError):
            out[field["key"]] = None
    return out


def parse_rate(raw: str) -> Decimal | None:
    """A typed rate: blank clears it; otherwise a number from 0 to 10,000 with at most 6 decimal places."""
    text = (raw or "").strip().replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{raw[:20]!r} is not a number") from None
    if not value.is_finite() or value < 0 or value > RATE_MAX:
        raise ValueError(f"{raw[:20]!r} is not a rate between 0 and {RATE_MAX}")
    if value.as_tuple().exponent < -6:
        raise ValueError(f"{raw[:20]!r} has more than six decimal places")
    return value


def set_running_cost_rates(db: Session, typed: dict[str, str], *, actor: User | str) -> list[str]:
    """Store the typed rates (blank clears one). Raises ValueError naming the first unreadable rate, before anything
    is written. One audit row (settings.running_cost_rates on running_costs:neon) carries every rate that changed,
    before and after. Returns the changed keys. The caller commits."""
    current = running_cost_rates(db)
    wanted: dict[str, Decimal | None] = {}
    for field in rate_fields():
        key = field["key"]
        if key not in typed:
            continue
        try:
            wanted[key] = parse_rate(typed[key])
        except ValueError as exc:
            raise ValueError(f"{field['label']} (per {field['unit']}): {exc}") from None
    changed = [key for key, value in wanted.items() if value != current.get(key)]
    for key in changed:
        value = wanted[key]
        if value is None:
            db.query(AppSetting).filter(AppSetting.key == RATE_PREFIX + key).delete(synchronize_session=False)
            continue
        statement = insert(AppSetting).values(key=RATE_PREFIX + key, value=str(value), updated_at=utcnow(),
                                              updated_by=_actor_label(actor))
        db.execute(statement.on_conflict_do_update(index_elements=[AppSetting.key], set_={
            "value": statement.excluded.value, "updated_at": statement.excluded.updated_at,
            "updated_by": statement.excluded.updated_by}))
    if changed:
        audit(db, actor, "settings.running_cost_rates", "running_costs:neon",
              before={k: (str(current[k]) if current.get(k) is not None else None) for k in changed},
              after={k: (str(wanted[k]) if wanted[k] is not None else None) for k in changed})
    return changed
