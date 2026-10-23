"""Sellers and their commission: who may earn for selling a Hello New Home territory, what the ledger says
they have earned, and the monthly statements that pay it (plan, Phase 4; docs/commission-rules.md).

    GET  /sellers                                  the register: code, kind, self-billing, earned, owed
    GET  /sellers/new, POST                        a seller from a person (or a person created inline); the code
                                                   is generated (initials, a dash, four characters) unless typed
    GET  /sellers/{id}                             detail: totals, tracked link, opportunities, entries, statements
    POST /sellers/{id}                             kind, code, bank reference, self-billing agreement, active
    POST /sellers/{id}/entries/{eid}/clawback      a clawback by hand, with its reason
    GET  /sellers/statements                       every statement, and the form that generates a month's
    POST /sellers/statements/generate              period YYYY-MM (the previous month by default)
    GET  /sellers/statements/{id}                  the statement in the app shell
    GET  /sellers/statements/{id}/print            the printable self-billing statement
    POST /sellers/statements/{id}/paid             paid on a date: stamps the statement and its entries
    POST /sellers/rules/{id}/confirm               the founder's sign-off of a commission rule

Money rule: every percentage and every figure derived from one is a proposal until the founder confirms
the commission rules (app.services.commission.rules_confirmed), and the screens badge each one [CONFIRM]
until then. The statement print view carries a DRAFT [CONFIRM] watermark while that is so. Partners never
see any of this; Group HQ is admin-only.

Conventions as in the other register routers: require_admin and the CSRF guard on every route, audit()
then commit on every write, a 303 with ?ok= or ?err= after a POST, a 400 re-render on a bad form.
"""
from __future__ import annotations

from datetime import date, datetime, time

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..auth import check_origin, normalise_email, require_admin
from ..db import get_db
from ..models import SELLER_KINDS, AuditLog, CommissionEntry, CommissionRule, FranchiseOpportunity, Person, Seller, Statement, User, utcnow
from ..services import commission
from ..services.audit import audit, changes
from ..services.commission import CommissionError, KIND_LABELS, percent, pounds
from ..services.earnings import TRACKED_LINK
from ..templating import go, render
from .people import find_person_by_key, like_pattern, page_of, parse_date, person_key_problem, qs

router = APIRouter(prefix="/sellers", dependencies=[Depends(check_origin)])

KIND_HINTS = {
    "mentee": "On the mentorship: upfront 20% and a 10% trail for 12 months under rule version 1.",
    "franchisee": "An existing Local Partner: the same as a mentee under rule version 1.",
    "outsider": "Anyone else: 10% of the upfront fee and no trail under rule version 1.",
}
STAGE_LABELS = {
    "enquiry": "Enquiry", "discovery": "Discovery", "pack_sent": "Pack sent", "follow_up": "Follow-up",
    "agreement": "Agreement", "paid": "Paid", "onboarding": "Onboarding", "lost": "Lost",
}
# The tracked link lives with the earning links (app.services.earnings), which shows it on the mentee and person pages.
NO_SELF_BILLING = ("No self-billing agreement is on record, so this seller's entries are held back from every statement "
                   "until the date it was signed is entered below.")


# ---- shared ------------------------------------------------------------------------------------------------
def _common(user: User, db: Session) -> dict:
    commission.ensure_rules(db)
    rules = commission.current_rules(db)
    return {"user": user, "section": "sellers", "kind_labels": KIND_LABELS, "kinds": list(KIND_LABELS.items()), "rules": rules,
            "confirmed": commission.rules_confirmed(db), "rule_confirmed": commission.rule_is_confirmed, "pounds": pounds,
            "percent": percent, "today": utcnow().date(), "tracked_link": TRACKED_LINK, "stage_labels": STAGE_LABELS}


def _load(db: Session, seller_id: int) -> Seller:
    seller = db.query(Seller).options(selectinload(Seller.person)).filter(Seller.id == seller_id).first()
    if seller is None:
        raise HTTPException(status_code=404, detail=f"Seller {seller_id} was not found.")
    return seller


def _load_statement(db: Session, statement_id: int) -> Statement:
    row = (db.query(Statement).options(selectinload(Statement.seller).selectinload(Seller.person))
           .filter(Statement.id == statement_id).first())
    if row is None:
        raise HTTPException(status_code=404, detail=f"Statement {statement_id} was not found.")
    return row


def seller_snapshot(s: Seller) -> dict:
    return {"person_id": s.person_id, "kind": s.kind, "code": s.code, "bank_reference": s.bank_reference,
            "self_billing_agreed_at": s.self_billing_agreed_at, "active": s.active}


def _at_midnight(d: date | None) -> datetime | None:
    return datetime.combine(d, time.min) if d else None


def _entries_for(db: Session, **filters) -> list[CommissionEntry]:
    q = (db.query(CommissionEntry)
         .options(selectinload(CommissionEntry.opportunity).selectinload(FranchiseOpportunity.person), selectinload(CommissionEntry.rule),
                  selectinload(CommissionEntry.seller).selectinload(Seller.person)))
    for column, value in filters.items():
        q = q.filter(getattr(CommissionEntry, column) == value)
    return q.order_by(CommissionEntry.earned_at, CommissionEntry.id).all()


def clawback_reasons(db: Session, entries: list[CommissionEntry]) -> dict[int, str]:
    """{clawback entry id: the reason it was written}, from the audit row that wrote it."""
    ids = [e.id for e in entries if e.clawback_of is not None]
    if not ids:
        return {}
    rows = db.query(AuditLog).filter(AuditLog.action == "commission.clawback", AuditLog.entity.in_([f"commission_entry:{i}" for i in ids])).all()
    out: dict[int, str] = {}
    for row in rows:
        try:
            out[int(row.entity.split(":")[1])] = str((row.after or {}).get("reason", ""))
        except (IndexError, ValueError):
            continue
    return out


def _clawed(entries: list[CommissionEntry]) -> set[int]:
    return {e.clawback_of for e in entries if e.clawback_of is not None}


# ---- the register -------------------------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
def listing(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    qp = request.query_params
    filters = {"kind": qp.get("kind", "").strip(), "active": qp.get("active", "").strip(), "q": qp.get("q", "").strip()[:120]}
    q = db.query(Seller).options(selectinload(Seller.person)).join(Person, Person.id == Seller.person_id)
    if filters["kind"] in SELLER_KINDS:
        q = q.filter(Seller.kind == filters["kind"])
    if filters["active"] in ("1", "0"):
        q = q.filter(Seller.active.is_(filters["active"] == "1"))
    if filters["q"]:
        like = like_pattern(filters["q"].lower())
        q = q.filter(or_(func.lower(Person.person_key).like(like, escape="\\"), func.lower(Person.name).like(like, escape="\\"),
                         func.lower(Seller.code).like(like, escape="\\")))
    q = q.order_by(Seller.active.desc(), Seller.code)
    rows, total, page, pages = page_of(q, request)
    totals = commission.totals_by_seller(db)
    attributed = dict(db.query(FranchiseOpportunity.seller_id, func.count(FranchiseOpportunity.id))
                      .filter(FranchiseOpportunity.seller_id.isnot(None)).group_by(FranchiseOpportunity.seller_id).all())
    unpaid = db.query(Statement).filter(Statement.paid_at.is_(None), Statement.total_pence > 0).count()
    ctx = {**_common(user, db), "rows": rows, "total": total, "page": page, "pages": pages, "filters": filters, "totals": totals,
           "attributed": attributed, "unpaid_statements": unpaid, "qs": qs(filters), "qs_prev": qs(filters, page=page - 1),
           "qs_next": qs(filters, page=page + 1), "previous_period": commission.period_key(commission.previous_month(utcnow().date())[0])}
    return render(request, "sellers/list.html", ctx)


# ---- create -------------------------------------------------------------------------------------------------
def _form(request: Request, db: Session, user: User, vals: dict, *, error: str = "", status: int = 200) -> HTMLResponse:
    ctx = {**_common(user, db), "vals": vals, "people": db.query(Person).order_by(Person.person_key).all(), "kind_hints": KIND_HINTS}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "sellers/form.html", ctx, status)


@router.get("/new", response_class=HTMLResponse)
def new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    qp = request.query_params
    vals = {"person_id": qp.get("person_id", "").strip()[:12], "email": "", "name": "", "phone": "", "kind": qp.get("kind", "mentee").strip(),
            "code": "", "bank_reference": "", "self_billing": "", "self_billing_date": utcnow().date().isoformat(), "active": "1"}
    if vals["kind"] not in SELLER_KINDS:
        vals["kind"] = "mentee"
    return _form(request, db, user, vals)


@router.post("/new", response_class=HTMLResponse)
def create(request: Request, person_id: str = Form(""), email: str = Form(""), name: str = Form(""), phone: str = Form(""),
           kind: str = Form(""), code: str = Form(""), bank_reference: str = Form(""), self_billing: str = Form(""),
           self_billing_date: str = Form(""), active: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    vals = {"person_id": person_id.strip()[:12], "email": email.strip()[:254], "name": name.strip()[:120], "phone": phone.strip()[:40],
            "kind": kind.strip(), "code": code.strip()[:24], "bank_reference": bank_reference.strip()[:120], "self_billing": self_billing.strip()[:1],
            "self_billing_date": self_billing_date.strip()[:10], "active": active.strip()[:1]}
    errors: list[str] = []
    notes: list[str] = []

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
                notes.append(f"{key} was already on the register as person #{person.id}, so that record is used.")
            else:
                person = Person(person_key=key, name=vals["name"], phone=vals["phone"])
                db.add(person)
                created_person = True
    else:
        errors.append("Choose a person from the register or enter the seller's email to add them.")
    if person is not None and person.id is not None:
        held = db.query(Seller).filter(Seller.person_id == person.id).first()
        if held is not None:
            errors.append(f"{person.person_key} is already seller {held.code} (#{held.id}); one seller per person.")
    if vals["kind"] not in SELLER_KINDS:
        errors.append("Say what kind of seller they are: a mentee, a franchisee or an outsider.")
    if vals["code"]:
        problem = commission.code_problem(vals["code"])
        if problem:
            errors.append(problem)
        elif db.query(Seller.id).filter(func.lower(Seller.code) == vals["code"].lower()).first() is not None:
            errors.append(f"The code {vals['code']} is already in use; leave it blank to have one generated.")
    agreed: datetime | None = None
    if vals["self_billing"]:
        try:
            when = parse_date(vals["self_billing_date"], "self-billing date")
        except ValueError as e:
            when = None
            errors.append(str(e))
        if when is None and not any("self-billing date" in e for e in errors):
            errors.append("Enter the date the self-billing agreement was signed, or untick it.")
        elif when is not None and when > utcnow().date():
            errors.append("The self-billing agreement cannot be signed in the future.")
        agreed = _at_midnight(when)
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
    seller = Seller(person_id=person.id, kind=vals["kind"], code=vals["code"] or commission.generate_code(db, person),
                    bank_reference=vals["bank_reference"], self_billing_agreed_at=agreed, active=bool(vals["active"]))
    db.add(seller)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return _form(request, db, user, vals, error="That person is already a seller, or the code is taken.", status=400)
    audit(db, user, "seller.create", f"seller:{seller.id}", after=seller_snapshot(seller))
    db.commit()
    flash = (f"Seller {seller.code} created for {person.person_key} ({KIND_LABELS[seller.kind]}). Their tracked link is "
             f"{TRACKED_LINK.format(code=seller.code)}. " + " ".join(notes)).strip()
    return go(f"/sellers/{seller.id}", ok=flash)


# ---- statements (declared before /{seller_id} so the literal path wins) --------------------------------------------
@router.get("/statements", response_class=HTMLResponse)
def statements(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    qp = request.query_params
    filters = {"state": qp.get("state", "").strip(), "seller": qp.get("seller", "").strip()}
    q = db.query(Statement).options(selectinload(Statement.seller).selectinload(Seller.person))
    if filters["state"] == "unpaid":
        q = q.filter(Statement.paid_at.is_(None))
    elif filters["state"] == "paid":
        q = q.filter(Statement.paid_at.isnot(None))
    if filters["seller"].isdigit():
        q = q.filter(Statement.seller_id == int(filters["seller"]))
    q = q.order_by(Statement.period_start.desc(), Statement.seller_id)
    rows, total, page, pages = page_of(q, request)
    today = utcnow().date()
    waiting = (db.query(CommissionEntry.seller_id, func.count(CommissionEntry.id), func.coalesce(func.sum(CommissionEntry.amount_pence), 0))
               .filter(CommissionEntry.statement_id.is_(None), CommissionEntry.paid_at.is_(None), CommissionEntry.cleared_at.isnot(None))
               .group_by(CommissionEntry.seller_id).all())
    waiting_sellers = {s.id: s for s in db.query(Seller).options(selectinload(Seller.person)).filter(Seller.id.in_([w[0] for w in waiting])).all()} if waiting else {}
    ctx = {**_common(user, db), "rows": rows, "total": total, "page": page, "pages": pages, "filters": filters, "qs": qs(filters),
           "qs_prev": qs(filters, page=page - 1), "qs_next": qs(filters, page=page + 1),
           "sellers": db.query(Seller).options(selectinload(Seller.person)).order_by(Seller.code).all(),
           "previous_period": commission.period_key(commission.previous_month(today)[0]),
           "previous_label": commission.period_label(commission.previous_month(today)[0]), "due_today": commission.statement_due(today),
           "statement_day": commission.STATEMENT_DAY, "payment_day": commission.PAYMENT_DAY,
           "waiting": [(waiting_sellers.get(sid), int(count), int(pence)) for sid, count, pence in waiting if sid in waiting_sellers],
           "payment_due_on": commission.payment_due_on,
           "outstanding_pence": sum(s.total_pence for s in db.query(Statement).filter(Statement.paid_at.is_(None), Statement.total_pence > 0).all())}
    return render(request, "statements/list.html", ctx)


@router.post("/statements/generate", response_class=HTMLResponse)
def generate(period: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    back = "/sellers/statements"
    key = period.strip() or commission.period_key(commission.previous_month(utcnow().date())[0])
    today = utcnow().date()
    try:
        start, end = commission.parse_period(key)
        if start > today:
            return go(back, err=f"{commission.period_label(start)} has not started; a statement is for a month that has ended.")
        run = commission.generate_statements(db, (start, end), actor=user)
        db.commit()
    except (CommissionError, IntegrityError) as e:
        db.rollback()
        if isinstance(e, IntegrityError):
            return go(back, err="That period is being generated by another run; refresh and try again.")
        return go(back, err=str(e))
    if not run.statements and not run.held and not run.paid_already:
        return go(back, ok=f"{commission.period_label(start)}: nothing to put on a statement.")
    held = " ".join(f"{s.code} held ({n} entr{'ies' if n != 1 else 'y'}, {pounds(p)}): no self-billing agreement on record." for s, n, p in run.held)
    early = ""
    if end >= today:
        early = (f" {commission.period_label(start)} has not ended: a fee clearing before {end:%d %b} is not on this statement yet, so "
                 f"generate it again on the {commission.STATEMENT_DAY}th before paying it.")
    return go(back, ok=(run.line() + " " + held).strip() + early)


@router.get("/statements/{statement_id}", response_class=HTMLResponse)
def statement_detail(statement_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    statement = _load_statement(db, statement_id)
    entries = _entries_for(db, statement_id=statement.id)
    ctx = {**_common(user, db), "statement": statement, "seller": statement.seller, "entries": entries, "reasons": clawback_reasons(db, entries),
           "period_label": commission.period_label(statement.period_start), "payment_due": commission.payment_due_on(statement),
           "paid_vals": {"paid_at": utcnow().date().isoformat()}, "unconfirmed_lines": len(commission.unconfirmed_entries(db, statement))}
    return render(request, "statements/detail.html", ctx)


@router.get("/statements/{statement_id}/print", response_class=HTMLResponse)
def statement_print(statement_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    statement = _load_statement(db, statement_id)
    entries = _entries_for(db, statement_id=statement.id)
    confirmed = commission.rules_confirmed(db)
    ctx = {"request": request, "user": user, "statement": statement, "seller": statement.seller, "entries": entries,
           "reasons": clawback_reasons(db, entries), "period_label": commission.period_label(statement.period_start),
           "payment_due": commission.payment_due_on(statement), "confirmed": confirmed, "watermark": "" if confirmed else "DRAFT [CONFIRM]",
           "pounds": pounds, "percent": percent, "today": utcnow().date(), "company": "Hello New Home 2 Limited"}
    return render(request, "statements/print.html", ctx)


@router.post("/statements/{statement_id}/paid", response_class=HTMLResponse)
def statement_paid(statement_id: int, paid_at: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    statement = _load_statement(db, statement_id)
    back = f"/sellers/statements/{statement.id}"
    try:
        when = parse_date(paid_at, "paid date")
        if when is not None and when > utcnow().date():
            return go(back, err="A statement cannot be paid in the future; enter the date the transfer was made.")
        if when is not None and when < statement.period_end:
            return go(back, err=f"The paid date is before the period ended ({statement.period_end:%d %b %Y}); check it.")
        commission.mark_paid(db, statement, _at_midnight(when) or utcnow(), actor=user)
    except (ValueError, CommissionError) as e:
        db.rollback()
        return go(back, err=str(e))
    db.commit()
    return go(back, ok=f"Statement #{statement.id} marked paid: {pounds(statement.total_pence)} to {statement.seller.code} on "
                       f"{statement.paid_at:%d %b %Y}. Every entry on it is stamped.")


# ---- rules -------------------------------------------------------------------------------------------------
@router.post("/rules/{rule_id}/confirm", response_class=HTMLResponse)
def rule_confirm(rule_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    rule = db.get(CommissionRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"Commission rule {rule_id} was not found.")
    if commission.rule_is_confirmed(rule):
        return go("/sellers", ok=f"Rule version {rule.version} for {KIND_LABELS.get(rule.seller_kind, rule.seller_kind)} sellers was already confirmed.")
    commission.confirm_rule(db, rule, user)
    db.commit()
    left = sum(1 for r in commission.current_rules(db).values() if not commission.rule_is_confirmed(r))
    tail = " Every rule is now confirmed; the [CONFIRM] badges are gone." if left == 0 else f" {left} rule{'s' if left != 1 else ''} still to confirm."
    return go("/sellers", ok=f"Rule version {rule.version} for {KIND_LABELS.get(rule.seller_kind, rule.seller_kind)} sellers confirmed by {user.email}.{tail}")


# ---- detail ------------------------------------------------------------------------------------------------
def _page(request: Request, db: Session, user: User, seller: Seller, *, vals: dict | None = None, error: str = "", status: int = 200) -> HTMLResponse:
    entries = _entries_for(db, seller_id=seller.id)
    opportunities = (db.query(FranchiseOpportunity).options(selectinload(FranchiseOpportunity.person))
                     .filter(FranchiseOpportunity.seller_id == seller.id).order_by(FranchiseOpportunity.id.desc()).all())
    statements = db.query(Statement).filter(Statement.seller_id == seller.id).order_by(Statement.period_start.desc()).all()
    rule = commission.rule_for(db, seller.kind, utcnow().date())
    ctx = {**_common(user, db), "seller": seller, "person": seller.person, "totals": commission.seller_totals(db, seller.id), "entries": entries,
           "clawed": _clawed(entries), "reasons": clawback_reasons(db, entries), "opportunities": opportunities, "statements": statements,
           "rule": rule, "no_self_billing": NO_SELF_BILLING, "kind_hints": KIND_HINTS, "payment_due_on": commission.payment_due_on,
           "vals": vals or {"kind": seller.kind, "code": seller.code, "bank_reference": seller.bank_reference,
                            "self_billing": "1" if seller.self_billing_agreed_at else "",
                            "self_billing_date": (seller.self_billing_agreed_at or utcnow()).date().isoformat(), "active": "1" if seller.active else ""},
           "link": TRACKED_LINK.format(code=seller.code)}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "sellers/detail.html", ctx, status)


@router.get("/{seller_id}", response_class=HTMLResponse)
def detail(seller_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _page(request, db, user, _load(db, seller_id))


@router.post("/{seller_id}", response_class=HTMLResponse)
def update(seller_id: int, request: Request, kind: str = Form(""), code: str = Form(""), bank_reference: str = Form(""), self_billing: str = Form(""),
           self_billing_date: str = Form(""), active: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    seller = _load(db, seller_id)
    vals = {"kind": kind.strip(), "code": code.strip()[:24], "bank_reference": bank_reference.strip()[:120], "self_billing": self_billing.strip()[:1],
            "self_billing_date": self_billing_date.strip()[:10], "active": active.strip()[:1]}
    errors: list[str] = []
    if vals["kind"] not in SELLER_KINDS:
        errors.append("Choose a kind.")
    problem = commission.code_problem(vals["code"])
    if problem:
        errors.append(problem)
    elif vals["code"].lower() != seller.code.lower() and db.query(Seller.id).filter(func.lower(Seller.code) == vals["code"].lower()).first() is not None:
        errors.append(f"The code {vals['code']} is already in use.")
    agreed: datetime | None = None
    if vals["self_billing"]:
        try:
            when = parse_date(vals["self_billing_date"], "self-billing date")
        except ValueError as e:
            when = None
            errors.append(str(e))
        if when is None and not any("self-billing date" in e for e in errors):
            errors.append("Enter the date the self-billing agreement was signed, or untick it.")
        elif when is not None and when > utcnow().date():
            errors.append("The self-billing agreement cannot be signed in the future.")
        agreed = _at_midnight(when)
    if errors:
        return _page(request, db, user, seller, vals=vals, error=" ".join(errors), status=400)
    before = seller_snapshot(seller)
    seller.kind = vals["kind"]
    seller.code = vals["code"]
    seller.bank_reference = vals["bank_reference"]
    seller.self_billing_agreed_at = agreed
    seller.active = bool(vals["active"])
    b, a = changes(before, seller_snapshot(seller))
    back = f"/sellers/{seller.id}"
    if not a:
        return go(back, ok="No changes to save.")
    db.add(seller)
    audit(db, user, "seller.update", f"seller:{seller.id}", before=b, after=a)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return _page(request, db, user, seller, vals=vals, error="That code is already in use.", status=400)
    hints: list[str] = []
    if "code" in a:
        hints.append(f"The tracked link is now {TRACKED_LINK.format(code=seller.code)}; anything printed with the old code stops attributing.")
    if "kind" in a:
        hints.append("Entries already earned keep the rule they were earned under; the new kind applies to the next sale.")
    if "self_billing_agreed_at" in a and seller.self_billing_agreed_at is None:
        hints.append("Without a self-billing agreement their entries are held back from statements.")
    return go(back, ok=("Seller saved. " + " ".join(hints)).strip())


@router.post("/{seller_id}/entries/{entry_id}/clawback", response_class=HTMLResponse)
def entry_clawback(seller_id: int, entry_id: int, reason: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    seller = _load(db, seller_id)
    entry = db.get(CommissionEntry, entry_id)
    if entry is None or entry.seller_id != seller.id:
        raise HTTPException(status_code=404, detail=f"Entry {entry_id} was not found on this seller.")
    back = f"/sellers/{seller.id}"
    existing = db.query(CommissionEntry).filter(CommissionEntry.clawback_of == entry.id).first()
    if existing is not None:
        return go(back, ok=f"Entry #{entry.id} was already clawed back (entry #{existing.id}).")
    try:
        row = commission.clawback(db, entry, reason, actor=user)
    except CommissionError as e:
        db.rollback()
        return go(back, err=str(e))
    db.commit()
    where = f"on statement #{row.statement_id}" if row.statement_id else "on the next statement"
    return go(back, ok=f"Clawed back {pounds(-row.amount_pence)} from entry #{entry.id} as entry #{row.id}, {where}.")
