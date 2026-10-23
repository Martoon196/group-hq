"""Growth: one section for visitors, sign-ups, trials, paying, MRR, spend and cost per sale by product and channel,
plus email, social, search and running costs, every figure read from Postgres (no page view calls a platform).

    GET  /growth                               Overview: tiles, a row per product, data freshness, running costs
    GET  /growth/channels                      spend beside sales by platform and campaign ("pixel says" / "money says")
    GET  /growth/email                         Brevo lists, campaigns and transactional health
    GET  /growth/social                        accounts, followers, top posts
    GET  /growth/search                        Search Console per site, top queries and pages
    GET  /growth/costs                         Neon usage per project, by day and month to date, priced from Settings
    GET  /growth/sources                       the health page: one row per source, switches, Test and Pull now
    POST /growth/sources/{source}/test         one cheap authenticated call (the redacted plan under the dry-run floor)
    POST /growth/sources/{source}/pull         run that collector now for its window (dry_run=1 plans only)
    POST /growth/sources/{source}/switch       mode=on|off
    POST /growth/promo-map                     add, change or remove a promo code's channel
    POST /growth/import                        an ad platform's CSV export into ad_spend (dry_run=1 shows what would be written)

Every page takes ?period=today|7d|30d and ?product=pp|kp|ordris|hnh|all. Same conventions as the rest of the app:
require_admin on every route, check_origin on every POST, an audit row for every change (growth.source_switch,
growth.source_test, growth.pull, growth.promo_map, growth.csv_import), a POST that redirects with a flash, except a
pull or a dry-run import, which renders its run page (no-store) because the point is to read what happened.
The import route takes no File or Form parameters, so FastAPI reads no body before require_admin and check_origin have
run; it then refuses a body over the CSV limit by its Content-Length before parsing the form itself.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from starlette.datastructures import UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import AD_PLATFORMS, CHANNELS, PRODUCTS, SOURCES, PromoChannelMap, User, utcnow
from ..services import settings
from ..services.audit import audit
from ..services.connectors.base import ConnectorError
from ..services.growth import reports, store
from ..services.growth.collectors import COLLECTORS, SOURCE_INFO, source_dry_run, source_mode
from ..services.growth.collectors import csv_import
from ..services.growth.collectors.base import scrub
from ..services.growth.runner import run_source
from ..templating import go, render

router = APIRouter(prefix="/growth", dependencies=[Depends(check_origin)])

STATE_LABELS = {"live": "Live", "dry_run": "Dry run", "not_configured": "Not configured", "off": "Off",
                "not_built": "Not built yet"}
CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,39}$")
PLATFORM = re.compile(r"^[a-z0-9_]{0,20}$")
UPLOAD_MAX_BYTES = csv_import.MAX_BYTES + 64 * 1024        # the CSV limit plus room for the form around it


def _filters(request: Request) -> tuple[reports.Period, str]:
    period = reports.period_of(request.query_params.get("period"))
    product = reports.product_filter(request.query_params.get("product"))
    return period, product


def _ctx(user: User, sub: str, period: reports.Period, product: str, **extra) -> dict:
    return {"user": user, "section": "growth", "sub": sub, "period": period, "product": product,
            "periods": reports.PERIOD_LABELS, "products": reports.PRODUCT_ORDER, "floor": bool(config.CONNECTORS_DRY_RUN),
            **extra}


def _source(source: str) -> str:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail="There is no such source.")
    return source


def _no_store(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("", response_class=HTMLResponse)
def overview(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    data = reports.overview(db, period, product, settings.growth_source_switches(db), settings.running_cost_rates(db))
    return render(request, "growth/overview.html", _ctx(user, "overview", period, product, data=data))


@router.get("/channels", response_class=HTMLResponse)
def channels(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    return render(request, "growth/channels.html", _ctx(user, "channels", period, product,
                                                        data=reports.channels(db, period, product)))


@router.get("/email", response_class=HTMLResponse)
def email(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    return render(request, "growth/email.html", _ctx(user, "email", period, product, data=reports.email(db, period, product),
                                                     brevo=reports.health(db).get("brevo")))


@router.get("/social", response_class=HTMLResponse)
def social(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    return render(request, "growth/social.html", _ctx(user, "social", period, product,
                                                      data=reports.social(db, period, product),
                                                      zernio=reports.health(db).get("zernio")))


@router.get("/search", response_class=HTMLResponse)
def search(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    return render(request, "growth/search.html", _ctx(user, "search", period, product,
                                                      data=reports.search(db, period, product),
                                                      gsc=reports.health(db).get("search_console")))


@router.get("/costs", response_class=HTMLResponse)
def costs(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    data = reports.running_costs(db, settings.running_cost_rates(db), reports.today_london(), start=period.start,
                                 end=period.end)
    return render(request, "growth/costs.html", _ctx(user, "costs", period, product, data=data,
                                                     neon=reports.health(db).get("neon")))


@router.get("/sources", response_class=HTMLResponse)
def sources(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    period, product = _filters(request)
    rows = reports.sources(db, settings.growth_source_switches(db))
    promo = db.query(PromoChannelMap).order_by(PromoChannelMap.product, PromoChannelMap.code).all()
    return render(request, "growth/sources.html", _ctx(
        user, "sources", period, product, rows=rows, promo=promo, state_labels=STATE_LABELS, channels_list=CHANNELS,
        ad_platforms=AD_PLATFORMS, product_codes=PRODUCTS, date_formats=csv_import.DATE_FORMATS,
        unmapped=reports.channels(db, period, "all")["unmapped"]))


@router.post("/sources/{source}/switch", response_class=HTMLResponse)
def switch(source: str, mode: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    source = _source(source)
    name = SOURCE_INFO[source]["name"]
    if source not in COLLECTORS:
        return go("/growth/sources", err=f"{name} is not built yet: there is nothing to switch on.")
    if mode not in ("on", "off"):
        return go("/growth/sources", err="Choose On or Off.")
    changed = settings.set_growth_source_switch(db, source, mode, actor=user)
    db.commit()
    if not changed:
        return go("/growth/sources", ok=f"{name} was already {mode}. Nothing changed.")
    if mode == "off":
        return go("/growth/sources", ok=f"{name} is off: the growth crons skip it and its figures stop moving.")
    now = source_mode(source, switches=settings.growth_source_switches(db))
    if now["state"] == "not_configured":
        return go("/growth/sources", err=f"{name} is on, but this server has no {', '.join(now['missing'])}: the crons will "
                                          "fail their run until it is set where they run, or the source is switched off.")
    return go("/growth/sources", ok=f"{name} is on: the {SOURCE_INFO[source]['tier']} cron collects it from its next run. "
                                    "Press Test first if you have not.")


@router.post("/sources/{source}/test", response_class=HTMLResponse)
def test(request: Request, source: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    source = _source(source)
    name = SOURCE_INFO[source]["name"]
    cls = COLLECTORS.get(source)
    if cls is None:
        return go("/growth/sources", err=f"{name} is not built yet.")
    collector = cls.from_config(dry_run=source_dry_run())
    try:
        result = collector.probe()
        after = {"ok": True, "status": result.status, "dry_run": result.dry_run or None,
                 "detail": result.data if isinstance(result.data, dict) and len(str(result.data)) < 600 else None}
        error = ""
    except ConnectorError as exc:
        error = scrub(exc.summary(), collector.secrets())[:300]
        after = {"ok": False, "status": exc.status, "detail": error}
        result = None
    except Exception as exc:   # noqa: BLE001  (a Test must answer, audited, whatever the collector raised)
        error = scrub(f"error: {type(exc).__name__}", collector.secrets())[:300]
        after = {"ok": False, "status": None, "detail": error}
        result = None
    finally:
        collector.close()
    audit(db, user, "growth.source_test", f"source:{source}", after=after)
    db.commit()
    if result is not None and result.dry_run:
        ctx = _ctx(user, "sources", *_filters(request), title=f"Test {name}", source=source, name=name,
                   outcome=None, test_plans=list(collector.plans))
        return _no_store(render(request, "growth/run.html", ctx))
    if error:
        return go("/growth/sources", err=f"{name}: the test failed: {error}")
    data = result.data if result is not None and isinstance(result.data, dict) else {}
    caveats = [f"not visible to the key: {', '.join(str(m) for m in data['missing'])[:200]}"] if data.get("missing") else []
    caveats += [str(w)[:200] for w in data.get("warnings") or []]
    if caveats:
        return go("/growth/sources", err=f"{name}: the test call answered (HTTP {after['status']}), but "
                                         + "; ".join(caveats)[:600] + ".")
    return go("/growth/sources", ok=f"{name}: the test call answered (HTTP {after['status']}). The credentials work from "
                                    "this server.")


@router.post("/sources/{source}/pull", response_class=HTMLResponse)
def pull(request: Request, source: str, dry_run: str = Form(""), db: Session = Depends(get_db),
         user: User = Depends(require_admin)):
    source = _source(source)
    name = SOURCE_INFO[source]["name"]
    if source not in COLLECTORS:
        return go("/growth/sources", err=f"{name} is not built yet.")
    outcome = run_source(db, source, dry_run=bool(dry_run), actor=user)
    audit(db, user, "growth.pull", f"source:{source}",
          after={"state": outcome.state, "rows": outcome.rows, "dry_run": outcome.state == "dry_run" or None,
                 "error": outcome.error or None, "since": outcome.since.isoformat() if outcome.since else None})
    db.commit()
    ctx = _ctx(user, "sources", *_filters(request), title=f"Pull {name}", source=source, name=name, outcome=outcome,
               test_plans=None)
    return _no_store(render(request, "growth/run.html", ctx))


@router.post("/promo-map", response_class=HTMLResponse)
def promo_map(code: str = Form(""), product: str = Form(""), channel: str = Form(""), platform: str = Form(""),
              campaign_name: str = Form(""), note: str = Form(""), action: str = Form("save"),
              db: Session = Depends(get_db), user: User = Depends(require_admin)):
    code = code.strip().upper()
    platform = platform.strip().lower()
    if product not in PRODUCTS:
        return go("/growth/sources#promo", err="Choose the product the code belongs to.")
    if not CODE.match(code):
        return go("/growth/sources#promo", err="A promo code is letters, digits, - or _, up to 40 characters.")
    entity = f"promo:{product}:{code}"
    row = db.get(PromoChannelMap, (code, product))
    before = ({"channel": row.channel, "platform": row.platform, "campaign_name": row.campaign_name, "note": row.note}
              if row is not None else None)
    if action == "delete":
        if row is None:
            return go("/growth/sources#promo", err=f"{code} is not mapped for {settings.product_name(product)}.")
        db.delete(row)
        audit(db, user, "growth.promo_map", entity, before=before, after=None)
        db.commit()
        return go("/growth/sources#promo", ok=f"{code} is no longer mapped; its sales fall back to their UTM touches.")
    if channel not in CHANNELS:
        return go("/growth/sources#promo", err="Choose a channel.")
    if not PLATFORM.match(platform):
        return go("/growth/sources#promo", err="A platform is a short lower-case name such as meta, google or brevo.")
    after = {"channel": channel, "platform": platform, "campaign_name": campaign_name.strip()[:200],
             "note": note.strip()[:200]}
    if before == after:
        return go("/growth/sources#promo", ok=f"{code} was already mapped that way. Nothing changed.")
    if row is None:
        row = PromoChannelMap(code=code, product=product, created_by=user.email)
        db.add(row)
    row.channel, row.platform, row.campaign_name, row.note = after["channel"], after["platform"], after["campaign_name"], after["note"]
    row.updated_at = utcnow()
    audit(db, user, "growth.promo_map", entity, before=before, after=after)
    db.commit()
    return go("/growth/sources#promo", ok=f"{code} ({settings.product_name(product)}) is credited to {channel.replace('_', ' ')}"
                                           f"{' / ' + platform if platform else ''}.")


@router.post("/import", response_class=HTMLResponse)
async def import_csv(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    # No File or Form parameters: FastAPI would read the whole body before require_admin ran. The login and the
    # origin are checked first, then the size by Content-Length, and only then is the form parsed.
    try:
        length = int(request.headers.get("content-length") or "")
    except ValueError:
        return go("/growth/sources#import", err="The upload had no size: choose the CSV file and press Import again.")
    if length > UPLOAD_MAX_BYTES:
        return go("/growth/sources#import", err="The file is over 2 MB: export a shorter range.")
    form = await request.form(max_files=1, max_fields=10)
    file = form.get("file")
    platform = str(form.get("platform") or "")
    account_id = str(form.get("account_id") or "")
    date_format = str(form.get("date_format") or "iso")
    dry_run = str(form.get("dry_run") or "")
    if not isinstance(file, UploadFile) or not file.filename:
        return go("/growth/sources#import", err="Choose the CSV file to import.")
    raw = await file.read(csv_import.MAX_BYTES + 1)
    if len(raw) > csv_import.MAX_BYTES:
        return go("/growth/sources#import", err="The file is over 2 MB: export a shorter range.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    try:
        parsed = csv_import.parse(text, platform=platform, account_id=account_id, date_format=date_format)
    except csv_import.CSVProblem as exc:
        return go("/growth/sources#import", err=f"Nothing was imported: {exc}")
    days = sorted({r.date for r in parsed.rows})
    summary = {"platform": platform, "file": (file.filename or "")[:120], "rows": len(parsed.rows), "skipped": parsed.skipped,
               "first_day": days[0].isoformat(), "last_day": days[-1].isoformat(),
               "file_rows": sum(int(r.raw.get("rows_summed", 1)) for r in parsed.rows),
               "spend_pence": sum(r.spend_pence for r in parsed.rows), "dry_run": bool(dry_run) or None}
    if dry_run:
        audit(db, user, "growth.csv_import", f"platform:{platform}", after=summary)
        db.commit()
        ctx = _ctx(user, "sources", *_filters(request), title="Import a CSV (dry run)", source="", name=platform,
                   outcome=None, test_plans=None, csv=parsed, csv_summary=summary)
        return _no_store(render(request, "growth/run.html", ctx))
    written = store.upsert_spend(db, parsed.rows)
    audit(db, user, "growth.csv_import", f"platform:{platform}", after={**summary, "written": written})
    db.commit()
    return go("/growth/sources#import", ok=f"Imported {written} {platform} campaign day{'s' if written != 1 else ''} "
                                           f"({days[0]:%d %b} to {days[-1]:%d %b}); a day already imported was replaced.")
