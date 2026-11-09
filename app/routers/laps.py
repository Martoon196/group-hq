"""The LAPS board: one screen for the sales rhythm (Leads, Appointments, Presentations, Sales), the income goal
worked backwards into weekly numbers, and the cleared money and clients recorded elsewhere in Group HQ.

Deals are entered and moved here. The money and client panels are read only: they show what the mentee,
franchise and Growth screens already hold. A CRM lead synced on the CRM screen can be added to the board once.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import CampaignLead, FranchiseOpportunity, LapsDeal, Mentee, User, utcnow
from ..services import laps
from ..services.audit import audit, changes
from ..templating import go, render
from .people import get_or_404

router = APIRouter(dependencies=[Depends(check_origin)])

SALES_SHOWN_DAYS = 30
CRM_SUGGESTIONS = 8
EDITABLE = ("name", "contact", "brand", "offer", "source", "value_pence", "next_step", "next_step_on", "notes")
MOVES = {"lead": "Back to lead", "appointment": "Appointment booked", "presentation": "Presentation done",
         "sale": "Sale made", "lost": "Mark lost", "reopen": "Reopen"}


def gbp(pence: int | None) -> str:
    if pence is None:
        return ""
    pounds, rest = divmod(abs(int(pence)), 100)
    return ("-" if pence < 0 else "") + f"£{pounds:,}" + (f".{rest:02d}" if rest else "")


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
def board(request: Request, brand: str = "", db: Session = Depends(get_db), user: User = Depends(require_admin)):
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

    taken = {o for o in db.scalars(select(LapsDeal.origin).where(LapsDeal.origin.like("crm:%"))).all()}
    crm_query = select(CampaignLead).where(CampaignLead.lifecycle == "lead").order_by(
        CampaignLead.synced_at.desc(), CampaignLead.id.desc())
    if brand:
        crm_query = crm_query.where(CampaignLead.brand == brand)
    crm_leads = [lead for lead in db.scalars(crm_query.limit(200)).all() if f"crm:{lead.id}" not in taken]

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
        "crm_leads": crm_leads[:CRM_SUGGESTIONS], "crm_waiting": len(crm_leads),
        "sales_days": SALES_SHOWN_DAYS,
    })


@router.post("/laps/deals")
def add_deal(name: str = Form(""), contact: str = Form(""), brand: str = Form(""), offer: str = Form(""),
             source: str = Form(""), value: str = Form(""), next_step: str = Form(""), next_step_on: str = Form(""),
             notes: str = Form(""), arrived: str = Form(""), stage: str = Form("lead"), view: str = Form(""),
             db: Session = Depends(get_db), user: User = Depends(require_admin)):
    now = utcnow()
    try:
        fields = _clean(name, contact, brand, offer, source, value, next_step, next_step_on, notes)
        lead_at = _arrived(arrived, now)
        if stage not in laps.PIPELINE:
            raise ValueError("Choose a stage for the new lead.")
    except ValueError as error:
        return go(_board_url(view) + "#add", err=str(error))
    deal = LapsDeal(**fields, stage="lead", lead_at=lead_at)
    laps.move(deal, stage, lead_at)
    db.add(deal)
    db.flush()
    audit(db, user, "laps.deal.add", f"laps_deal:{deal.id}", after=_snapshot(deal))
    db.commit()
    return go(_board_url(view), ok=f"{deal.name} added to {laps.LABELS[deal.stage].lower()}.")


@router.post("/laps/import")
def import_crm_lead(lead_id: int = Form(...), view: str = Form(""), db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    lead = get_or_404(db, CampaignLead, lead_id)
    origin = f"crm:{lead.id}"
    if lead.brand not in laps.BRANDS:
        return go(_board_url(view), err="That CRM lead belongs to a business the board does not track.")
    if db.scalar(select(LapsDeal.id).where(LapsDeal.origin == origin)):
        return go(_board_url(view), err=f"{lead.display_name} is already on the board.")
    deal = LapsDeal(name=lead.display_name[:200], brand=lead.brand, source=(lead.source or "CRM")[:100],
                    stage="lead", lead_at=utcnow(), origin=origin)
    db.add(deal)
    db.flush()
    audit(db, user, "laps.deal.import", f"laps_deal:{deal.id}", after=_snapshot(deal) | {"origin": origin})
    db.commit()
    return go(_board_url(view), ok=f"{deal.name} added to leads.")


@router.get("/laps/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(deal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, LapsDeal, deal_id)
    return render(request, "laps_deal.html", {
        "user": user, "section": "laps", "deal": deal, "brands": laps.BRANDS, "labels": laps.LABELS,
        "pipeline": laps.PIPELINE, "moves": MOVES, "gbp": gbp, "furthest": laps.furthest(deal),
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
