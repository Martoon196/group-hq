"""Property deals: the buying side of Group HQ. One board from a seller's first message to completion, on the
LAPS board's machinery (stage stamps, audited moves), with a working checklist at every stage.

A deal can be started here or from an Inbox conversation, which hands over the person's name and the channel
only; no message text is copied.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import Acquisition, AcquisitionThread, User, utcnow
from ..services import acquisitions as service
from ..services import inbox as inbox_service
from ..services import inbox_crm, inbox_organisation, laps
from ..services.inbox_providers import InboxError
from ..services.audit import audit, changes
from ..templating import Refused, go, render
from .laps import _arrived, gbp, percent
from .people import get_or_404

router = APIRouter(dependencies=[Depends(check_origin)])

COMPLETED_SHOWN_DAYS = 90
MESSAGES_SHOWN = 30
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


def _mark_property(db: Session, user: User, account, bound: dict) -> None:
    """Attaching a conversation to a property deal files it under Property in the Inbox too. Commits."""
    settings = inbox_organisation.choices(db, user.id, account, bound["thread"],
                                          inbox_crm.links(db, account, bound["thread"]))
    if settings["kind"] != "property":
        inbox_organisation.save(db, user, account, bound["thread"], "property", settings["brands"],
                                settings["sync_brands"], settings["note"])


def _conversations(db: Session, user: User, deal: Acquisition) -> list[dict]:
    """The attached Inbox conversations with their latest messages, read through the Inbox as this user. Nothing
    is stored here: an account that is gone, or one attached by someone else, shows as unavailable."""
    from .inbox import thread_context
    mine = {a.key: a for a in inbox_service.accounts(db) if not a.owner_email or a.owner_email == user.email.lower()}
    result = []
    for link in service.attached(db, deal.id):
        entry = {"link": link, "account": mine.get(link.account_key), "messages": [], "error": None, "ticket": None}
        if entry["account"] is None or link.user_id != user.id:
            entry["error"] = "This conversation's inbox account is not available to you here."
        else:
            try:
                entry["ticket"] = inbox_service.ticket(entry["account"], link.thread_id, user.id, link.name,
                                                       link.participant_id)
                context = thread_context(db, user, entry["ticket"])
                entry["messages"] = list(context["messages"])[-MESSAGES_SHOWN:]
                entry["error"] = context["thread_error"]
            except (HTTPException, InboxError):
                entry["error"] = "The messages could not be loaded just now. Open the conversation in the Inbox."
        result.append(entry)
    return result


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
def start(request: Request, seller_name: str = Form(""), source: str = Form(""), thread_ticket: str = Form(""),
          db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """The add form, opened and filled with the name and channel of an Inbox conversation. Nothing is saved
    until the form is submitted; the conversation's ticket rides along so the new deal can be attached to it."""
    if thread_ticket:
        inbox_service.resolve(thread_ticket, user.id, user.email, db)       # refuses a stale or foreign ticket
    return _board(request, db, user, {"seller_name": seller_name.strip()[:200], "source": source.strip()[:100],
                                      "thread_ticket": thread_ticket})


def create_deal(db: Session, user: User, form: dict[str, str]) -> tuple[Acquisition, str]:
    """Add a property deal (the board's Add form, and the operator tool acquisition_create) from the form's fields by
    name. A thread_ticket from an Inbox conversation attaches the deal to it and files the conversation under
    Property. Audits and commits. Returns (the deal, the screen's flash); raises Refused with the form's error, or
    HTTPException(409) for a stale or foreign ticket, nothing saved."""
    now = utcnow()
    try:
        fields = _clean(form)
        lead_at = _arrived(form.get("arrived", ""), now)
        stage = form.get("stage") or "lead"
        if stage not in service.PIPELINE:
            raise ValueError("Choose a stage for the new deal.")
    except ValueError as error:
        raise Refused(str(error)) from None
    ticket = form.get("thread_ticket", "")
    account, bound = inbox_service.resolve(ticket, user.id, user.email, db) if ticket else (None, None)
    deal = Acquisition(**fields, stage="lead", lead_at=lead_at, checks={})
    service.move(deal, stage, lead_at)
    db.add(deal)
    db.flush()
    audit(db, user, "acquisition.add", f"acquisition:{deal.id}", after=_snapshot(deal))
    if account is not None:
        service.attach(db, deal, account, bound, user.id)
        audit(db, user, "acquisition.attach", f"acquisition:{deal.id}", after={"account": account.key})
    db.commit()
    if account is not None:
        _mark_property(db, user, account, bound)
    return deal, f"{deal.title} added to {service.LABELS[deal.stage].lower()}."


@router.post("/acquisitions/deals")
async def add_deal(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        deal, message = create_deal(db, user, form)
    except Refused as refusal:
        return go("/acquisitions#add", err=str(refusal))
    return go(f"/acquisitions/deals/{deal.id}", ok=message)


@router.get("/acquisitions/deals/{deal_id}", response_class=HTMLResponse)
def deal_page(deal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    pounds = {name.removesuffix("_pence"): ("" if getattr(deal, name) is None
              else f"{getattr(deal, name) / 100:.2f}".removesuffix(".00")) for name in PRICE_FIELDS}
    conversations = _conversations(db, user, deal)
    response = render(request, "acquisition.html", {
        "user": user, "section": "acquisitions", "deal": deal, "pipeline": service.PIPELINE,
        "conversations": conversations, "channels": inbox_service.CHANNELS,
        "labels": service.LABELS, "moves": service.MOVES, "kinds": service.KINDS, "plans": service.PLANS,
        "sellers": service.SELLERS, "gbp": gbp, "percent": percent, "pounds": pounds,
        "numbers": service.numbers(deal), "progress": service.progress(deal),
        "furthest": laps.furthest(deal, service.PIPELINE), "price_on_move": PRICE_ON_MOVE})
    if conversations:
        response.headers["Cache-Control"] = "no-store"          # the page carries private messages
    return response


def edit_deal(db: Session, user: User, deal: Acquisition, form: dict[str, str]) -> str:
    """Save the deal's details (the deal page's form, and the operator tool acquisition_edit) from the form's fields by
    name; a field the form does not carry is saved blank, as on the screen. Audits and commits when something
    changed. Returns the screen's flash; raises Refused with the form's error, nothing saved."""
    try:
        fields = _clean(form)
    except ValueError as error:
        raise Refused(str(error)) from None
    before = _snapshot(deal)
    for key, item in fields.items():
        setattr(deal, key, item)
    was, now_is = changes(before, _snapshot(deal))
    if now_is:
        audit(db, user, "acquisition.edit", f"acquisition:{deal.id}", before=was, after=now_is)
        db.commit()
    return "Saved."


@router.post("/acquisitions/deals/{deal_id}")
async def save_deal(deal_id: int, request: Request, db: Session = Depends(get_db),
                    user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    try:
        message = edit_deal(db, user, deal, form)
    except Refused as refusal:
        return go(f"/acquisitions/deals/{deal.id}", err=str(refusal))
    return go(f"/acquisitions/deals/{deal.id}", ok=message)


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


@router.post("/acquisitions/link")
def link(thread_ticket: str = Form(...), deal_id: int = Form(...), db: Session = Depends(get_db),
         user: User = Depends(require_admin)):
    """Attach an Inbox conversation to an existing deal, from the conversation."""
    from urllib.parse import urlencode
    account, bound = inbox_service.resolve(thread_ticket, user.id, user.email, db)
    deal = get_or_404(db, Acquisition, deal_id)
    back = "/inbox?" + urlencode({"account": account.key, "thread": thread_ticket})
    if not service.attach(db, deal, account, bound, user.id):
        return go(back, err=f"This conversation is already on {deal.title}.")
    audit(db, user, "acquisition.attach", f"acquisition:{deal.id}", after={"account": account.key})
    db.commit()
    _mark_property(db, user, account, bound)
    return go(back, ok=f"Conversation added to {deal.title}. Its messages now show on the deal.")


@router.post("/acquisitions/deals/{deal_id}/unlink")
def unlink(deal_id: int, account_key: str = Form(...), thread_sha256: str = Form(...),
           db: Session = Depends(get_db), user: User = Depends(require_admin)):
    deal = get_or_404(db, Acquisition, deal_id)
    row = db.get(AcquisitionThread, (deal.id, account_key, thread_sha256))
    if row is None:
        return go(f"/acquisitions/deals/{deal.id}", err="That conversation is not on this deal.")
    db.delete(row)
    audit(db, user, "acquisition.detach", f"acquisition:{deal.id}", before={"account": account_key})
    db.commit()
    return go(f"/acquisitions/deals/{deal.id}", ok="Conversation removed from this deal. The Inbox still has it.")
