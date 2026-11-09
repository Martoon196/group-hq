"""The LAPS board: one screen for the sales rhythm (Leads, Appointments, Presentations, Sales), the income goal
worked backwards into weekly numbers, and the cleared money and clients recorded elsewhere in Group HQ.

Deals are entered and moved here. The money and client panels are read only: they show what the mentee,
franchise and Growth screens already hold. A CRM lead synced on the CRM screen can be added to the board once.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import CampaignLead, FranchiseOpportunity, LapsDeal, Mentee, User, utcnow
from ..services import laps, laps_sync
from ..services.audit import audit, changes
from ..templating import go, render
from .people import get_or_404

router = APIRouter(dependencies=[Depends(check_origin)])

SALES_SHOWN_DAYS = 30
ORIGINS = {"crm": ("CRM", "/laps/source/"), "franchise": ("Franchise pipeline", "/franchise/")}
EDITABLE = ("name", "contact", "brand", "offer", "source", "value_pence", "next_step", "next_step_on", "notes")
MOVES = {"lead": "Back to lead", "appointment": "Appointment booked", "presentation": "Presentation done",
         "sale": "Sale made", "lost": "Mark lost", "reopen": "Reopen"}


def gbp(pence: int | None) -> str:
    if pence is None:
        return ""
    pounds, rest = divmod(abs(int(pence)), 100)
    return ("-" if pence < 0 else "") + f"£{pounds:,}" + (f".{rest:02d}" if rest else "")


def initials(name: str) -> str:
    parts = [p for p in name.split() if p[:1].isalnum()]
    return ((parts[0][:1] + (parts[-1][:1] if len(parts) > 1 else "")) if parts else "?").upper()


def origin_of(deal: LapsDeal) -> dict | None:
    """Where an automatic deal came from: a short label and the record it follows."""
    kind, _, ref = (deal.origin or "").partition(":")
    if kind not in ORIGINS or not ref.isdigit():
        return None
    label, path = ORIGINS[kind]
    return {"label": label, "url": path + ref}


def siblings(db: Session, deals) -> dict[int, list[LapsDeal]]:
    """For each deal that shares a party, the same person's cards under other businesses."""
    parties = {d.party for d in deals if d.party}
    if not parties:
        return {}
    rows = db.scalars(select(LapsDeal).where(LapsDeal.party.in_(parties)).order_by(LapsDeal.id)).all()
    return {d.id: [r for r in rows if r.party == d.party and r.id != d.id] for d in deals if d.party}


def percent(rate: float | None) -> str:
    return "" if rate is None else f"{round(rate * 100)}%"


def _snapshot(deal: LapsDeal) -> dict:
    return {k: getattr(deal, k) for k in EDITABLE} | {"stage": deal.stage}


def _clean(name: str, contact: str, brand: str, offer: str, source: str, value: str, next_step: str,
           next_step_on: str, notes: str) -> dict:
    """Validated field values for a deal. Raises ValueError with a sentence for the flash."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("Give the lead a name.")
    if brand not in laps.BRANDS:
        raise ValueError("Choose which business this lead is for.")
    due = None
    if next_step_on.strip():
        try:
            due = date.fromisoformat(next_step_on.strip())
        except ValueError:
            raise ValueError("Enter the next step date as a date.") from None
    return {"name": name[:200], "contact": contact.strip()[:200], "brand": brand, "offer": offer.strip()[:120],
            "source": source.strip()[:100], "value_pence": laps.parse_pounds(value),
            "next_step": next_step.strip()[:200], "next_step_on": due, "notes": notes.strip()[:5000]}


def _arrived(raw: str, now: datetime) -> datetime:
    """When the lead came in: now for today (or blank), midday UK for an earlier day. Never the future."""
    if not raw.strip():
        return now
    try:
        day = date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError("Enter the date the lead came in as a date.") from None
    today = now.replace(tzinfo=timezone.utc).astimezone(laps.LONDON).date()
    if day > today:
        raise ValueError("A lead cannot come in on a future date.")
    if day == today:
        return now
    return datetime(day.year, day.month, day.day, 12, tzinfo=laps.LONDON).astimezone(timezone.utc).replace(tzinfo=None)


def _board_url(brand: str = "") -> str:
    return "/laps" + (f"?brand={brand}" if brand in laps.BRANDS else "")


@router.get("/laps", response_class=HTMLResponse)
def board(request: Request, background_tasks: BackgroundTasks, brand: str = "", db: Session = Depends(get_db),
          user: User = Depends(require_admin)):
    """The board places the leads this database already holds on every visit, and asks the CRM for fresh ones
    in the background when the last pull is older than laps_sync.STALE."""
    laps_sync.refresh_local(db)
    if laps_sync.due(db):
        background_tasks.add_task(laps_sync.run_background)
    return _board(request, db, user, brand)


@router.post("/laps/refresh")
def refresh(view: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    result = laps_sync.refresh(db, user)
    if result["problem"]:
        return go(_board_url(view), err=result["problem"])
    return go(_board_url(view), ok=f"Leads refreshed: {result['checked']} CRM contacts checked, "
                                   f"{result['created']} new on the board, {result['moved']} moved on.")


@router.post("/laps/start", response_class=HTMLResponse)
def start(request: Request, name: str = Form(""), source: str = Form(""), db: Session = Depends(get_db),
          user: User = Depends(require_admin)):
    """The add form, opened and filled with the name and channel of an Inbox conversation. Nothing is saved."""
    return _board(request, db, user, "", {"name": " ".join(name.split())[:200], "source": source.strip()[:100]})


def _board(request: Request, db: Session, user: User, brand: str = "", prefill: dict | None = None) -> HTMLResponse:
    brand = brand if brand in laps.BRANDS else ""
    now = utcnow()
    windows = laps.periods(now)
    scoped = (lambda q: q.where(LapsDeal.brand == brand)) if brand else (lambda q: q)
    columns = {}
    for stage in laps.PIPELINE[:-1]:
        columns[stage] = db.scalars(scoped(select(LapsDeal).where(LapsDeal.stage == stage)).order_by(
            LapsDeal.next_step_on.asc().nulls_last(), LapsDeal.lead_at.desc(), LapsDeal.id.desc())).all()
    columns["sale"] = db.scalars(scoped(select(LapsDeal).where(
        LapsDeal.stage == "sale", LapsDeal.sale_at >= now - timedelta(days=SALES_SHOWN_DAYS))).order_by(
        LapsDeal.sale_at.desc(), LapsDeal.id.desc())).all()
    lost = db.scalars(scoped(select(LapsDeal).where(LapsDeal.stage == "lost")).order_by(
        LapsDeal.lost_at.desc(), LapsDeal.id.desc()).limit(20)).all()
    lost_total = db.scalar(scoped(select(func.count()).where(LapsDeal.stage == "lost"))) or 0
    open_value = int(db.scalar(scoped(select(func.coalesce(func.sum(LapsDeal.value_pence), 0)).where(
        LapsDeal.stage.in_(laps.PIPELINE[:-1])))) or 0)

    stats = laps.cohort(db, now, brand or None)
    goals = laps.targets(db)
    weeks = [{"monday": monday, **laps.counts(db, start, end, brand or None)}
             for monday, start, end in laps.week_starts(now)]

    automatic = {kind: db.scalar(select(func.count()).where(LapsDeal.origin.like(kind + ":%"))) or 0 for kind in ORIGINS}
    by_hand = db.scalar(select(func.count()).where(LapsDeal.origin.is_(None))) or 0
    crm_counts = dict(db.execute(select(LapsDeal.brand, func.count()).where(LapsDeal.origin.like("crm:%"))
                                 .group_by(LapsDeal.brand)).all())

    today = now.replace(tzinfo=timezone.utc).astimezone(laps.LONDON).date()
    return render(request, "laps.html", {
        "user": user, "section": "laps", "brand": brand, "brands": laps.BRANDS, "labels": laps.LABELS,
        "pipeline": laps.PIPELINE, "columns": columns, "lost": lost, "lost_total": lost_total,
        "open_value": open_value, "today": today, "moves": MOVES, "gbp": gbp, "percent": percent,
        "score": {name: laps.counts(db, *windows[name], brand or None)
                  for name in ("this_week", "last_week", "this_month", "last_month")},
        "weeks": weeks, "stats": stats, "goals": goals,
        "plan": laps.plan(goals["monthly_goal_pence"], goals["typical_sale_pence"], stats),
        "money": {name: laps.money_in(db, *windows[name]) for name in ("this_month", "last_month")},
        "clients": {
            "mentees": db.scalar(select(func.count()).where(Mentee.status == "active")) or 0,
            "territories": db.scalar(select(func.count()).where(
                FranchiseOpportunity.stage.in_(("paid", "onboarding")))) or 0,
            "territory_open": db.scalar(select(func.count()).where(
                FranchiseOpportunity.stage.not_in(("paid", "onboarding", "lost")))) or 0,
        },
        "automatic": automatic, "by_hand": by_hand, "sync": laps_sync.state(db), "origin_of": origin_of,
        "crm_brands": laps_sync.CRM_BRANDS, "crm_counts": crm_counts,
        "siblings": siblings(db, [d for column in columns.values() for d in column]),
        "sales_days": SALES_SHOWN_DAYS, "prefill": prefill or {}, "initials": initials,
    })


@router.post("/laps/deals")
def add_deal(name: str = Form(""), contact: str = Form(""), brand: list[str] = Form([]), offer: str = Form(""),
             source: str = Form(""), value: str = Form(""), next_step: str = Form(""), next_step_on: str = Form(""),
             notes: str = Form(""), arrived: str = Form(""), stage: str = Form("lead"), view: str = Form(""),
             db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """One card per business ticked. Cards made together share a party, so each shows the others."""
    now = utcnow()
    chosen = list(dict.fromkeys(brand))
    try:
        if not chosen:
            raise ValueError("Choose which business this lead is for.")
        each = [_clean(name, contact, b, offer, source, value, next_step, next_step_on, notes) for b in chosen]
        lead_at = _arrived(arrived, now)
        if stage not in laps.PIPELINE:
            raise ValueError("Choose a stage for the new lead.")
    except ValueError as error:
        return go(_board_url(view) + "#add", err=str(error))
    party = str(uuid.uuid4()) if len(each) > 1 else None
    deals = []
    for fields in each:
        deal = LapsDeal(**fields, stage="lead", lead_at=lead_at, party=party)
        laps.move(deal, stage, lead_at)
        db.add(deal)
        deals.append(deal)
    db.flush()
    for deal in deals:
        audit(db, user, "laps.deal.add", f"laps_deal:{deal.id}", after=_snapshot(deal))
    db.commit()
    where = laps.LABELS[deals[0].stage].lower()
    if len(deals) > 1:
        return go(_board_url(view), ok=f"{deals[0].name} added to {where} for {len(deals)} businesses.")
    return go(_board_url(view), ok=f"{deals[0].name} added to {where}.")


@router.post("/laps/deals/{deal_id}/also")
def also(deal_id: int, brand: list[str] = Form([]), db: Session = Depends(get_db),
         user: User = Depends(require_admin)):
    """The same person wants something from another business: a new lead card there, linked to this one."""
    deal = get_or_404(db, LapsDeal, deal_id)
    taken = {deal.brand} | {d.brand for d in siblings(db, [deal]).get(deal.id, [])}
    chosen = [b for b in dict.fromkeys(brand) if b in laps.BRANDS and b not in taken]
    if not chosen:
        return go(f"/laps/deals/{deal.id}", err="Choose a business this person is not already under.")
    if not deal.party:
        deal.party = str(uuid.uuid4())
    now = utcnow()
    for b in chosen:
        twin = LapsDeal(name=deal.name, contact=deal.contact, brand=b, source=deal.source, stage="lead", lead_at=now,
                        party=deal.party)
        db.add(twin)
        db.flush()
        audit(db, user, "laps.deal.add", f"laps_deal:{twin.id}", after=_snapshot(twin) | {"with": deal.id})
    db.commit()
    names = ", ".join(laps.BRANDS[b] for b in chosen)
    return go(f"/laps/deals/{deal.id}", ok=f"{deal.name} added as a lead for {names}.")


@router.get("/laps/source/{lead_id}")
def source(lead_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """The CRM record an automatic card follows: the campaign screen for Property Predator, the CRM contact
    view for the other businesses."""
    lead = get_or_404(db, CampaignLead, lead_id)
    if lead.brand == "pp":
        return RedirectResponse(f"/crm/leads/{lead.id}", status_code=303)
    return RedirectResponse(f"/inbox/crm/contact?brand={lead.brand}&contact={lead.external_id}", status_code=303)


@router.get("/laps/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(deal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, LapsDeal, deal_id)
    return render(request, "laps_deal.html", {
        "user": user, "section": "laps", "deal": deal, "brands": laps.BRANDS, "labels": laps.LABELS,
        "pipeline": laps.PIPELINE, "moves": MOVES, "gbp": gbp, "furthest": laps.furthest(deal),
        "origin": origin_of(deal), "siblings": siblings(db, [deal]).get(deal.id, []),
        "value": "" if deal.value_pence is None else f"{deal.value_pence / 100:.2f}".removesuffix(".00")})


@router.post("/laps/deals/{deal_id}")
def save_deal(deal_id: int, name: str = Form(""), contact: str = Form(""), brand: str = Form(""),
              offer: str = Form(""), source: str = Form(""), value: str = Form(""), next_step: str = Form(""),
              next_step_on: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db),
              user: User = Depends(require_admin)):
    deal = get_or_404(db, LapsDeal, deal_id)
    try:
        fields = _clean(name, contact, brand, offer, source, value, next_step, next_step_on, notes)
    except ValueError as error:
        return go(f"/laps/deals/{deal.id}", err=str(error))
    before = _snapshot(deal)
    for key, item in fields.items():
        setattr(deal, key, item)
    was, now_is = changes(before, _snapshot(deal))
    if now_is:
        audit(db, user, "laps.deal.edit", f"laps_deal:{deal.id}", before=was, after=now_is)
        db.commit()
    return go(f"/laps/deals/{deal.id}", ok="Saved.")


@router.post("/laps/deals/{deal_id}/stage")
def move_deal(deal_id: int, to: str = Form(...), value: str = Form(""), view: str = Form(""),
              back: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, LapsDeal, deal_id)
    done = f"/laps/deals/{deal.id}" if back == "deal" else _board_url(view)
    if to not in MOVES:
        return go(done, err="Choose where to move this lead.")
    before = _snapshot(deal)
    try:
        if value.strip():
            deal.value_pence = laps.parse_pounds(value)
        laps.move(deal, to)
    except ValueError as error:
        db.rollback()
        return go(done, err=str(error))
    if deal.stage == before["stage"] and deal.value_pence == before["value_pence"]:
        return go(done)
    was, now_is = changes(before, _snapshot(deal))
    audit(db, user, "laps.deal.move", f"laps_deal:{deal.id}", before=was, after=now_is)
    db.commit()
    message = f"{deal.name} moved to {laps.LABELS[deal.stage].lower()}."
    if deal.stage == "sale" and deal.value_pence is None:
        message += " Add the sale value so it counts towards income."
    return go(done, ok=message)


@router.post("/laps/targets")
def save_targets(monthly_goal: str = Form(""), typical_sale: str = Form(""), view: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(require_admin)):
    try:
        goal, typical = laps.parse_pounds(monthly_goal), laps.parse_pounds(typical_sale)
    except ValueError as error:
        return go(_board_url(view) + "#plan", err=str(error))
    before = laps.targets(db)
    after = laps.save_targets(db, goal or None, typical or None, user.email)
    was, now_is = changes(before, after)
    if now_is:
        audit(db, user, "laps.targets", "laps", before=was, after=now_is)
    db.commit()
    return go(_board_url(view) + "#plan", ok="Income goal saved.")
