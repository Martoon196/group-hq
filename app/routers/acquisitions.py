"""Property deals: the buying side of Group HQ. One board from a seller's first message to completion, on the
LAPS board's machinery (stage stamps, audited moves), with a working checklist at every stage.

A deal can be started here or from an Inbox conversation, which hands over the person's name and the channel
only; no message text is copied.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import Acquisition, User, utcnow
from ..services import acquisitions as service
from ..services import laps
from ..services.audit import audit, changes
from ..templating import go, render
from .laps import _arrived, gbp, percent
from .people import get_or_404

router = APIRouter(dependencies=[Depends(check_origin)])

COMPLETED_SHOWN_DAYS = 90
TEXT_FIELDS = {"postcode": 10, "seller_name": 200, "seller_contact": 200, "situation": 300, "source": 100,
               "solicitor": 200, "next_step": 200, "lost_reason": 200}
PRICE_FIELDS = ("asking_pence", "offer_pence", "agreed_pence", "value_pence", "works_pence")
EDITABLE = ("title", "kind", "units", "plan", "seller_type", "target_completion", "next_step_on", "notes",
            *TEXT_FIELDS, *PRICE_FIELDS)
PRICE_ON_MOVE = {"offer": "offer_pence", "agreed": "agreed_pence"}       # the price a move to this stage can set


def short(pence: int | None) -> str:
    """£1.2m, £450k: for chart labels and cards where the exact figure sits elsewhere."""
    if pence is None:
        return ""
    pounds = pence / 100
    if abs(pounds) >= 1_000_000:
        return f"£{pounds / 1_000_000:.2f}".rstrip("0").rstrip(".") + "m"
    if abs(pounds) >= 10_000:
        return f"£{round(pounds / 1000)}k"
    return gbp(int(pence))


def _snapshot(deal: Acquisition) -> dict:
    return {k: getattr(deal, k) for k in EDITABLE} | {"stage": deal.stage}


def _day(raw: str, what: str) -> date | None:
    if not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f"Enter {what} as a date.") from None


def _clean(form: dict) -> dict:
    """Validated field values from a posted form. Raises ValueError with a sentence for the flash."""
    title = " ".join(form.get("title", "").split())
    if not title:
        raise ValueError("Give the deal an address or a working name.")
    fields = {"title": title[:200], "notes": form.get("notes", "").strip()[:5000]}
    for name, labels in (("kind", service.KINDS), ("plan", service.PLANS), ("seller_type", service.SELLERS)):
        fields[name] = form.get(name) or ("other" if name == "kind" else next(iter(labels)))
        if fields[name] not in labels:
            raise ValueError("Choose one of the listed options.")
    units = form.get("units", "").strip()
    if units and (not units.isdigit() or not 1 <= int(units) <= 10000):
        raise ValueError("Enter the number of properties or units as a whole number.")
    fields["units"] = int(units) if units else None
    for name, limit in TEXT_FIELDS.items():
        fields[name] = form.get(name, "").strip()[:limit]
    fields["postcode"] = fields["postcode"].upper()
    for name in PRICE_FIELDS:
        fields[name] = service.parse_price(form.get(name.removesuffix("_pence"), ""))
    fields["target_completion"] = _day(form.get("target_completion", ""), "the target completion date")
    fields["next_step_on"] = _day(form.get("next_step_on", ""), "the next step date")
    return fields


def _board(request: Request, db: Session, user: User, prefill: dict | None = None) -> HTMLResponse:
    now = utcnow()
    windows = laps.periods(now)
    today = now.replace(tzinfo=timezone.utc).astimezone(laps.LONDON).date()
    year_start = datetime(today.year, 1, 1, tzinfo=laps.LONDON).astimezone(timezone.utc).replace(tzinfo=None)
    columns = {stage: db.scalars(select(Acquisition).where(Acquisition.stage == stage).order_by(
        Acquisition.next_step_on.asc().nulls_last(), Acquisition.lead_at.desc(), Acquisition.id.desc())).all()
        for stage in service.OPEN}
    columns["completed"] = db.scalars(select(Acquisition).where(
        Acquisition.stage == "completed", Acquisition.completed_at >= now - timedelta(days=COMPLETED_SHOWN_DAYS)
    ).order_by(Acquisition.completed_at.desc(), Acquisition.id.desc())).all()
    totals = service.stage_totals(db)
    return render(request, "acquisitions.html", {
        "user": user, "section": "acquisitions", "pipeline": service.PIPELINE, "labels": service.LABELS,
        "moves": service.MOVES, "kinds": service.KINDS, "plans": service.PLANS, "sellers": service.SELLERS,
        "columns": columns, "today": today, "gbp": gbp, "short": short, "percent": percent,
        "numbers": service.numbers, "progress": service.progress, "price_on_move": PRICE_ON_MOVE,
        "lost": db.scalars(select(Acquisition).where(Acquisition.stage == "lost").order_by(
            Acquisition.lost_at.desc(), Acquisition.id.desc()).limit(20)).all(),
        "lost_total": db.scalar(select(func.count()).where(Acquisition.stage == "lost")) or 0,
        "month": laps.reached_in(db, Acquisition, service.PIPELINE, *windows["this_month"]),
        "last_month": laps.reached_in(db, Acquisition, service.PIPELINE, *windows["last_month"]),
        "totals": totals, "most": max([t["count"] for t in totals] + [1]),
        "open_count": sum(t["count"] for t in totals), "open_pence": sum(t["pence"] for t in totals),
        "year": today.year, "completed_year": service.completed_between(db, year_start, now + timedelta(seconds=1)),
        "completed_days": COMPLETED_SHOWN_DAYS, "prefill": prefill or {},
    })


@router.get("/acquisitions", response_class=HTMLResponse)
def board(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _board(request, db, user)


@router.post("/acquisitions/start", response_class=HTMLResponse)
def start(request: Request, seller_name: str = Form(""), source: str = Form(""), db: Session = Depends(get_db),
          user: User = Depends(require_admin)):
    """The add form, opened and filled with the name and channel of an Inbox conversation. Nothing is saved."""
    return _board(request, db, user, {"seller_name": seller_name.strip()[:200], "source": source.strip()[:100]})


@router.post("/acquisitions/deals")
async def add_deal(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    now = utcnow()
    try:
        fields = _clean(form)
        lead_at = _arrived(form.get("arrived", ""), now)
        stage = form.get("stage") or "lead"
        if stage not in service.PIPELINE:
            raise ValueError("Choose a stage for the new deal.")
    except ValueError as error:
        return go("/acquisitions#add", err=str(error))
    deal = Acquisition(**fields, stage="lead", lead_at=lead_at, checks={})
    service.move(deal, stage, lead_at)
    db.add(deal)
    db.flush()
    audit(db, user, "acquisition.add", f"acquisition:{deal.id}", after=_snapshot(deal))
    db.commit()
    return go(f"/acquisitions/deals/{deal.id}", ok=f"{deal.title} added to {service.LABELS[deal.stage].lower()}.")


@router.get("/acquisitions/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(deal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    pounds = {name.removesuffix("_pence"): ("" if getattr(deal, name) is None
              else f"{getattr(deal, name) / 100:.2f}".removesuffix(".00")) for name in PRICE_FIELDS}
    return render(request, "acquisition.html", {
        "user": user, "section": "acquisitions", "deal": deal, "pipeline": service.PIPELINE,
        "labels": service.LABELS, "moves": service.MOVES, "kinds": service.KINDS, "plans": service.PLANS,
        "sellers": service.SELLERS, "gbp": gbp, "percent": percent, "pounds": pounds,
        "numbers": service.numbers(deal), "progress": service.progress(deal),
        "furthest": laps.furthest(deal, service.PIPELINE), "price_on_move": PRICE_ON_MOVE})


@router.post("/acquisitions/deals/{deal_id}")
async def save_deal(deal_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        fields = _clean(form)
    except ValueError as error:
        return go(f"/acquisitions/deals/{deal.id}", err=str(error))
    before = _snapshot(deal)
    for key, item in fields.items():
        setattr(deal, key, item)
    was, now_is = changes(before, _snapshot(deal))
    if now_is:
        audit(db, user, "acquisition.edit", f"acquisition:{deal.id}", before=was, after=now_is)
        db.commit()
    return go(f"/acquisitions/deals/{deal.id}", ok="Saved.")


@router.post("/acquisitions/deals/{deal_id}/stage")
def move_deal(deal_id: int, to: str = Form(...), price: str = Form(""), lost_reason: str = Form(""),
              back: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    done = "/acquisitions" if back == "board" else f"/acquisitions/deals/{deal.id}"
    if to not in service.MOVES:
        return go(done, err="Choose where to move this deal.")
    before = _snapshot(deal)
    try:
        if price.strip() and to in PRICE_ON_MOVE:
            setattr(deal, PRICE_ON_MOVE[to], service.parse_price(price))
        service.move(deal, to)
    except ValueError as error:
        db.rollback()
        return go(done, err=str(error))
    if to == "lost":
        deal.lost_reason = lost_reason.strip()[:200] or deal.lost_reason
    was, now_is = changes(before, _snapshot(deal))
    if not now_is:
        return go(done)
    audit(db, user, "acquisition.move", f"acquisition:{deal.id}", before=was, after=now_is)
    db.commit()
    return go(done, ok=f"{deal.title} moved to {service.LABELS[deal.stage].lower()}.")


@router.post("/acquisitions/deals/{deal_id}/check")
def check(deal_id: int, key: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    today = utcnow().replace(tzinfo=timezone.utc).astimezone(laps.LONDON).date().isoformat()
    try:
        ticked = service.toggle(deal, key, today)
    except ValueError as error:
        return go(f"/acquisitions/deals/{deal.id}", err=str(error))
    audit(db, user, "acquisition.check", f"acquisition:{deal.id}", after={key: ticked})
    db.commit()
    return go(f"/acquisitions/deals/{deal.id}#checklist")
