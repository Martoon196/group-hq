"""Settings: the switches the founder flips in the app.

    GET  /settings                         each product's Live / Dry run switch, what it is now and why; the running-cost rates
    POST /settings/products/{product}      set a product's switch (mode=live or dry_run; mode=default clears it)
    POST /settings/running-costs           the founder's Neon plan rates, per billing unit (blank clears one)

The rule a switch takes part in (connectors.product_dry_run): CONNECTORS_DRY_RUN=1 on the server holds every
product in dry run whatever the switches say; otherwise a product's switch decides once it has been set, and the
CONNECTORS_DRY_RUN list is only the default for a product never switched. A product without its token or base
URL sends nothing, whatever the switch says. The nightly job reads the same switches from the same database, but
it runs as its own Render cron with its own copy of the environment, so the page also shows what the job's last
real run held: a product live here whose token never reached the cron is named on its row, and the Live wording
never promises a nightly send the job cannot make.

Same conventions as the rest of the app: require_admin on every route, check_origin on every POST, an audit
row for every change (settings.product_switch, written by the service), and a POST that redirects with a flash.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import PRODUCTS, AuditLog, User
from ..services import settings
from ..services.connectors import CONNECTORS
from ..services.entitlements import JOB_ACTOR
from ..templating import go, render

router = APIRouter(prefix="/settings", dependencies=[Depends(check_origin)])

STATE_LABELS = {"live": "Live", "dry_run": "Dry run", "not_configured": "Not configured"}
MODES = {"live": "live", "dry_run": "dry_run", "default": ""}
# The variable that carries each product's token, named where the nightly job is missing it (render.yaml).
TOKEN_VARS = {"pp": "PP_SERVICE_TOKEN", "ordris": "ORDRIS_PARTNER_TOKEN", "kp": "KP_PARTNER_TOKEN", "hnh": "HNH_PARTNER_TOKEN"}
RECENT_RUNS = 50


def _last_nightly(db: Session) -> AuditLog | None:
    """The nightly job's latest real run. The cron audits as job:recompute; a run from the Grants page is audited as
    the admin who pressed it, on the web service's environment, so it is not the job's view. A --dry-run of the job
    (from a shell, say) is passed over too, unless every recent run was a dry run, as under CONNECTORS_DRY_RUN=1."""
    rows = (db.query(AuditLog).filter(AuditLog.action == "recompute.run", AuditLog.actor == JOB_ACTOR)
            .order_by(AuditLog.id.desc()).limit(RECENT_RUNS).all())
    for row in rows:
        if isinstance(row.after, dict) and not row.after.get("dry_run"):
            return row
    return rows[0] if rows else None


def _nightly_configured(last: AuditLog | None) -> dict[str, bool]:
    """{product: whether the nightly job held its token and base URL} at its last run; a product absent is unknown
    (no run has recorded it yet)."""
    configured = last.after.get("configured") if last is not None and isinstance(last.after, dict) else None
    if not isinstance(configured, dict):
        return {}
    return {product: bool(value) for product, value in configured.items() if product in PRODUCTS}


def live_wording(product: str, nightly: bool | None) -> dict:
    """What switching a product live means for the nightly job, for the Live confirmation and the flash. nightly is
    whether the job held the product's token at its last run (None: not known yet)."""
    name, var = settings.product_name(product), TOKEN_VARS.get(product, "its token")
    if nightly:
        return {"kind": "ok", "confirm": f"Switch {name} to live? Grants, extensions and revokes are sent to it from the "
                                         "next run, the nightly one included.",
                "flash": f"{name} is live: grants, extensions and revokes are sent to it from the next run, the nightly "
                         "one included."}
    if nightly is False:
        return {"kind": "missing",
                "confirm": f"Switch {name} to live? Group HQ's buttons will send to it, but the nightly job cannot: its "
                           f"last run had no token or base URL for {name}. Add the {var} entry to the recompute cron first.",
                "flash": f"{name} is switched to live, but only Group HQ's buttons can send to it: the nightly job's last "
                         f"run had no token or base URL for {name}. Add the {var} fromService entry to group-hq-recompute "
                         f"in render.yaml and sync; until then access in {name} is not extended or revoked at night."}
    return {"kind": "unknown",
            "confirm": f"Switch {name} to live? Group HQ's buttons will send to it. No nightly run has shown yet whether "
                       f"the recompute cron holds {var}; check Settings after the next one.",
            "flash": f"{name} is live for Group HQ's buttons from now on. No nightly run has shown yet whether the "
                     f"recompute cron holds {var}: check this screen after the next nightly run (03:10 UTC)."}


def dry_run_confirm(name: str) -> str:
    return (f"Switch {name} to dry run? Nothing more is sent to it, a run under way included: new grants and extensions "
            f"wait, so access in {name} ends when its current window does, and revokes wait too.")


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    modes = settings.product_modes(db)
    rows = settings.switch_rows(db)
    last = _last_nightly(db)
    nightly = _nightly_configured(last)
    floor = bool(config.CONNECTORS_DRY_RUN)
    for mode in modes:
        product = mode["product"]
        connector = CONNECTORS[product].from_config(dry_run=True)
        mode["base_url"] = connector.base_url
        mode["default"] = "dry_run" if product in config.CONNECTORS_DRY_RUN_PRODUCTS else "live"
        mode["row"] = rows.get(product)
        mode["nightly"] = nightly.get(product)
        mode["token_var"] = TOKEN_VARS.get(product, "")
        mode["live"] = live_wording(product, mode["nightly"])
        # The confirmation each button needs: Live always asks; Dry run asks when it stops a product that sends now;
        # "Use the default" asks as the switch it falls back to would, when that changes anything.
        stops = mode["state"] == "live"
        live = (f"Save {mode['name']}'s switch as live? CONNECTORS_DRY_RUN=1 on this server still holds every product in "
                "dry run, so nothing is sent until that is removed.") if floor else mode["live"]["confirm"]
        mode["confirm"] = {"live": live, "dry_run": dry_run_confirm(mode["name"]) if stops else ""}
        changes = not floor and mode["switch"] and mode["switch"] != mode["default"]
        mode["confirm"]["default"] = mode["confirm"][mode["default"]] if changes else ""
    ctx = {"user": user, "section": "settings", "modes": modes, "floor": floor,
           "env_list": ",".join(sorted(config.CONNECTORS_DRY_RUN_PRODUCTS)), "state_labels": STATE_LABELS,
           "switch_labels": settings.SWITCH_LABELS, "last_run": last,
           "last_products": (last.after or {}).get("products") if last is not None and isinstance(last.after, dict) else None,
           "rate_fields": settings.rate_fields(), "rates": settings.running_cost_rates(db)}
    return render(request, "settings.html", ctx)


@router.post("/running-costs", response_class=HTMLResponse)
async def set_rates(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()
    typed = {f["key"]: str(form.get(f["key"], "")) for f in settings.rate_fields() if f["key"] in form}
    try:
        changed = settings.set_running_cost_rates(db, typed, actor=user)
    except ValueError as exc:
        db.rollback()
        return go("/settings#running-costs", err=f"Nothing was saved: {exc}")
    db.commit()
    if not changed:
        return go("/settings#running-costs", ok="The rates were already set that way. Nothing changed.")
    return go("/settings#running-costs", ok=f"Saved {len(changed)} rate{'s' if len(changed) != 1 else ''}: the Costs screen "
                                            "estimates from them now.")


@router.post("/products/{product}", response_class=HTMLResponse)
def set_switch(product: str, mode: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    if product not in PRODUCTS:
        raise HTTPException(status_code=404, detail="There is no such product.")
    if mode not in MODES:
        return go("/settings", err="Choose Live or Dry run.")
    name = settings.product_name(product)
    changed = settings.set_product_switch(db, product, MODES[mode], actor=user)
    db.commit()
    if not changed:
        return go("/settings", ok=f"{name} was already set that way. Nothing changed.")
    [now] = [m for m in settings.product_modes(db) if m["product"] == product]
    err = ""
    if config.CONNECTORS_DRY_RUN:
        note = (f"{name}'s switch is saved, but CONNECTORS_DRY_RUN=1 on this server still holds every product in dry run. "
                "The switch takes effect once that is removed.")
    elif now["state"] == "live":
        wording = live_wording(product, _nightly_configured(_last_nightly(db)).get(product))
        note = wording["flash"]
        if wording["kind"] == "missing":
            err, note = note, ""
    elif now["state"] == "not_configured":
        note = (f"{name} is switched to live, but its token or base URL is missing on this server, so nothing is sent "
                "until it is set.")
    else:
        note = (f"{name} is in dry run: nothing more is sent to it, a run already under way included (from the next "
                "person it reaches); its calls are recorded and shown.")
    if mode == "default":
        prefix = f"{name} follows the environment default again. "
        note, err = (prefix + note if note else ""), (prefix + err if err else "")
    return go("/settings", ok=note, err=err)
