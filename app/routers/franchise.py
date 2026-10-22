"""The franchise pipeline: territory sales from enquiry to paid and onboarding (plan, Phase 4).

    GET  /franchise                          the board: one column per stage, counts, overdue follow-ups
    GET  /franchise/list                     the list with filters (stage, search, seller)
    GET  /franchise/new, POST                an opportunity from a person (or a person created inline),
                                             districts, a derived band, a quote, an optional seller code
    GET  /franchise/{id}                     stage, quote, districts, activities, tasks, payments, packs, hand-off
    POST /franchise/{id}/stage               move the stage (audited, stage_changed_at); pack_sent creates
                                             the day 2, 5, 9 and 14 follow-ups; Paid and Onboarding need a
                                             cleared upfront fee (or the fee waived) unless Override is ticked
                                             with a note, which the audit row and the timeline record
    POST /franchise/{id}/requote             recompute the quote from the districts and today's assumptions
    POST /franchise/{id}/details             notes, source and seller attribution
    POST /franchise/{id}/activities          a call, email or note on the timeline
    POST /franchise/{id}/tasks/{tid}/done    mark a follow-up done (an activity is written) or skipped
    POST /franchise/{id}/tasks/{tid}/skip
    POST /franchise/{id}/tasks/{tid}/send    send the task's email through SMTP, when it is configured
    POST /franchise/{id}/payments            record a fee received (upfront or one trail month), in pounds
    POST /franchise/{id}/payments/{pid}/clear  mark it cleared, which is when commission can accrue
    POST /franchise/{id}/payments/{pid}/refund mark it refunded; the ledger claws back inside 90 days
    POST /franchise/{id}/hnh                 "Create Local Partner in Hello New Home" through the HNH connector,
                                             only at Paid or Onboarding with the upfront fee cleared (or waived)
    GET  /franchise/{id}/pack/territory      the territory report print view
    GET  /franchise/packs/prospectus         the prospectus print view

Money rule (docs/pricing-bands.md): a quote is copied onto the opportunity when it is made and never moves
on its own; every quoted or modelled figure is a proposal until the founder confirms the assumptions, so
the screens badge it [CONFIRM] and the packs withhold it. Partners never see any of this: Group HQ is
admin-only and the packs are printed by head office.

The quote for several districts (proposal, [CONFIRM]): the band is the highest band among them; the fee per
unit is the achievable fee weighted by each district's private-rented households (units are proportional
to households), and the upfront fee is the plan's 6 x the partner's share at 30 units on that fee, rounded
to 500 with the band's floor. For one district this is exactly territory_rules.model_district()'s figure.

The seller commission ledger (app.services.commission, packet C) is imported leniently, as the entitlement
engine is in app.routers.mentees: when a payment clears and the ledger is present, its accrual function is
called; when it is absent, a pending note is written on the timeline and the audit log so nothing is
forgotten. FranchisePayment (franchise_payments, alembic/versions/0004_franchise_payments.py) is in
app/models.py with the other tables, so Alembic's autogenerate sees it.
"""
from __future__ import annotations

import inspect
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup
from sqlalchemy import Text, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..auth import check_origin, normalise_email, require_admin
from ..db import get_db
from ..models import (FRANCHISE_PAYMENT_BASES, OPPORTUNITY_STAGES, PAYMENT_METHODS, Activity, CommissionEntry, ConnectorCall,
                      FranchiseOpportunity, FranchisePayment, Mentee, Person, PostcodeDistrict, Programme, Seller, Task, User, utcnow)
from ..services import followups, packs, settings
from ..services import territory_rules as tr
from ..services.audit import audit, changes
from ..services.connectors import ConnectorError, HNHConnector, product_dry_run
from ..templating import go, render
from .mentees import pounds_to_pence
from .people import find_person_by_key, like_pattern, page_of, parse_date, parse_int, person_key_problem, qs

try:
    from ..services import commission as commission_ledger
except ImportError:          # packet C's ledger is not in this tree yet; payments still clear and a note is left
    commission_ledger = None  # type: ignore[assignment]

router = APIRouter(prefix="/franchise", dependencies=[Depends(check_origin)])

STAGE_LABELS = {
    "enquiry": "Enquiry", "discovery": "Discovery", "pack_sent": "Pack sent", "follow_up": "Follow-up",
    "agreement": "Agreement", "paid": "Paid", "onboarding": "Onboarding", "lost": "Lost",
}
BAND_LABELS = {"london": "London", "major_city": "Major city", "town": "Town", "rural": "Rural"}
BAND_RANK = {band: rank for rank, band in enumerate(("rural", "town", "major_city", "london"))}
OFFERABLE_STATES = ("open", "reserved")
ACTIVITY_CHOICES = (("note", "Note"), ("call", "Call"), ("email", "Email"))
METHOD_LABELS = {"manual": "Bank transfer (recorded by hand)", "stripe_link": "Stripe payment link", "invoice": "Invoice"}
BOARD_COLUMN_LIMIT = 30
LEDGER_FUNCTIONS = ("on_payment_cleared", "accrue_payment", "accrue_for_payment", "record_cleared_payment", "accrue")
LEDGER_MISSING = ("The seller commission ledger (app.services.commission) is not installed on this server, so the entry "
                  "has been noted as pending on the timeline rather than written.")
HNH_ALREADY = "This person already has a Hello New Home partner id ({pid}); assign the districts on the HNH admin partner page."
MENTORSHIP = "mentorship"
MONEY_STAGES = ("paid", "onboarding")        # reached only on a cleared upfront fee, a waived fee, or an audited override
QUOTE_FROZEN_STAGES = ("agreement", "paid", "onboarding")   # heads of terms signed: the quote no longer moves
NEEDS_MONEY = ("{label} needs a cleared upfront payment (or the fee waived). Record and clear the payment first, or tick "
               "Override and say why in the note.")


# ---- the quote -------------------------------------------------------------------------------------------
def highest_band(bands: list[str]) -> str:
    """docs/pricing-bands.md: an opportunity covering several districts takes the highest band among them [CONFIRM]."""
    known = [b for b in bands if b in BAND_RANK]
    return max(known, key=BAND_RANK.__getitem__) if known else "rural"


def weighted_fee(districts: list[PostcodeDistrict], assumptions: dict) -> tuple[Decimal | None, bool]:
    """(fee per unit, weighted) across the districts: the achievable fee from each district's two-bed rent
    under today's assumptions (falling back to the loader's stored figure), weighted by private-rented
    households, or a plain mean when no counts exist. None when no district has a rent on file."""
    pairs: list[tuple[Decimal, int | None]] = []
    for d in districts:
        fee = tr.achievable_fee(d.rent_2bed, assumptions["fee_cap"], assumptions["management_pct"]) if d.rent_2bed is not None \
            else (tr.money(d.achievable_fee) if d.achievable_fee is not None else None)
        if fee is not None:
            pairs.append((fee, d.prs_households))
    if not pairs:
        return None, False
    if len(pairs) == 1:
        return pairs[0][0], False
    weights_known = all(w is not None and int(w) > 0 for _f, w in pairs)
    if weights_known:
        total = sum(int(w) for _f, w in pairs)   # type: ignore[arg-type]
        return tr.money(sum(f * int(w) for f, w in pairs) / total), True   # type: ignore[arg-type]
    return tr.money(sum(f for f, _w in pairs) / len(pairs)), False


def quote_for(districts: list[PostcodeDistrict], assumptions: dict, *, waived: bool = False) -> dict:
    """The band and the two fees for a set of district rows, in pence, plus how the figure was reached. With
    waived=True (a mentee, docs/pricing-bands.md section 4) both fees are zero and the band is still recorded."""
    band = highest_band([d.band for d in districts])
    fee, weighted = weighted_fee(districts, assumptions)
    floor = tr.upfront_floor(band, assumptions)
    if fee is None:
        upfront = floor
        floor_applied = True
    else:
        at_30 = tr.partner_net_month(int(assumptions["upfront_units"]), fee, assumptions["partner_split_own"], 0, 1)
        upfront = tr.upfront_fee(at_30, assumptions["upfront_multiplier"], floor, round_to_step=assumptions["upfront_round_to"])
        floor_applied = upfront == floor
    monthly = tr.monthly_fee(band, assumptions)
    quote = {
        "band": band, "fee": fee, "weighted": weighted, "floor_applied": floor_applied,
        "upfront": Decimal("0.00") if waived else upfront, "monthly": Decimal("0.00") if waived else monthly, "waived": waived,
    }
    quote["upfront_pence"] = packs.to_pence(quote["upfront"])
    quote["monthly_pence"] = packs.to_pence(quote["monthly"])
    return quote


def quote_note(quote: dict, codes: list[str]) -> str:
    """The timeline entry that records how a quote was reached, so a later change of assumptions is visible."""
    if quote["waived"]:
        basis = "fees waived (mentee: upfront waived, monthly waived while the mentorship is active)"
    elif quote["fee"] is None:
        basis = "no rent on file for any district, so the band floor applies"
    else:
        how = "weighted by private-rented households" if quote["weighted"] else "from the district's two-bed rent"
        basis = f"fee per unit {packs.money(quote['fee'])} {how}" + ("; the band floor applies" if quote["floor_applied"] else "")
    return (f"Quoted: band {BAND_LABELS.get(quote['band'], quote['band'])}, upfront {packs.money(quote['upfront'])}, "
            f"monthly {packs.money(quote['monthly'])}, for {', '.join(codes)}; {basis}. Every figure is a proposal until "
            f"the assumptions are confirmed [CONFIRM].")


def is_fee_waived(opportunity: FranchiseOpportunity) -> bool:
    return opportunity.quoted_upfront_pence == 0 and opportunity.quoted_monthly_pence == 0


def active_mentorship(db: Session, person: Person) -> bool:
    row = (db.query(Mentee.id).join(Programme, Programme.id == Mentee.programme_id)
           .filter(Mentee.person_id == person.id, Mentee.status == "active", Programme.code == MENTORSHIP).first())
    return row is not None


# ---- lookups ---------------------------------------------------------------------------------------------------
def parse_districts(raw: list[str]) -> list[str]:
    seen: list[str] = []
    for item in raw:
        for part in str(item).replace(",", " ").split():
            code = part.strip().upper()
            if code and code not in seen:
                seen.append(code)
    return seen


def offerable_districts(db: Session) -> list[PostcodeDistrict]:
    return (db.query(PostcodeDistrict).filter(PostcodeDistrict.state.in_(OFFERABLE_STATES))
            .order_by(PostcodeDistrict.area, func.length(PostcodeDistrict.district), PostcodeDistrict.district).all())


def district_rows(db: Session, codes: list[str]) -> list[PostcodeDistrict]:
    """The rows for the codes, in the codes' order; a code with no row is left out (the caller counts)."""
    if not codes:
        return []
    rows = {r.district: r for r in db.query(PostcodeDistrict).filter(PostcodeDistrict.district.in_(codes)).all()}
    return [rows[c] for c in codes if c in rows]


def find_seller(db: Session, code: str) -> Seller | None:
    code = (code or "").strip()
    if not code:
        return None
    return db.query(Seller).options(selectinload(Seller.person)).filter(func.lower(Seller.code) == code.lower()).first()


def _load(db: Session, opportunity_id: int) -> FranchiseOpportunity:
    row = (db.query(FranchiseOpportunity)
           .options(selectinload(FranchiseOpportunity.person), selectinload(FranchiseOpportunity.seller).selectinload(Seller.person),
                    selectinload(FranchiseOpportunity.activities), selectinload(FranchiseOpportunity.tasks))
           .filter(FranchiseOpportunity.id == opportunity_id).first())
    if row is None:
        raise HTTPException(status_code=404, detail=f"Opportunity {opportunity_id} was not found.")
    return row


def snapshot(o: FranchiseOpportunity) -> dict:
    return {"person_id": o.person_id, "stage": o.stage, "districts": list(o.districts or []), "band": o.band,
            "quoted_upfront_pence": o.quoted_upfront_pence, "quoted_monthly_pence": o.quoted_monthly_pence,
            "seller_id": o.seller_id, "source": o.source, "notes": o.notes}


def _note(db: Session, opportunity: FranchiseOpportunity, body: str, user: User | str, kind: str = "note") -> Activity:
    actor = user.email if isinstance(user, User) else str(user)
    row = Activity(opportunity_id=opportunity.id, kind=kind, body=body[:20_000], actor=actor[:200])
    db.add(row)
    return row


def _common(user: User, db: Session) -> dict:
    confirmed = packs.assumptions_confirmed(db)
    return {"user": user, "section": "franchise", "stage_labels": STAGE_LABELS, "band_labels": BAND_LABELS,
            "stages": OPPORTUNITY_STAGES, "confirmed": confirmed, "unconfirmed": 0 if confirmed else packs.unconfirmed_count(db),
            "pounds": packs.pounds, "money": packs.money, "number": packs.number, "today": utcnow().date()}


# ---- the board and the list ---------------------------------------------------------------------------------
def overdue_tasks(db: Session, today: date) -> list[Task]:
    return (db.query(Task).options(selectinload(Task.opportunity).selectinload(FranchiseOpportunity.person))
            .filter(Task.state == "open", Task.due_on < today).order_by(Task.due_on, Task.id).all())


def due_tasks(db: Session, today: date, days: int = 2) -> list[Task]:
    return (db.query(Task).options(selectinload(Task.opportunity).selectinload(FranchiseOpportunity.person))
            .filter(Task.state == "open", Task.due_on >= today, Task.due_on <= today + timedelta(days=days))
            .order_by(Task.due_on, Task.id).all())


@router.get("", response_class=HTMLResponse)
def board(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    today = utcnow().date()
    counts = dict(db.query(FranchiseOpportunity.stage, func.count(FranchiseOpportunity.id)).group_by(FranchiseOpportunity.stage).all())
    open_by_opportunity = dict(db.query(Task.opportunity_id, func.count(Task.id)).filter(Task.state == "open").group_by(Task.opportunity_id).all())
    overdue_by_opportunity = dict(db.query(Task.opportunity_id, func.count(Task.id))
                                  .filter(Task.state == "open", Task.due_on < today).group_by(Task.opportunity_id).all())
    columns = []
    for stage in OPPORTUNITY_STAGES:
        rows = (db.query(FranchiseOpportunity).options(selectinload(FranchiseOpportunity.person))
                .filter(FranchiseOpportunity.stage == stage).order_by(FranchiseOpportunity.stage_changed_at.desc(), FranchiseOpportunity.id.desc())
                .limit(BOARD_COLUMN_LIMIT).all())
        columns.append({"stage": stage, "label": STAGE_LABELS[stage], "count": int(counts.get(stage, 0)), "rows": rows})
    overdue = overdue_tasks(db, today)
    ctx = {**_common(user, db), "columns": columns, "overdue": overdue, "due_soon": due_tasks(db, today),
           "open_by_opportunity": open_by_opportunity, "overdue_by_opportunity": overdue_by_opportunity,
           "total": sum(int(v) for v in counts.values()), "live": sum(int(v) for k, v in counts.items() if k not in ("lost", "onboarding")),
           "column_limit": BOARD_COLUMN_LIMIT}
    return render(request, "franchise/board.html", ctx)


@router.get("/list", response_class=HTMLResponse)
def listing(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    qp = request.query_params
    filters = {"stage": qp.get("stage", "").strip(), "q": qp.get("q", "").strip()[:120], "seller": qp.get("seller", "").strip()}
    q = (db.query(FranchiseOpportunity).options(selectinload(FranchiseOpportunity.person),
                                                selectinload(FranchiseOpportunity.seller).selectinload(Seller.person))
         .join(Person, Person.id == FranchiseOpportunity.person_id))
    if filters["stage"] in OPPORTUNITY_STAGES:
        q = q.filter(FranchiseOpportunity.stage == filters["stage"])
    if filters["seller"].isdigit():
        q = q.filter(FranchiseOpportunity.seller_id == int(filters["seller"]))
    if filters["q"]:
        like = like_pattern(filters["q"].lower())
        q = q.filter(or_(func.lower(Person.person_key).like(like, escape="\\"), func.lower(Person.name).like(like, escape="\\"),
                         func.lower(FranchiseOpportunity.districts.cast(Text)).like(like, escape="\\"),
                         func.lower(FranchiseOpportunity.source).like(like, escape="\\")))
    q = q.order_by(FranchiseOpportunity.id.desc())
    rows, total, page, pages = page_of(q, request)
    today = utcnow().date()
    overdue_by_opportunity = dict(db.query(Task.opportunity_id, func.count(Task.id))
                                  .filter(Task.state == "open", Task.due_on < today).group_by(Task.opportunity_id).all())
    sellers = db.query(Seller).options(selectinload(Seller.person)).order_by(Seller.code).all()
    ctx = {**_common(user, db), "rows": rows, "total": total, "page": page, "pages": pages, "filters": filters, "sellers": sellers,
           "overdue_by_opportunity": overdue_by_opportunity, "qs": qs(filters), "qs_prev": qs(filters, page=page - 1),
           "qs_next": qs(filters, page=page + 1)}
    return render(request, "franchise/list.html", ctx)


# ---- create --------------------------------------------------------------------------------------------------
def _form(request: Request, db: Session, user: User, vals: dict, *, error: str = "", status: int = 200) -> HTMLResponse:
    ctx = {**_common(user, db), "vals": vals, "people": db.query(Person).order_by(Person.person_key).all(),
           "districts": offerable_districts(db), "floors": tr.upfront_floors(tr.load_assumptions(db)),
           "monthly_fees": tr.monthly_fees(tr.load_assumptions(db))}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "franchise/form.html", ctx, status)


def _blank_vals(request: Request) -> dict:
    qp = request.query_params
    return {"person_id": qp.get("person_id", "").strip(), "email": "", "name": "", "phone": "", "districts": [],
            "seller_code": qp.get("seller", "").strip()[:24], "source": qp.get("source", "").strip()[:60], "fee_waived": "",
            "notes": ""}


@router.get("/new", response_class=HTMLResponse)
def new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    vals = _blank_vals(request)
    if vals["person_id"].isdigit():
        person = db.get(Person, int(vals["person_id"]))
        if person is not None and active_mentorship(db, person):
            vals["fee_waived"] = "1"
    return _form(request, db, user, vals)


@router.post("/new", response_class=HTMLResponse)
async def create(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    form = await request.form()

    def field(name: str, limit: int = 20_000) -> str:
        value = form.get(name)
        return (value if isinstance(value, str) else "").strip()[:limit]

    vals = {"person_id": field("person_id", 12), "email": field("email", 254), "name": field("name", 120), "phone": field("phone", 40),
            "districts": parse_districts([v for v in form.getlist("districts") if isinstance(v, str)]),
            "seller_code": field("seller_code", 24), "source": field("source", 60), "fee_waived": field("fee_waived", 1),
            "notes": field("notes")}
    errors: list[str] = []
    notes_for_flash: list[str] = []

    # the person: chosen from the register, or created (or found) from the inline fields
    person: Person | None = None
    created_person = False
    if vals["person_id"].isdigit():
        person = db.get(Person, int(vals["person_id"]))
        if person is None:
            errors.append("That person is not on the register.")
    elif vals["email"]:
        key = normalise_email(vals["email"])
        problem = person_key_problem(key)
        if problem:
            errors.append(problem)
        else:
            person = find_person_by_key(db, key)
            if person is not None:
                notes_for_flash.append(f"{key} was already on the register as person #{person.id}, so that record is used.")
            else:
                person = Person(person_key=key, name=vals["name"], phone=vals["phone"])
                db.add(person)
                created_person = True
    else:
        errors.append("Choose a person from the register or enter the enquirer's email to add them.")

    # the districts: offerable rows only
    rows = district_rows(db, vals["districts"])
    found = {r.district for r in rows}
    missing = [c for c in vals["districts"] if c not in found]
    if not vals["districts"]:
        errors.append("Choose at least one postcode district.")
    elif missing:
        errors.append(f"Not in the territory model: {', '.join(missing)}. Load the model (or add the district) first.")
    held = [r.district for r in rows if r.state not in OFFERABLE_STATES]
    if held:
        errors.append(f"Not available: {', '.join(held)} (held by HQ or already a partner's core district).")

    # the seller, by tracked-link code
    seller: Seller | None = None
    if vals["seller_code"]:
        seller = find_seller(db, vals["seller_code"])
        if seller is None:
            errors.append(f"No seller has the code {vals['seller_code']}.")
        elif not seller.active:
            errors.append(f"Seller {seller.code} is not active, so new enquiries cannot be attributed to them.")
        elif person is not None and person.id is not None and seller.person_id == person.id:
            errors.append("A seller cannot be attributed to their own purchase (docs/commission-rules.md).")
    if errors:
        db.rollback()
        return _form(request, db, user, vals, error=" ".join(errors), status=400)
    assert person is not None

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _form(request, db, user, vals, error="That email is already on the register.", status=400)
    if created_person:
        audit(db, user, "person.create", f"person:{person.id}", after={"person_key": person.person_key, "name": person.name,
                                                                       "phone": person.phone, "notes": ""})

    waived = bool(vals["fee_waived"])
    quote = quote_for(rows, tr.load_assumptions(db), waived=waived)
    source = vals["source"] or (f"seller:{seller.code}" if seller else "")
    opportunity = FranchiseOpportunity(person_id=person.id, stage="enquiry", districts=[r.district for r in rows], band=quote["band"],
                                       quoted_upfront_pence=quote["upfront_pence"], quoted_monthly_pence=quote["monthly_pence"],
                                       seller_id=seller.id if seller else None, source=source, notes=vals["notes"],
                                       stage_changed_at=utcnow())
    db.add(opportunity)
    db.flush()
    _note(db, opportunity, quote_note(quote, opportunity.districts), user)
    if seller:
        _note(db, opportunity, f"Attributed to seller {seller.code} ({seller.person.person_key}, {seller.kind}).", user)
    audit(db, user, "franchise.create", f"opportunity:{opportunity.id}", after={**snapshot(opportunity), "quote_basis": quote_note(quote, opportunity.districts)})
    db.commit()
    label = BAND_LABELS[quote["band"]]
    flash = (f"Opportunity #{opportunity.id} created for {person.person_key}: {label} band, "
             f"{'fees waived' if waived else packs.money(quote['upfront']) + ' upfront and ' + packs.money(quote['monthly']) + ' a month'} "
             f"[CONFIRM]. " + " ".join(notes_for_flash)).strip()
    return go(f"/franchise/{opportunity.id}", ok=flash)


# ---- detail ---------------------------------------------------------------------------------------------------
def _payments(db: Session, opportunity: FranchiseOpportunity) -> list[FranchisePayment]:
    return (db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == opportunity.id)
            .order_by(FranchisePayment.paid_at.desc(), FranchisePayment.id.desc()).all())


def _cleared_upfront(db: Session, opportunity: FranchiseOpportunity) -> FranchisePayment | None:
    """The upfront fee that has cleared and not been refunded, or None: what Paid, Onboarding and the Hello
    New Home hand-off are gated on when the fee is not waived."""
    return (db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == opportunity.id, FranchisePayment.basis == "upfront",
                                             FranchisePayment.cleared_at.isnot(None), FranchisePayment.refunded_at.is_(None)).first())


def _hnh_call(db: Session, opportunity: FranchiseOpportunity) -> ConnectorCall | None:
    return db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == _hnh_key(opportunity)).first()


def _hnh_key(opportunity: FranchiseOpportunity) -> str:
    return f"franchise:{opportunity.id}:hnh_partner"


def _page(request: Request, db: Session, user: User, opportunity: FranchiseOpportunity, *, error: str = "", status: int = 200,
          payment_vals: dict | None = None, activity_vals: dict | None = None, detail_vals: dict | None = None) -> HTMLResponse:
    confirmed = packs.assumptions_confirmed(db)
    today = utcnow().date()
    tasks = sorted(opportunity.tasks, key=lambda t: (t.state != "open", t.due_on, t.id))
    pack_sent_on = opportunity.stage_changed_at.date() if opportunity.stage == "pack_sent" else None
    emails = {t.id: followups.email_for(t.template_key, opportunity, confirmed=confirmed, pack_sent_on=pack_sent_on, today=today)
              for t in tasks}
    entries = (db.query(CommissionEntry).options(selectinload(CommissionEntry.seller).selectinload(Seller.person))
               .filter(CommissionEntry.opportunity_id == opportunity.id).order_by(CommissionEntry.id).all())
    districts = district_rows(db, [str(c) for c in opportunity.districts])
    ctx = {
        **_common(user, db), "o": opportunity, "person": opportunity.person, "districts": districts,
        "missing_districts": [c for c in opportunity.districts if c not in {d.district for d in districts}],
        "fee_waived": is_fee_waived(opportunity), "activities": sorted(opportunity.activities, key=lambda a: (a.at, a.id), reverse=True),
        "tasks": tasks, "emails": emails, "payments": _payments(db, opportunity), "entries": entries,
        "smtp_configured": followups.smtp_configured(), "hnh_call": _hnh_call(db, opportunity),
        "hnh_dry_run": product_dry_run("hnh", switches=settings.product_switches(db)), "ledger_available": _ledger_function() is not None,
        "activity_choices": ACTIVITY_CHOICES, "methods": [(k, v) for k, v in METHOD_LABELS.items()],
        "payment_vals": payment_vals or {"basis": "upfront", "trail_month": "", "amount": "", "paid_at": today.isoformat(), "cleared_at": "",
                                         "method": "manual", "reference": "", "notes": ""},
        "activity_vals": activity_vals or {"kind": "note", "body": ""},
        "detail_vals": detail_vals or {"notes": opportunity.notes, "source": opportunity.source,
                                       "seller_code": opportunity.seller.code if opportunity.seller else ""},
        "stage_choices": [(s, STAGE_LABELS[s]) for s in OPPORTUNITY_STAGES if s != opportunity.stage],
        "cleared_pence": sum(p.amount_pence for p in _payments(db, opportunity) if p.cleared_at and not p.refunded_at),
        "upfront_cleared": _cleared_upfront(db, opportunity) is not None, "money_stages": MONEY_STAGES, "quote_frozen_stages": QUOTE_FROZEN_STAGES,
        "refund_vals": {"refunded_at": today.isoformat()},
    }
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "franchise/detail.html", ctx, status)


@router.get("/packs/prospectus", response_class=HTMLResponse)
def prospectus(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    confirmed = packs.assumptions_confirmed(db)
    ctx = {"request": request, "body": Markup(_prospectus_body()), "confirmed": confirmed, "watermark": "" if confirmed else packs.WATERMARK,
           "unconfirmed": 0 if confirmed else packs.unconfirmed_count(db), "today": utcnow().date(), "user": user}
    return render(request, "packs/prospectus.html", ctx)


@lru_cache(maxsize=1)
def _prospectus_body() -> str:
    """The generated HTML, read once. It is plain HTML, not a Jinja template, so nothing in the source
    document can be interpreted as template syntax."""
    try:
        return packs.PROSPECTUS_BODY.read_text(encoding="utf-8")
    except OSError:
        return "<p>The prospectus has not been generated on this server. See app/services/packs.py.</p>"


@router.get("/{opportunity_id}", response_class=HTMLResponse)
def detail(opportunity_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _page(request, db, user, _load(db, opportunity_id))


@router.get("/{opportunity_id}/pack/territory", response_class=HTMLResponse)
def territory_pack(opportunity_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    ctx = packs.report_context(db, opportunity, today=utcnow().date())
    ctx.update({"request": request, "user": user, "band_labels": BAND_LABELS, "fee_waived": is_fee_waived(opportunity)})
    return render(request, "packs/territory.html", ctx)


# ---- stage changes -------------------------------------------------------------------------------------------
@router.post("/{opportunity_id}/stage", response_class=HTMLResponse)
def change_stage(opportunity_id: int, stage: str = Form(""), note: str = Form(""), override: str = Form(""), paid_at: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Paid and Onboarding are money stages: without a cleared, unrefunded upfront payment (or the fee waived) the
    move is refused, unless Override is ticked and a note says why, in which case the audit row carries
    override: true and the timeline says the stage was moved without cleared money. Moving to Paid with no
    payment on record creates the uncleared upfront payment, dated paid_at (today by default), to be cleared
    when the money arrives."""
    opportunity = _load(db, opportunity_id)
    stage = stage.strip()
    note = note.strip()[:20_000]
    overriding = bool(override.strip())
    back = f"/franchise/{opportunity.id}"
    if stage not in OPPORTUNITY_STAGES:
        return go(back, err="Choose a stage.")
    if stage == opportunity.stage:
        return go(back, ok=f"Already at {STAGE_LABELS[stage]}.")
    needs_money = stage in MONEY_STAGES and not is_fee_waived(opportunity)
    if stage == "paid" and opportunity.quoted_upfront_pence is None:
        return go(back, err="There is no quote on this opportunity. Press Requote first.")
    if needs_money and _cleared_upfront(db, opportunity) is None:
        if not overriding:
            return go(back, err=NEEDS_MONEY.format(label=STAGE_LABELS[stage]))
        if not note:
            return go(back, err="An override needs a note saying why the stage is moving without a cleared payment.")
    else:
        overriding = False      # nothing to override: the box is ignored and not recorded
    paid_on: date | None = None
    if stage == "paid":
        try:
            paid_on = parse_date(paid_at, "paid date") or utcnow().date()
        except ValueError as e:
            return go(back, err=str(e))
        if paid_on > utcnow().date():
            return go(back, err="The paid date cannot be in the future.")
    before = {"stage": opportunity.stage, "stage_changed_at": opportunity.stage_changed_at}
    now = utcnow()
    opportunity.stage = stage
    opportunity.stage_changed_at = now
    db.add(opportunity)
    extras: list[str] = []
    after: dict[str, Any] = {"stage": stage, "stage_changed_at": now}
    if overriding:
        after["override"] = True
        after["note"] = note
        _note(db, opportunity, f"Stage moved to {STAGE_LABELS[stage]} WITHOUT a cleared payment (override by {user.email}): {note}", user)
        extras.append("Moved without a cleared payment: the override and your note are in the audit log.")
    elif note:
        _note(db, opportunity, f"Stage moved to {STAGE_LABELS[stage]}: {note}", user)
        after["note"] = note
    else:
        _note(db, opportunity, f"Stage moved to {STAGE_LABELS[stage]}.", user)

    if stage == "pack_sent":
        created = followups.create_followups(db, opportunity, now.date())
        due = ", ".join(f"{t.title.split(':')[0]} on {t.due_on:%d %b}" for t in created)
        _note(db, opportunity, "Territory pack sent: the territory report is at "
                               f"/franchise/{opportunity.id}/pack/territory and the prospectus at /franchise/packs/prospectus. "
                               f"Follow-ups created: {due or 'none (a cadence was already running)'}.", user)
        after["tasks_created"] = [t.template_key for t in created]
        extras.append(f"{len(created)} follow-up task{'s' if len(created) != 1 else ''} created." if created else
                      "Follow-ups were already running, so none were added.")
        extras.append("Print the territory report and the prospectus from the Packs panel.")
    elif stage == "paid":
        if is_fee_waived(opportunity):
            _note(db, opportunity, "Fees waived: no upfront payment is due.", user)
            extras.append("Fees are waived, so no payment was recorded.")
        else:
            existing = (db.query(FranchisePayment).filter(FranchisePayment.opportunity_id == opportunity.id,
                                                         FranchisePayment.basis == "upfront", FranchisePayment.refunded_at.is_(None)).first())
            if existing is None:
                assert paid_on is not None
                payment = FranchisePayment(opportunity_id=opportunity.id, basis="upfront", amount_pence=opportunity.quoted_upfront_pence,
                                           paid_at=_at_midnight(paid_on), method="manual", notes="Recorded when the stage moved to Paid.")
                db.add(payment)
                db.flush()
                after["payment_id"] = payment.id
                audit(db, user, "franchise.payment.create", f"franchise_payment:{payment.id}",
                      after={"opportunity_id": opportunity.id, "basis": "upfront", "amount_pence": payment.amount_pence, "paid_at": payment.paid_at})
                extras.append(f"Upfront payment of {packs.pounds(payment.amount_pence)} recorded as paid on {paid_on:%d %b %Y}; "
                              "mark it cleared once the money has arrived.")
            elif existing.cleared_at is None:
                extras.append("The upfront payment is on record and still to be cleared.")
            else:
                extras.append("The upfront payment is on record and cleared.")
    elif stage == "onboarding":
        extras.append("Press Create Local Partner in Hello New Home when the agreement is signed.")
    audit(db, user, "franchise.stage", f"opportunity:{opportunity.id}", before=before, after=after)
    db.commit()
    return go(back, ok=f"Moved to {STAGE_LABELS[stage]}. " + " ".join(extras))


@router.post("/{opportunity_id}/requote", response_class=HTMLResponse)
def requote(opportunity_id: int, fee_waived: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    back = f"/franchise/{opportunity.id}"
    if opportunity.stage in QUOTE_FROZEN_STAGES:
        return go(back, err="A territory at Agreement or beyond keeps the quote it was sold on; the heads of terms carry it.")
    rows = district_rows(db, [str(c) for c in opportunity.districts])
    if not rows:
        return go(back, err="None of the districts is in the territory model, so there is nothing to quote from.")
    quote = quote_for(rows, tr.load_assumptions(db), waived=bool(fee_waived))
    before = {"band": opportunity.band, "quoted_upfront_pence": opportunity.quoted_upfront_pence, "quoted_monthly_pence": opportunity.quoted_monthly_pence}
    opportunity.band = quote["band"]
    opportunity.quoted_upfront_pence = quote["upfront_pence"]
    opportunity.quoted_monthly_pence = quote["monthly_pence"]
    b, a = changes(before, {"band": opportunity.band, "quoted_upfront_pence": opportunity.quoted_upfront_pence,
                            "quoted_monthly_pence": opportunity.quoted_monthly_pence})
    if not a:
        return go(back, ok="The quote is unchanged.")
    db.add(opportunity)
    _note(db, opportunity, "Requoted. " + quote_note(quote, [r.district for r in rows]), user)
    audit(db, user, "franchise.requote", f"opportunity:{opportunity.id}", before=b, after=a)
    db.commit()
    return go(back, ok=f"Requoted: {packs.money(quote['upfront'])} upfront and {packs.money(quote['monthly'])} a month [CONFIRM].")


@router.post("/{opportunity_id}/details", response_class=HTMLResponse)
def update_details(opportunity_id: int, request: Request, notes: str = Form(""), source: str = Form(""), seller_code: str = Form(""),
                   db: Session = Depends(get_db), user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    vals = {"notes": notes.strip()[:20_000], "source": source.strip()[:60], "seller_code": seller_code.strip()[:24]}
    seller: Seller | None = None
    if vals["seller_code"]:
        seller = find_seller(db, vals["seller_code"])
        if seller is None:
            return _page(request, db, user, opportunity, detail_vals=vals, error=f"No seller has the code {vals['seller_code']}.", status=400)
        if seller.person_id == opportunity.person_id:
            return _page(request, db, user, opportunity, detail_vals=vals, error="A seller cannot be attributed to their own purchase.", status=400)
        if not seller.active and seller.id != opportunity.seller_id:
            return _page(request, db, user, opportunity, detail_vals=vals, error=f"Seller {seller.code} is not active.", status=400)
    new_seller_id = seller.id if seller else None
    if new_seller_id != opportunity.seller_id:
        accrued = (db.query(CommissionEntry.id).filter(CommissionEntry.opportunity_id == opportunity.id,
                                                        CommissionEntry.clawback_of.is_(None)).first())
        if accrued is not None:
            return _page(request, db, user, opportunity, detail_vals=vals, status=400,
                         error="Commission has already accrued on this sale, so the seller cannot be changed here; claw the entries "
                               "back on the seller page first.")
    before = snapshot(opportunity)
    opportunity.notes = vals["notes"]
    opportunity.source = vals["source"]
    opportunity.seller_id = seller.id if seller else None
    b, a = changes(before, snapshot(opportunity))
    back = f"/franchise/{opportunity.id}"
    if not a:
        return go(back, ok="No changes to save.")
    db.add(opportunity)
    if "seller_id" in a:
        _note(db, opportunity, f"Seller attribution changed to {seller.code if seller else 'none'}.", user)
    audit(db, user, "franchise.update", f"opportunity:{opportunity.id}", before=b, after=a)
    db.commit()
    return go(back, ok="Saved.")


# ---- activities and tasks -----------------------------------------------------------------------------------
@router.post("/{opportunity_id}/activities", response_class=HTMLResponse)
def add_activity(opportunity_id: int, request: Request, kind: str = Form("note"), body: str = Form(""), db: Session = Depends(get_db),
                 user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    vals = {"kind": kind.strip(), "body": body.strip()[:20_000]}
    if vals["kind"] not in dict(ACTIVITY_CHOICES):
        return _page(request, db, user, opportunity, activity_vals=vals, error="Choose what happened: a note, a call or an email.", status=400)
    if not vals["body"]:
        return _page(request, db, user, opportunity, activity_vals=vals, error="Write what happened.", status=400)
    row = _note(db, opportunity, vals["body"], user, kind=vals["kind"])
    db.flush()
    audit(db, user, "franchise.activity", f"opportunity:{opportunity.id}", after={"activity_id": row.id, "kind": row.kind})
    db.commit()
    return go(f"/franchise/{opportunity.id}", ok="Added to the timeline.")


def _task(db: Session, opportunity: FranchiseOpportunity, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.opportunity_id != opportunity.id:
        raise HTTPException(status_code=404, detail=f"Task {task_id} was not found on this opportunity.")
    return task


def _finish(db: Session, opportunity: FranchiseOpportunity, task: Task, state: str, user: User, *, detail: str = "") -> None:
    task.state = state
    task.done_at = utcnow()
    db.add(task)
    if state == "done":
        _note(db, opportunity, detail or f"Done: {task.title}", user, kind="task_done")
    audit(db, user, f"franchise.task.{state}", f"task:{task.id}", before={"state": "open"}, after={"state": state, "title": task.title})


@router.post("/{opportunity_id}/tasks/{task_id}/done", response_class=HTMLResponse)
def task_done(opportunity_id: int, task_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    task = _task(db, opportunity, task_id)
    back = f"/franchise/{opportunity.id}"
    if task.state != "open":
        return go(back, ok=f"That task is already {task.state}.")
    _finish(db, opportunity, task, "done", user)
    db.commit()
    return go(back, ok=f"Done: {task.title}")


@router.post("/{opportunity_id}/tasks/{task_id}/skip", response_class=HTMLResponse)
def task_skip(opportunity_id: int, task_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    task = _task(db, opportunity, task_id)
    back = f"/franchise/{opportunity.id}"
    if task.state != "open":
        return go(back, ok=f"That task is already {task.state}.")
    _finish(db, opportunity, task, "skipped", user)
    db.commit()
    return go(back, ok=f"Skipped: {task.title}")


@router.post("/{opportunity_id}/tasks/{task_id}/send", response_class=HTMLResponse)
def task_send(opportunity_id: int, task_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Send the task's email to the person and mark the task done. Only when SMTP is configured; otherwise the
    text is on the page to copy."""
    opportunity = _load(db, opportunity_id)
    task = _task(db, opportunity, task_id)
    back = f"/franchise/{opportunity.id}"
    if task.state != "open":
        return go(back, err=f"That task is already {task.state}; nothing was sent.")
    email = followups.email_for(task.template_key, opportunity, confirmed=packs.assumptions_confirmed(db),
                                pack_sent_on=opportunity.stage_changed_at.date() if opportunity.stage == "pack_sent" else None,
                                today=utcnow().date())
    if email is None:
        return go(back, err="That task has no email template; write the email yourself and mark the task done.")
    if not followups.smtp_configured():
        return go(back, err="Email is not configured on this server (SMTP_HOST and MAIL_FROM). Copy the text from the task instead.")
    to = opportunity.person.person_key
    try:
        message_id = followups.send_email(to, email.subject, email.body, reply_to=config.MAIL_FROM)
    except Exception as e:   # noqa: BLE001  (an SMTP refusal must reach the screen and the audit log, never vanish)
        audit(db, user, "franchise.email_failed", f"task:{task.id}", after={"to": to, "subject": email.subject, "error": str(e)[:500]})
        db.commit()
        return go(back, err=f"The email was not sent: {str(e)[:300]}")
    _note(db, opportunity, f"Emailed {to}: {email.subject}\n\n{email.body}", user, kind="email")
    _finish(db, opportunity, task, "done", user, detail=f"Done: {task.title} (email sent)")
    audit(db, user, "franchise.email_sent", f"task:{task.id}", after={"to": to, "subject": email.subject, "message_id": message_id})
    db.commit()
    return go(back, ok=f"Sent to {to} and the task is done.")


# ---- payments and the commission hook -----------------------------------------------------------------------------
def _ledger_function():
    if commission_ledger is None:
        return None
    for name in LEDGER_FUNCTIONS:
        fn = getattr(commission_ledger, name, None)
        if callable(fn):
            return fn
    return None


def _call_ledger(fn, db: Session, opportunity: FranchiseOpportunity, payment: FranchisePayment, user: User):
    """Call the ledger with whatever subset of (opportunity, payment, basis, basis_pence, trail_month, cleared_at,
    seller, actor) its signature names, after the session as the first positional argument."""
    offered = {"opportunity": opportunity, "payment": payment, "basis": payment.basis, "basis_pence": payment.amount_pence,
               "trail_month": payment.trail_month, "cleared_at": payment.cleared_at, "seller": opportunity.seller, "actor": user.email}
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs = offered if accepts_any else {k: v for k, v in offered.items() if k in params}
    return fn(db, **kwargs)


def commission_hook(db: Session, opportunity: FranchiseOpportunity, payment: FranchisePayment, user: User) -> str:
    """What happens to seller commission when a fee clears. Commits its own work; a failure inside the ledger is
    rolled back to the cleared payment (already committed) and recorded as pending. Returns a sentence for the flash."""
    if opportunity.seller_id is None:
        return "No seller is attributed, so no commission arises."
    if payment.amount_pence == 0:
        return "A waived fee carries no commission."
    label = f"{payment.basis}{' month ' + str(payment.trail_month) if payment.trail_month else ''} fee of {packs.pounds(payment.amount_pence)}"
    fn = _ledger_function()
    if fn is None:
        _note(db, opportunity, f"Commission pending: the {label} cleared on {payment.cleared_at:%d %b %Y} for seller "
                               f"#{opportunity.seller_id}, but the ledger service (app.services.commission) is not installed. "
                               "Record the entry when it is.", user)
        audit(db, user, "franchise.commission_pending", f"opportunity:{opportunity.id}",
              after={"payment_id": payment.id, "seller_id": opportunity.seller_id, "basis": payment.basis,
                     "basis_pence": payment.amount_pence, "reason": "ledger not installed"})
        db.commit()
        return LEDGER_MISSING
    try:
        result = _call_ledger(fn, db, opportunity, payment, user)
        audit(db, user, "franchise.commission", f"opportunity:{opportunity.id}",
              after={"payment_id": payment.id, "seller_id": opportunity.seller_id, "basis": payment.basis,
                     "basis_pence": payment.amount_pence, "result": result if isinstance(result, (dict, list, str, int)) else str(result)})
        db.commit()
    except Exception as e:   # noqa: BLE001  (a ledger failure must be visible and must not undo the cleared payment)
        db.rollback()
        _note(db, opportunity, f"Commission pending: the ledger refused the {label}: {str(e)[:300]}", user)
        audit(db, user, "franchise.commission_failed", f"opportunity:{opportunity.id}",
              after={"payment_id": payment.id, "seller_id": opportunity.seller_id, "error": str(e)[:500]})
        db.commit()
        return f"The commission ledger refused the entry and it is noted as pending: {str(e)[:200]}"
    if isinstance(result, dict) and not result.get("entries"):
        reason = str(result.get("reason") or "the ledger wrote nothing")
        return f"No seller commission accrued on the {label}: {reason}."
    if isinstance(result, dict) and result.get("amount_pence"):
        return f"Seller commission of {packs.pounds(int(result['amount_pence']))} accrued through the ledger."
    return "Seller commission accrued through the ledger."


def _at_midnight(d: date | None) -> datetime | None:
    return datetime.combine(d, time.min) if d else None


@router.post("/{opportunity_id}/payments", response_class=HTMLResponse)
def payment_add(opportunity_id: int, request: Request, basis: str = Form("upfront"), trail_month: str = Form(""), amount: str = Form(""),
                paid_at: str = Form(""), cleared_at: str = Form(""), method: str = Form("manual"), reference: str = Form(""),
                notes: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    vals = {"basis": basis.strip(), "trail_month": trail_month.strip(), "amount": amount.strip(), "paid_at": paid_at.strip(),
            "cleared_at": cleared_at.strip(), "method": method.strip(), "reference": reference.strip()[:120], "notes": notes.strip()[:20_000]}
    errors: list[str] = []
    pence = 0
    try:
        pence = pounds_to_pence(vals["amount"])
    except ValueError as e:
        errors.append(str(e))
    if vals["basis"] not in FRANCHISE_PAYMENT_BASES:
        errors.append("Say whether this is the upfront fee or a month of the monthly fee.")
    month: int | None = None
    if vals["basis"] == "monthly":
        month = parse_int(vals["trail_month"], 0)
        if month < 1:
            errors.append("A monthly fee needs the month number it pays for (1 for the first month).")
    dates: dict[str, date | None] = {}
    for key, label in (("paid_at", "paid date"), ("cleared_at", "cleared date")):
        try:
            dates[key] = parse_date(vals[key], label)
        except ValueError as e:
            dates[key] = None
            errors.append(str(e))
    if dates.get("paid_at") is None and not any("paid date" in e for e in errors):
        errors.append("The paid date is required.")
    today = utcnow().date()
    for key in ("paid_at", "cleared_at"):
        if dates.get(key) and dates[key] > today:     # type: ignore[operator]
            errors.append(f"The {key.replace('_at', '')} date cannot be in the future.")
    if dates.get("paid_at") and dates.get("cleared_at") and dates["cleared_at"] < dates["paid_at"]:     # type: ignore[operator]
        errors.append("The cleared date cannot be before the paid date.")
    if vals["method"] not in PAYMENT_METHODS:
        errors.append("Choose how the payment arrived.")
    if errors:
        return _page(request, db, user, opportunity, payment_vals=vals, error=" ".join(errors), status=400)
    payment = FranchisePayment(opportunity_id=opportunity.id, basis=vals["basis"], trail_month=month if vals["basis"] == "monthly" else None,
                               amount_pence=pence, paid_at=_at_midnight(dates["paid_at"]), cleared_at=_at_midnight(dates["cleared_at"]),
                               method=vals["method"], reference=vals["reference"], notes=vals["notes"])
    db.add(payment)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        which = "upfront fee" if vals["basis"] == "upfront" else f"month {month}"
        return _page(request, db, user, opportunity, payment_vals=vals, status=400,
                     error=f"The {which} is already on record for this opportunity. Refund it first if it was paid again.")
    audit(db, user, "franchise.payment.create", f"franchise_payment:{payment.id}",
          after={"opportunity_id": opportunity.id, "basis": payment.basis, "trail_month": payment.trail_month, "amount_pence": pence,
                 "paid_at": payment.paid_at, "cleared_at": payment.cleared_at, "method": payment.method, "reference": payment.reference})
    db.commit()
    tail = ""
    if payment.cleared_at:
        tail = " " + commission_hook(db, opportunity, payment, user)
    else:
        tail = " Mark it cleared once the money has arrived."
    return go(f"/franchise/{opportunity.id}", ok=f"Payment of {packs.pounds(pence)} recorded.{tail}")


@router.post("/{opportunity_id}/payments/{payment_id}/clear", response_class=HTMLResponse)
def payment_clear(opportunity_id: int, payment_id: int, cleared_at: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    opportunity = _load(db, opportunity_id)
    payment = db.get(FranchisePayment, payment_id)
    if payment is None or payment.opportunity_id != opportunity.id:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} was not found on this opportunity.")
    back = f"/franchise/{opportunity.id}"
    if payment.cleared_at is not None:
        return go(back, ok="That payment was already cleared.")
    if payment.refunded_at is not None:
        return go(back, err=f"That payment was refunded on {payment.refunded_at:%d %b %Y} and cannot be marked cleared.")
    try:
        when = parse_date(cleared_at, "cleared date")
    except ValueError as e:
        return go(back, err=str(e))
    today = utcnow().date()
    if when is not None and when > today:
        return go(back, err="The cleared date cannot be in the future.")
    if when is not None and when < payment.paid_at.date():
        return go(back, err=f"The cleared date cannot be before the paid date ({payment.paid_at:%d %b %Y}).")
    payment.cleared_at = _at_midnight(when) if when else utcnow()
    db.add(payment)
    audit(db, user, "franchise.payment.clear", f"franchise_payment:{payment.id}", before={"cleared_at": None}, after={"cleared_at": payment.cleared_at})
    db.commit()
    return go(back, ok=f"Payment of {packs.pounds(payment.amount_pence)} marked cleared. {commission_hook(db, opportunity, payment, user)}")


@router.post("/{opportunity_id}/payments/{payment_id}/refund", response_class=HTMLResponse)
def payment_refund(opportunity_id: int, payment_id: int, refunded_at: str = Form(""), reason: str = Form(""), db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    """Mark a fee refunded. The refund is audited and committed first; then, when the fee had cleared and the
    ledger is present, its on_refund claws back every entry earned on it when the refund falls inside the 90-day
    window (docs/commission-rules.md section 4) and leaves a note for the founder when it does not. A refunded
    upfront fee also means the territory is no longer paid for: the stage is left for the admin to move, with
    the audit trail saying why."""
    opportunity = _load(db, opportunity_id)
    payment = db.get(FranchisePayment, payment_id)
    if payment is None or payment.opportunity_id != opportunity.id:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} was not found on this opportunity.")
    back = f"/franchise/{opportunity.id}"
    if payment.refunded_at is not None:
        return go(back, ok=f"That payment was already refunded on {payment.refunded_at:%d %b %Y}.")
    reason = reason.strip()[:500]
    if not reason:
        return go(back, err="Say why the fee is being refunded; the reason goes on the timeline and any clawback.")
    try:
        when = parse_date(refunded_at, "refunded date")
    except ValueError as e:
        return go(back, err=str(e))
    today = utcnow().date()
    if when is not None and when > today:
        return go(back, err="The refunded date cannot be in the future.")
    if when is not None and when < payment.paid_at.date():
        return go(back, err=f"The refunded date cannot be before the paid date ({payment.paid_at:%d %b %Y}).")
    payment.refunded_at = _at_midnight(when) if when else utcnow()
    db.add(payment)
    label = f"{payment.basis}{' month ' + str(payment.trail_month) if payment.trail_month else ''} fee of {packs.pounds(payment.amount_pence)}"
    _note(db, opportunity, f"Refunded the {label} on {payment.refunded_at:%d %b %Y}: {reason}", user)
    audit(db, user, "franchise.payment.refund", f"franchise_payment:{payment.id}", before={"refunded_at": None},
          after={"refunded_at": payment.refunded_at, "reason": reason, "cleared_at": payment.cleared_at, "amount_pence": payment.amount_pence})
    db.commit()
    tail = ""
    if payment.cleared_at is None:
        tail = " It had not cleared, so no commission was earned on it."
    elif commission_ledger is None or not callable(getattr(commission_ledger, "on_refund", None)):
        _note(db, opportunity, f"Clawback pending: the {label} was refunded but the ledger service (app.services.commission) is not installed.", user)
        audit(db, user, "franchise.commission_clawback_pending", f"opportunity:{opportunity.id}", after={"payment_id": payment.id, "reason": "ledger not installed"})
        db.commit()
        tail = " The ledger is not installed, so any clawback is noted as pending."
    else:
        try:
            result = commission_ledger.on_refund(db, opportunity, payment, actor=user)
            audit(db, user, "franchise.commission_clawback", f"opportunity:{opportunity.id}",
                  after={"payment_id": payment.id, "seller_id": opportunity.seller_id, "result": result})
            db.commit()
            if result.get("clawbacks"):
                tail = f" Commission of {packs.pounds(-int(result['amount_pence']))} clawed back through the ledger."
            else:
                tail = f" No clawback written: {result.get('reason') or 'nothing to claw back'}."
                if "[CONFIRM]" in str(result.get("reason", "")):
                    _note(db, opportunity, f"Clawback left to the founder: {result['reason']}", user)
                    db.commit()
        except Exception as e:   # noqa: BLE001  (a ledger failure must be visible and must not undo the refund)
            db.rollback()
            _note(db, opportunity, f"Clawback pending: the ledger refused it for the {label}: {str(e)[:300]}", user)
            audit(db, user, "franchise.commission_clawback_failed", f"opportunity:{opportunity.id}",
                  after={"payment_id": payment.id, "seller_id": opportunity.seller_id, "error": str(e)[:500]})
            db.commit()
            tail = f" The ledger refused the clawback and it is noted as pending: {str(e)[:200]}"
    return go(back, ok=f"The {label} is marked refunded.{tail}")


# ---- the Hello New Home hand-off -------------------------------------------------------------------------------
@router.post("/{opportunity_id}/hnh", response_class=HTMLResponse)
def hnh_handoff(opportunity_id: int, request: Request, dry_run: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    """Create the Local Partner in Hello New Home through the HNH connector: the person, tier area, the
    opportunity's districts as the new area's postcodes and fee_waived from the quote. Idempotent on
    connector_calls (franchise:<id>:hnh_partner): a succeeded call is never sent twice; a dry run records the
    plan on the same row and the next live press sends it. The temporary portal password HNH returns is shown
    once on the result page and stored nowhere (ConnectorResult.once)."""
    opportunity = _load(db, opportunity_id)
    person = opportunity.person
    back = f"/franchise/{opportunity.id}"
    existing = _hnh_call(db, opportunity)
    if existing is not None and existing.status == "succeeded":
        return go(back, err=f"Already created in Hello New Home (connector call #{existing.id}). Nothing was sent again.")
    if person.hnh_partner_id:
        return go(back, err=HNH_ALREADY.format(pid=person.hnh_partner_id))
    if not opportunity.districts:
        return go(back, err="The opportunity has no districts, so there is no area to create.")
    fee_waived = is_fee_waived(opportunity)
    if opportunity.stage not in MONEY_STAGES:
        return go(back, err="Move the opportunity to Paid or Onboarding first; a partner is created in Hello New Home only once the "
                            "territory is sold.")
    if not fee_waived and _cleared_upfront(db, opportunity) is None:
        return go(back, err="The upfront fee has not cleared (or was refunded), so the partner cannot be created yet. Mark the "
                            "payment cleared first.")
    dry = bool(dry_run) or product_dry_run("hnh", switches=settings.product_switches(db))
    stand_in = SimpleNamespace(id=opportunity.id, kind="partner_seat", value="area", remote_ref=None)
    payload = {"email": normalise_email(person.person_key), "tier": "area", "postcodes": [str(c) for c in opportunity.districts],
               "franchise": {"fee_waived": fee_waived}, "opportunity_id": opportunity.id}
    call = existing or ConnectorCall(product="hnh", action="franchise_partner", idempotency_key=_hnh_key(opportunity), payload=payload)
    call.payload = payload
    call.attempts = int(call.attempts or 0) + 1
    call.last_attempt_at = utcnow()
    error: ConnectorError | None = None
    result = None
    connector = HNHConnector.from_config(dry_run=dry)
    try:
        result = connector.grant(stand_in, person, reason=f"grouphq:franchise:{opportunity.id}",
                                 extra={"postcodes": payload["postcodes"], "fee_waived": fee_waived})
    except ConnectorError as e:
        error = e
    finally:
        connector.close()
    links: dict = {}
    if error is not None:
        call.status = "failed"
        call.response = error.as_dict()
        _note(db, opportunity, f"Hello New Home refused to create the partner: {error.summary()}", user)
        audit(db, user, "franchise.hnh_partner_failed", f"opportunity:{opportunity.id}", after={"error": error.as_dict()})
    else:
        assert result is not None
        call.status = "pending" if result.dry_run else "succeeded"
        call.response = result.as_dict()
        if not result.dry_run:
            links = dict(result.links or {})
            if links.get("hnh_partner_id") and not person.hnh_partner_id:
                person.hnh_partner_id = int(links["hnh_partner_id"])
            if links.get("hnh_user_id") and not person.hnh_user_id:
                person.hnh_user_id = int(links["hnh_user_id"])
            db.add(person)
            _note(db, opportunity, f"Local Partner created in Hello New Home (partner id {result.remote_ref or '?'}) with "
                                   f"{', '.join(payload['postcodes'])}; fee waived: {'yes' if fee_waived else 'no'}.", user)
        else:
            _note(db, opportunity, f"Dry run of the Hello New Home hand-off recorded (nothing sent): {', '.join(payload['postcodes'])}, "
                                   f"fee waived {'yes' if fee_waived else 'no'}.", user)
        audit(db, user, "franchise.hnh_partner", f"opportunity:{opportunity.id}",
              after={"dry_run": result.dry_run, "remote_ref": result.remote_ref, "links": links, "postcodes": payload["postcodes"],
                     "fee_waived": fee_waived})
    db.add(call)
    db.flush()
    db.commit()
    ctx = {**_common(user, db), "o": opportunity, "person": person, "call": call, "result": result, "error": error,
           "once": result.once if result is not None else "", "links": links, "fee_waived": fee_waived, "postcodes": payload["postcodes"]}
    return render(request, "franchise/hnh_result.html", ctx)
