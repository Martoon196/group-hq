"""Territories: the territory model on screen, who holds each postcode district, and the state-only export.

    GET  /territories                       every district, with filters (band, state, area, nation, search) and totals
    GET  /territories/map                   Leaflet map: a marker at each district centroid, coloured by state
    GET  /territories/{district}            every column, the price today, Reserve / Assign / Release / Keep for HQ
    POST /territories/{district}/reserve    first refusal for a partner until a date (12 months by default)
    POST /territories/{district}/assign     core: exclusive, the partner must serve it
    POST /territories/{district}/release    back to open, whatever it was
    POST /territories/{district}/hq         kept for head office, never offered
    GET  /api/territories.json              state, district and area only, no money and no band: for Hello New Home to proxy

Money rule: every modelled figure (band, fee, units, partner net, HQ contribution, upfront and monthly fee) is a
proposal until the founder confirms the assumptions behind it, so each one is badged [CONFIRM] while any
assumption it depends on is unconfirmed (CONFIRM_DEPENDS). Partners never see these screens: Group HQ is
admin-only, and the only thing that leaves is /api/territories.json, which carries no money column and no band
(EXPORT_FIELDS, pinned by tests/test_territories.py): the band is the row of an unconfirmed pricing table and a
public JSON cannot carry a [CONFIRM] badge, so it stays inside until the bands and fees are confirmed and published.

The rows come from scripts/load_territory_model.py. The loader owns the public and modelled columns and never
touches state, partner_id or reserved_until; these screens own those three and write an audit row for every
change (entity district:<code>). partner_id is the Hello New Home partner id (people.hnh_partner_id), not a
foreign key: Group HQ never reads another app's tables. Calling Hello New Home's partner endpoint on Reserve and
Assign is the connectors' work and is not done here yet.

District centroids are not in the database (postcode_districts has no coordinate columns): the loader writes
them to data/district_centroids.json (data/sample/district_centroids.json for the sample run) and the map reads
whichever exists, preferring the real file. A sample file is said to be one on the screen.
"""
from __future__ import annotations

import hmac
import json
import os
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from markupsafe import Markup
from sqlalchemy import Integer, cast, func, or_
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..auth import check_origin, current_user, require_admin
from ..db import get_db
from ..models import BANDS, DISTRICT_STATES, NATIONS, AuditLog, ModelRun, Person, PostcodeDistrict, User, utcnow
from ..services import territory_rules as tr
from ..services.audit import audit, changes
from ..templating import go, render
from .mentees import add_months
from .people import like_pattern, page_of, parse_date, parse_int, qs


def money(amount) -> str:
    """Decimal('1250.5') is shown as £1,250.50 and a negative contribution as -£13,541.50; None is blank."""
    if amount is None:
        return ""
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    return f"-£{-value:,.2f}" if value < 0 else f"£{value:,.2f}"

router = APIRouter(dependencies=[Depends(check_origin)])

PAGE_SIZE = 100
RESERVE_MONTHS = 12            # operating model section 2.1: "First refusal for 12 months"
MAX_RESERVE_MONTHS = 24        # a reservation further away than this is almost certainly a typo
DATA_DIR = config.ROOT / "data"
CENTROIDS_PATHS = (DATA_DIR / "district_centroids.json", DATA_DIR / "sample" / "district_centroids.json")
# A bearer token lets Hello New Home proxy the export without a browser session. Empty means session only.
EXPORT_TOKEN = (os.environ.get("TERRITORY_EXPORT_TOKEN") or "").strip()
EXPORT_FIELDS = ("district", "area", "state")

STATE_LABELS = {"open": "Open: nobody holds it; leads route to head office",
                "reserved": "Reserved: first refusal for the partner until the date shown",
                "core": "Core: exclusive; the partner must serve it",
                "hq": "Head office: never offered"}
BAND_LABELS = {"london": "London", "major_city": "Major city", "town": "Town", "rural": "Rural"}
NATION_LABELS = {"england": "England", "wales": "Wales", "scotland": "Scotland", "northern_ireland": "Northern Ireland"}

# Which assumptions each modelled figure depends on. A figure wears the [CONFIRM] badge while any of them is
# unconfirmed; confirming a figure in the app takes the badge off everything that depended on it and nothing else.
_FEE = {"fee_cap", "management_pct"}
_SPLIT = {"partner_split_own", "partner_split_hq_leads", "partner_own_lead_share"}
_SYSTEMS = {"systems_fee", "systems_fee_from_month"}
_COSTS = {"cost_to_serve", "cost_to_serve_london", "portals", "ads", "ads_london", "letters"}
CONFIRM_DEPENDS: dict[str, frozenset[str]] = {
    "band": frozenset({"town_min_prs_households"}),
    "achievable_fee": frozenset(_FEE),
    "cost_to_serve": frozenset({"cost_to_serve", "cost_to_serve_london"}),
    "units": frozenset({"capture_y1", "capture_y2", "capture_y3"}),
    "partner_net_y1": frozenset({"capture_y1"} | _FEE | _SPLIT | _SYSTEMS),
    "hq_contribution_y1": frozenset({"capture_y1"} | _FEE | _SPLIT | _SYSTEMS | _COSTS),
    "upfront_fee": frozenset(_FEE | {"partner_split_own", "upfront_units", "upfront_multiplier", "upfront_round_to",
                                     "upfront_floor_rural", "upfront_floor_town", "upfront_floor_major_city", "upfront_floor_london"}),
    "monthly_fee": frozenset({"monthly_fee_rural", "monthly_fee_town", "monthly_fee_major_city", "monthly_fee_london"}),
    "break_even_units": frozenset(_FEE | _SPLIT | _COSTS | {"systems_fee"}),
    "blended_split": frozenset(_SPLIT),
}


# ---- helpers ----------------------------------------------------------------------------------------
def district_code(raw: str) -> str:
    """'sa1' and ' SA1 ' give SA1; a full postcode gives its outward code (docs/shared-keys.md section 3)."""
    s = "".join(str(raw or "").upper().split())
    return s[:-3] if len(s) >= 5 else s


def _load(db: Session, raw: str) -> PostcodeDistrict:
    row = db.get(PostcodeDistrict, district_code(raw))
    if row is None:
        raise HTTPException(status_code=404, detail=f"{district_code(raw) or raw} is not in the territory model. Load the model, or check the code.")
    return row


def holding_snapshot(d: PostcodeDistrict) -> dict:
    return {"state": d.state, "partner_id": d.partner_id, "reserved_until": d.reserved_until}


def confirm_badges(unconfirmed: set[str]) -> dict[str, bool]:
    return {figure: bool(keys & unconfirmed) for figure, keys in CONFIRM_DEPENDS.items()}


def price_for(row: PostcodeDistrict, a: dict) -> dict:
    """The band's monthly fee, and from the stored achievable fee the upfront fee and the steady-state break-even,
    exactly as territory_rules.model_district prices them (docs/pricing-bands.md). None when the district has no rent."""
    out = {"upfront_fee": None, "monthly_fee": tr.monthly_fee(row.band, a), "break_even_units": None}
    if row.achievable_fee is None:
        return out
    at_30 = tr.partner_net_month(int(a["upfront_units"]), row.achievable_fee, a["partner_split_own"], 0, 1)
    out["upfront_fee"] = tr.upfront_fee(at_30, a["upfront_multiplier"], tr.upfront_floor(row.band, a), round_to_step=a["upfront_round_to"])
    split = tr.blended_split(a["partner_split_own"], a["partner_split_hq_leads"], a["partner_own_lead_share"])
    cost = row.cost_to_serve if row.cost_to_serve is not None else tr.cost_to_serve_for(row.band, a)
    out["break_even_units"] = tr.break_even_units(row.achievable_fee, split, cost, tr.fixed_costs_for(row.band, a), a["systems_fee"])
    return out


def latest_run(db: Session) -> ModelRun | None:
    return db.query(ModelRun).order_by(ModelRun.id.desc()).first()


def run_is_sample(run: ModelRun | None) -> bool:
    return bool(run) and any(bool(f.get("sample")) for f in (run.source_files or []) if isinstance(f, dict))


_centroid_cache: dict[str, tuple[float, dict]] = {}


def load_centroids() -> tuple[dict[str, tuple[float, float]], dict]:
    """{district: (lat, lon)} from the first centroids file that exists, with its metadata (sample, generated_at,
    path). Re-read when the file changes. Empty when neither file exists."""
    for path in CENTROIDS_PATHS:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        cached = _centroid_cache.get(str(path))
        if cached and cached[0] == mtime:
            return cached[1]["districts"], cached[1]["meta"]
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            districts = {str(k).upper(): (float(v[0]), float(v[1])) for k, v in (data.get("districts") or {}).items()}
        except (OSError, ValueError, TypeError, IndexError):
            continue
        try:
            shown = str(path.relative_to(config.ROOT))
        except ValueError:
            shown = str(path)
        meta = {"sample": bool(data.get("sample")), "generated_at": data.get("generated_at", ""), "path": shown, "source": data.get("source", "")}
        _centroid_cache[str(path)] = (mtime, {"districts": districts, "meta": meta})
        return districts, meta
    return {}, {"sample": False, "generated_at": "", "path": "", "source": ""}


def _ordered(query):
    """SA1, SA2, SA10 rather than SA1, SA10, SA2: by area, then the digits of the district, then the whole code."""
    digits = cast(func.nullif(func.regexp_replace(PostcodeDistrict.district, r"\D", "", "g"), ""), Integer)
    return query.order_by(PostcodeDistrict.area, digits, PostcodeDistrict.district)


def _filters(request: Request) -> dict:
    qp = request.query_params
    return {"band": qp.get("band", "").strip(), "state": qp.get("state", "").strip(), "area": qp.get("area", "").strip().upper()[:2],
            "nation": qp.get("nation", "").strip(), "q": qp.get("q", "").strip()[:60]}


def _filtered(db: Session, filters: dict):
    q = db.query(PostcodeDistrict)
    if filters["band"] in BANDS:
        q = q.filter(PostcodeDistrict.band == filters["band"])
    if filters["state"] in DISTRICT_STATES:
        q = q.filter(PostcodeDistrict.state == filters["state"])
    if filters["area"]:
        q = q.filter(PostcodeDistrict.area == filters["area"])
    if filters["nation"] in NATIONS:
        q = q.filter(PostcodeDistrict.nation == filters["nation"])
    if filters["q"]:
        like = like_pattern(filters["q"].lower())
        q = q.filter(or_(func.lower(PostcodeDistrict.district).like(like, escape="\\"), func.lower(PostcodeDistrict.la_name).like(like, escape="\\")))
    return q


def _totals(query) -> dict:
    """Aggregates over the whole filtered set, not the page."""
    row = query.with_entities(
        func.count(PostcodeDistrict.district), func.count(PostcodeDistrict.achievable_fee), func.sum(PostcodeDistrict.prs_households),
        func.sum(PostcodeDistrict.units_y1), func.sum(PostcodeDistrict.units_y2), func.sum(PostcodeDistrict.units_y3),
        func.sum(PostcodeDistrict.partner_net_y1), func.sum(PostcodeDistrict.hq_contribution_y1),
    ).one()
    states = dict(query.with_entities(PostcodeDistrict.state, func.count()).group_by(PostcodeDistrict.state).all())
    bands = dict(query.with_entities(PostcodeDistrict.band, func.count()).group_by(PostcodeDistrict.band).all())
    return {"districts": row[0] or 0, "priced": row[1] or 0, "prs_households": int(row[2] or 0), "units_y1": int(row[3] or 0),
            "units_y2": int(row[4] or 0), "units_y3": int(row[5] or 0), "partner_net_y1": row[6], "hq_contribution_y1": row[7],
            "states": {s: states.get(s, 0) for s in DISTRICT_STATES}, "bands": {b: bands.get(b, 0) for b in BANDS}}


def _areas(db: Session) -> list[str]:
    return [a for (a,) in db.query(PostcodeDistrict.area).distinct().order_by(PostcodeDistrict.area).all()]


def _partner_people(db: Session) -> list[Person]:
    return db.query(Person).filter(Person.hnh_partner_id.isnot(None)).order_by(Person.person_key).all()


def _holder(db: Session, partner_id: int | None) -> Person | None:
    if partner_id is None:
        return None
    return db.query(Person).filter(Person.hnh_partner_id == partner_id).first()


def _common(db: Session, user: User, filters: dict) -> dict:
    run = latest_run(db)
    return {"user": user, "section": "territories", "filters": filters, "qs": qs(filters), "bands": BANDS, "states": DISTRICT_STATES,
            "nations": NATIONS, "areas": _areas(db), "band_labels": BAND_LABELS, "state_labels": STATE_LABELS,
            "nation_labels": NATION_LABELS, "money": money, "run": run, "sample": run_is_sample(run),
            "unconfirmed": tr.unconfirmed_assumptions(db)}


# ---- list ---------------------------------------------------------------------------------------------
@router.get("/territories", response_class=HTMLResponse)
def territories_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    filters = _filters(request)
    base = _filtered(db, filters)
    totals = _totals(base)
    rows, total, page, pages = page_of(_ordered(base), request, PAGE_SIZE)
    a = tr.load_assumptions(db)
    ctx = _common(db, user, filters)
    badges = confirm_badges(set(ctx["unconfirmed"]))
    state_links = {st: qs({**filters, "state": "" if filters["state"] == st else st}) for st in DISTRICT_STATES}
    band_links = {b: qs({**filters, "band": "" if filters["band"] == b else b}) for b in BANDS}
    ctx.update(rows=[(d, price_for(d, a)) for d in rows], total=total, page=page, pages=pages, totals=totals, badges=badges,
               qs_prev=qs(filters, page=page - 1), qs_next=qs(filters, page=page + 1), page_size=PAGE_SIZE,
               state_links=state_links, band_links=band_links)
    return render(request, "territories/list.html", ctx)


# ---- map ---------------------------------------------------------------------------------------------
def _json_for_script(data) -> Markup:
    """JSON safe inside a <script type="application/json"> block: no </script> and no entity can break out."""
    text = json.dumps(data, default=str, separators=(",", ":"))
    return Markup(text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))


@router.get("/territories/map", response_class=HTMLResponse)
def territories_map(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    filters = _filters(request)
    rows = _ordered(_filtered(db, filters)).all()
    centroids, meta = load_centroids()
    a = tr.load_assumptions(db)
    ctx = _common(db, user, filters)
    badges = confirm_badges(set(ctx["unconfirmed"]))
    any_badge = any(badges.values())
    points: list[dict] = []
    missing: list[str] = []
    for d in rows:
        at = centroids.get(d.district)
        price = price_for(d, a)
        if at is None:
            missing.append(d.district)
        points.append({
            "district": d.district, "area": d.area, "nation": NATION_LABELS.get(d.nation, d.nation), "la_name": d.la_name,
            "band": BAND_LABELS.get(d.band, d.band), "state": d.state, "partner_id": d.partner_id,
            "reserved_until": d.reserved_until.isoformat() if d.reserved_until else None,
            "lat": at[0] if at else None, "lon": at[1] if at else None,
            "prs_households": d.prs_households, "units_y1": d.units_y1, "units_y3": d.units_y3,
            "achievable_fee": money(d.achievable_fee), "partner_net_y1": money(d.partner_net_y1),
            "hq_contribution_y1": money(d.hq_contribution_y1), "upfront_fee": money(price["upfront_fee"]),
            "monthly_fee": money(price["monthly_fee"]), "confirm": any_badge,
        })
    data = {"districts": points, "sample": ctx["sample"] or meta["sample"], "centroids": meta, "state_labels": STATE_LABELS}
    ctx.update(points=len(points) - len(missing), missing=missing, centroids_meta=meta, data_json=_json_for_script(data), badges=badges)
    return render(request, "territories/map.html", ctx)


# ---- detail -------------------------------------------------------------------------------------------
def _same(stored, live) -> bool:
    """A stored column against the figure the current assumptions give: numbers compared as Decimal, the band as text."""
    if stored is None or live is None:
        return stored is None and live is None
    if isinstance(stored, str) or isinstance(live, str):
        return str(stored) == str(live)
    return Decimal(str(stored)) == Decimal(str(live))


def _detail(request: Request, db: Session, user: User, d: PostcodeDistrict, *, vals: dict | None = None, error: str = "",
            status: int = 200) -> HTMLResponse:
    a = tr.load_assumptions(db)
    share = d.cost_share if d.cost_share is not None else Decimal(1)
    live = tr.model_district(district=d.district, la_name=d.la_name, prs_households=d.prs_households, rent_2bed=d.rent_2bed,
                             la_gss=d.la_gss, assumptions=a, fixed_share=share, systems_fee_share=share)
    stored = {"band": d.band, "achievable_fee": d.achievable_fee, "cost_to_serve": d.cost_to_serve, "units_y1": d.units_y1,
              "units_y2": d.units_y2, "units_y3": d.units_y3, "partner_net_y1": d.partner_net_y1, "hq_contribution_y1": d.hq_contribution_y1}
    moved = sorted(k for k, v in stored.items() if not _same(v, live[k]))
    centroids, meta = load_centroids()
    history = (db.query(AuditLog).options(selectinload(AuditLog.user)).filter(AuditLog.entity == f"district:{d.district}")
               .order_by(AuditLog.id.desc()).limit(20).all())
    today = utcnow().date()
    ctx = _common(db, user, _filters(request))
    people = _partner_people(db)
    people_choices = [("", "Nobody chosen")] + [(str(p.id), f"{p.name or p.person_key} (partner {p.hnh_partner_id})") for p in people]
    ctx.update(d=d, live=live, moved=moved, badges=confirm_badges(set(ctx["unconfirmed"])), centroid=centroids.get(d.district),
               centroids_meta=meta, history=history, holder=_holder(db, d.partner_id), people_choices=people_choices, today=today,
               vals=vals or {"partner_id": "", "person_id": "", "reserved_until": add_months(today, RESERVE_MONTHS).isoformat()},
               reserve_months=RESERVE_MONTHS, blended_split=live["blended_split"], cost_share=share,
               cost_share_known=d.cost_share is not None)
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "territories/detail.html", ctx, status)


@router.get("/territories/{district}", response_class=HTMLResponse)
def territory_detail(district: str, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _detail(request, db, user, _load(db, district))


# ---- holding changes ------------------------------------------------------------------------------------
def _partner_from_form(db: Session, partner_id: str, person_id: str) -> tuple[int | None, list[str]]:
    """The partner id typed, or the chosen person's Hello New Home partner id; both must agree when both are given."""
    errors: list[str] = []
    pid: int | None = None
    if person_id.strip():
        person = db.get(Person, parse_int(person_id, -1))
        if person is None:
            errors.append("Choose a person who is on the register.")
        elif person.hnh_partner_id is None:
            errors.append(f"{person.person_key} has no Hello New Home partner id yet: set it under Linked ids on their person record first.")
        else:
            pid = person.hnh_partner_id
    typed = partner_id.strip()
    if typed:
        if not typed.isdigit() or int(typed) <= 0:
            errors.append("The partner id must be a whole number above zero.")
        elif pid is not None and int(typed) != pid:
            errors.append(f"The partner id typed ({typed}) and the chosen person's partner id ({pid}) differ: use one or the other.")
        else:
            pid = int(typed)
    return pid, errors


def _vals(partner_id: str, person_id: str, reserved_until: str) -> dict:
    return {"partner_id": partner_id.strip(), "person_id": person_id.strip(), "reserved_until": reserved_until.strip()}


@router.post("/territories/{district}/reserve", response_class=HTMLResponse)
def territory_reserve(district: str, request: Request, partner_id: str = Form(""), person_id: str = Form(""), reserved_until: str = Form(""),
                      db: Session = Depends(get_db), user: User = Depends(require_admin)):
    d = _load(db, district)
    vals = _vals(partner_id, person_id, reserved_until)
    errors: list[str] = []
    if d.state == "hq":
        errors.append(f"{d.district} is kept for head office and is never offered. Release it first if that has changed.")
    elif d.state == "core":
        errors.append(f"{d.district} is a core district held by partner {d.partner_id}. Release it first.")
    pid, problems = _partner_from_form(db, partner_id, person_id)
    errors.extend(problems)
    if pid is None and not problems:
        errors.append("Reserve it for whom? Give a Hello New Home partner id, or choose a person who has one.")
    today = utcnow().date()
    until: date | None = None
    try:
        until = parse_date(vals["reserved_until"], "reserved-until date")
    except ValueError as e:
        errors.append(str(e))
    if until is None and not any("reserved-until" in e for e in errors):
        until = add_months(today, RESERVE_MONTHS)
    if until is not None:
        if until <= today:
            errors.append("The reservation must end after today.")
        elif until > add_months(today, MAX_RESERVE_MONTHS):
            errors.append(f"A reservation longer than {MAX_RESERVE_MONTHS} months is almost certainly a typo; first refusal runs {RESERVE_MONTHS} months.")
    if errors:
        return _detail(request, db, user, d, vals=vals, error=" ".join(errors), status=400)
    assert pid is not None and until is not None
    before = holding_snapshot(d)
    d.state, d.partner_id, d.reserved_until = "reserved", pid, until
    b, after = changes(before, holding_snapshot(d))
    if not after:
        return go(f"/territories/{d.district}", ok="Nothing changed: it was already reserved for that partner until that date.")
    db.add(d)
    audit(db, user, "territory.reserve", f"district:{d.district}", before=b, after=after)
    db.commit()
    return go(f"/territories/{d.district}", ok=f"{d.district} reserved for partner {pid} until {until:%d %b %Y}. Hello New Home is not told from here yet.")


@router.post("/territories/{district}/assign", response_class=HTMLResponse)
def territory_assign(district: str, request: Request, partner_id: str = Form(""), person_id: str = Form(""),
                     db: Session = Depends(get_db), user: User = Depends(require_admin)):
    d = _load(db, district)
    vals = _vals(partner_id, person_id, "")
    errors: list[str] = []
    pid, problems = _partner_from_form(db, partner_id, person_id)
    errors.extend(problems)
    if pid is None and not problems:
        errors.append("Assign it to whom? Give a Hello New Home partner id, or choose a person who has one.")
    if d.state == "hq":
        errors.append(f"{d.district} is kept for head office and is never offered. Release it first if that has changed.")
    elif d.state == "core" and pid is not None and d.partner_id != pid:
        errors.append(f"{d.district} is already a core district held by partner {d.partner_id}. Release it first.")
    if errors:
        return _detail(request, db, user, d, vals=vals, error=" ".join(errors), status=400)
    assert pid is not None
    before = holding_snapshot(d)
    d.state, d.partner_id, d.reserved_until = "core", pid, None
    b, after = changes(before, holding_snapshot(d))
    if not after:
        return go(f"/territories/{d.district}", ok=f"Nothing changed: {d.district} was already core for partner {pid}.")
    db.add(d)
    audit(db, user, "territory.assign", f"district:{d.district}", before=b, after=after)
    db.commit()
    return go(f"/territories/{d.district}", ok=f"{d.district} is now a core district for partner {pid}. Hello New Home is not told from here yet.")


@router.post("/territories/{district}/release", response_class=HTMLResponse)
def territory_release(district: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    d = _load(db, district)
    if d.state == "open" and d.partner_id is None:
        return go(f"/territories/{d.district}", ok=f"{d.district} is already open.")
    before = holding_snapshot(d)
    d.state, d.partner_id, d.reserved_until = "open", None, None
    b, after = changes(before, holding_snapshot(d))
    db.add(d)
    audit(db, user, "territory.release", f"district:{d.district}", before=b, after=after)
    db.commit()
    return go(f"/territories/{d.district}", ok=f"{d.district} released: it is open again and leads route to head office.")


@router.post("/territories/{district}/hq", response_class=HTMLResponse)
def territory_hq(district: str, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    d = _load(db, district)
    if d.state == "hq":
        return go(f"/territories/{d.district}", ok=f"{d.district} is already kept for head office.")
    if d.partner_id is not None:
        return go(f"/territories/{d.district}", err=f"{d.district} is held by partner {d.partner_id}. Release it first.")
    before = holding_snapshot(d)
    d.state, d.partner_id, d.reserved_until = "hq", None, None
    b, after = changes(before, holding_snapshot(d))
    db.add(d)
    audit(db, user, "territory.hq", f"district:{d.district}", before=b, after=after)
    db.commit()
    return go(f"/territories/{d.district}", ok=f"{d.district} is kept for head office and will not be offered.")


# ---- the export -------------------------------------------------------------------------------------------
def _token_ok(request: Request) -> bool:
    if not EXPORT_TOKEN:
        return False
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip(), EXPORT_TOKEN)


@router.get("/api/territories.json")
def territories_export(request: Request, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    """State, district and area for every district, nothing else: no money and no band (the band is an unconfirmed
    pricing classification). An admin session or the bearer token TERRITORY_EXPORT_TOKEN (for Hello New Home's
    proxy) is required; anything else is a 401, never a redirect."""
    if user is None and not _token_ok(request):
        return JSONResponse({"ok": False, "error": "A Group HQ session or the export bearer token is required."}, status_code=401,
                            headers={"Cache-Control": "no-store"})
    rows = _ordered(db.query(PostcodeDistrict)).with_entities(*(getattr(PostcodeDistrict, f) for f in EXPORT_FIELDS)).all()
    run = latest_run(db)
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "model_run": run.id if run else None,
        "sample": run_is_sample(run),
        "states": {s: STATE_LABELS[s].split(":")[0] for s in DISTRICT_STATES},
        "count": len(rows),
        "districts": [dict(zip(EXPORT_FIELDS, row)) for row in rows],
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})
