"""Mentees: who is on which programme, what they have paid, and the grants that follow from it.

Money: the payment form takes pounds. The text is converted to an exact integer number of pence
(pounds_to_pence) before it is stored as a two-decimal amount (pence_to_amount), so no float ever
touches a figure. Only a cleared payment counts towards access.

Access end dates shown on these screens mirror the plan's rule (Phase 1): paid in full gives
start_date + 12 months once a cleared payment exists; monthly gives the latest cleared payment's
period_end + 7 days of grace. The engine in app.services.entitlements (packet B) is what actually
grants and revokes; this module only displays the rule and hands the buttons to the engine.

The engine is imported leniently. When app.services.entitlements is missing, the Grants tab still
renders with a 503 note and the two buttons answer 503, so this packet can be tested on its own.
"""
from __future__ import annotations

import inspect
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import (MENTEE_STATUSES, PAYMENT_METHODS, PAYMENT_MODES, EntitlementGrant, Mentee, Payment, Person,
                      Programme, User, utcnow)
from ..services import earnings, settings
from ..services.audit import audit, changes
from ..templating import go, render
from .people import like_pattern, page_of, parse_date, parse_int, qs

try:
    from ..services.entitlements import RunSummary, hold_of, pause_person, recompute_person, resume_person, revoke_person
except ImportError:          # packet B's engine is not in this tree yet; the screens still work without it
    RunSummary = None        # type: ignore[assignment,misc]
    hold_of = None           # type: ignore[assignment]
    pause_person = None      # type: ignore[assignment]
    recompute_person = None  # type: ignore[assignment]
    resume_person = None     # type: ignore[assignment]
    revoke_person = None     # type: ignore[assignment]

router = APIRouter(dependencies=[Depends(check_origin)])

ENGINE_MISSING = ("The entitlement engine is not available on this server (error 503), so grants cannot be "
                  "recomputed or revoked from here. Payments and notes can still be recorded; the nightly "
                  "recompute will pick them up once the engine is installed.")
TABS = ("payments", "grants", "notes")
# Lapsed is worked out from the payments by the rule and is never set by hand; ended is a decision.
STATUS_CHOICES = (("active", "Active"), ("ended", "Ended"))
LAPSED_BY_HAND = ("Lapsed is worked out from the payments and cannot be set by hand: record the position in the notes, "
                  "or choose Ended to stop access for good.")
MAX_PERIOD_DAYS = 400              # a period end further away than this is almost certainly a typo
MONTHLY_PERIOD_SLACK_DAYS = 7      # a monthly period may run this much past one calendar month
METHOD_LABELS = {"manual": "Bank transfer or cash (recorded by hand)", "stripe_link": "Stripe payment link", "invoice": "Invoice"}
MODE_LABELS = {"full": "Paid in full (12 months)", "monthly": "Monthly (while paying)"}
MAX_PENCE = 99_999_999_99          # Numeric(10, 2)

_POUNDS_RE = re.compile(r"^(\d+)(?:\.(\d{1,2}))?$")
_GROUPED_RE = re.compile(r"^\d{1,3}(?:[, ]\d{3})+(?:\.\d{1,2})?$")


# ---- money -------------------------------------------------------------------------------
def pounds_to_pence(raw: str) -> int:
    """'1,250.50' (or '£1250.5', '1250') becomes 125050. Exact: the digits are read as text and no float is
    involved. A comma or space is accepted only as a thousands separator, so '12,5' is refused rather than
    read as £125. Raises ValueError with a message a person can act on."""
    text = (raw or "").strip().replace("£", "")
    if not text:
        raise ValueError("Enter the amount in pounds, for example 1250.00.")
    if "," in text or " " in text:
        if not _GROUPED_RE.match(text):
            raise ValueError("Commas and spaces may only group thousands, as in 1,250.00. Put a full stop before the pence.")
        text = text.replace(",", "").replace(" ", "")
    m = _POUNDS_RE.match(text)
    if not m:
        raise ValueError("Amounts are pounds and pence, for example 1250.00, with at most two decimal places and no sign.")
    pence = int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0"))
    if pence == 0:
        raise ValueError("Enter an amount above zero.")
    if pence > MAX_PENCE:
        raise ValueError("That amount is larger than this register can hold.")
    return pence


def pence_to_amount(pence: int) -> Decimal:
    """125050 becomes Decimal('1250.50'), the two-decimal value the payments table stores."""
    return Decimal(int(pence)).scaleb(-2).quantize(Decimal("0.01"))


def money(amount) -> str:
    """Decimal('1250.5') is shown as £1,250.50."""
    if amount is None:
        return ""
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    return f"£{value:,.2f}"


# ---- the access rule, as displayed -------------------------------------------------------------
def _setting_int(key: str, default: int) -> int:
    return parse_int(settings.get(key), default)


def add_months(d: date, months: int) -> date:
    """Calendar months on; a day the target month lacks (29 Feb, 31 Apr) falls back to that month's last day."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    last_day = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day))


def programme_price(mentee: Mentee) -> Decimal | None:
    try:
        price = mentee.programme.price if mentee.programme is not None else None
    except Exception:   # noqa: BLE001  (a half-built mentee in a unit test has no programme to ask)
        return None
    return Decimal(str(price)) if price is not None else None


def cleared_total(mentee: Mentee) -> Decimal:
    return sum((Decimal(str(p.amount)) for p in mentee.payments if p.cleared_at is not None), Decimal("0"))


def shortfall(mentee: Mentee) -> Decimal | None:
    """How much more must clear before a full-mode mentee's term starts, or None when nothing is owed (no
    price on the programme, monthly mode, or the price has been reached)."""
    price = programme_price(mentee)
    if mentee.payment_mode != "full" or price is None:
        return None
    owed = price - cleared_total(mentee)
    return owed if owed > 0 else None


def access_end(mentee: Mentee) -> date | None:
    """The day access runs to under the plan's rule, or None when nothing cleared has been recorded yet (or,
    for a full payer, the cleared payments have not reached the programme's price)."""
    cleared = [p for p in mentee.payments if p.cleared_at is not None]
    if not cleared:
        return None
    if mentee.payment_mode == "full":
        if shortfall(mentee) is not None:
            return None
        return add_months(mentee.start_date, min(_setting_int("full_term_months", 12), 13))
    ends = [p.period_end for p in cleared if p.period_end is not None]
    if not ends:
        return None
    return max(ends) + timedelta(days=_setting_int("grace_days", 7))


def access_summary(mentee: Mentee, today: date | None = None) -> dict:
    """{end, label, tone} for a list row: tone is ok, warn or bad."""
    today = today or utcnow().date()
    end = access_end(mentee)
    if end is None:
        owed = shortfall(mentee)
        if owed is not None and cleared_total(mentee) > 0:
            note = f"{money(owed)} short of the price"
        else:
            note = "No cleared payment" if mentee.payment_mode == "full" else "No cleared payment with a period end"
        return {"end": None, "label": note, "tone": "warn"}
    if end < today:
        return {"end": end, "label": f"Ended {end:%d %b %Y}", "tone": "bad"}
    days = (end - today).days
    return {"end": end, "label": f"To {end:%d %b %Y} ({days} day{'s' if days != 1 else ''})", "tone": "ok" if days > 14 else "warn"}


# ---- queries ------------------------------------------------------------------------------
def _mentee_query(db: Session):
    return db.query(Mentee).options(selectinload(Mentee.person), selectinload(Mentee.programme), selectinload(Mentee.payments))


def _load_mentee(db: Session, mentee_id: int) -> Mentee:
    mentee = _mentee_query(db).filter(Mentee.id == mentee_id).first()
    if mentee is None:
        raise HTTPException(status_code=404, detail=f"Mentee {mentee_id} was not found.")
    return mentee


def _people_choices(db: Session) -> list[Person]:
    return db.query(Person).order_by(Person.person_key).all()


def _programme_choices(db: Session) -> list[Programme]:
    return db.query(Programme).order_by(Programme.active.desc(), Programme.name).all()


def mentee_snapshot(m: Mentee) -> dict:
    return {"person_id": m.person_id, "programme_id": m.programme_id, "payment_mode": m.payment_mode,
            "start_date": m.start_date, "status": m.status, "cohort": m.cohort, "notes": m.notes}


def payment_snapshot(p: Payment) -> dict:
    return {"mentee_id": p.mentee_id, "amount": p.amount, "paid_at": p.paid_at, "cleared_at": p.cleared_at, "method": p.method,
            "period_start": p.period_start, "period_end": p.period_end, "reference": p.reference, "notes": p.notes}


# ---- list ---------------------------------------------------------------------------------
@router.get("/mentees", response_class=HTMLResponse)
def mentees_list(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """The register. ?region=needed is the "Needs a region" queue: active mentees whose Hello New Home partner had no
    area at the last recompute (app.services.earnings.needs_region), each with the button to Hello New Home's admin page
    where the founder assigns it."""
    qp = request.query_params
    filters = {"status": qp.get("status", "").strip(), "programme": qp.get("programme", "").strip(), "q": qp.get("q", "").strip()[:120],
               "region": "needed" if qp.get("region", "").strip() == "needed" else ""}
    q = _mentee_query(db)
    regions: dict[int, dict] = {}
    if filters["region"]:
        q = q.filter(Mentee.status == "active", Mentee.person_id.in_(earnings.needs_region_person_ids(db)))
        regions = {r["person"].id: r for r in earnings.needs_region(db)}
    if filters["status"] in MENTEE_STATUSES:
        q = q.filter(Mentee.status == filters["status"])
    if filters["programme"].isdigit():
        q = q.filter(Mentee.programme_id == int(filters["programme"]))
    if filters["q"]:
        like = like_pattern(filters["q"].lower())
        q = (q.join(Person, Person.id == Mentee.person_id)
             .filter(or_(func.lower(Person.person_key).like(like, escape="\\"), func.lower(Person.name).like(like, escape="\\"),
                         func.lower(Mentee.cohort).like(like, escape="\\"))))
    q = q.order_by(Mentee.id.desc())
    mentees, total, page, pages = page_of(q, request)
    rows = [(m, access_summary(m)) for m in mentees]
    ctx = {"user": user, "section": "mentees", "rows": rows, "total": total, "page": page, "pages": pages, "filters": filters,
           "statuses": MENTEE_STATUSES, "programmes": _programme_choices(db), "qs": qs(filters),
           "qs_prev": qs(filters, page=page - 1), "qs_next": qs(filters, page=page + 1), "mode_labels": MODE_LABELS,
           "regions": regions}
    return render(request, "mentees.html", ctx)


# ---- create -------------------------------------------------------------------------------
def _mentee_form(request: Request, db: Session, user: User, vals: dict, *, error: str = "", status: int = 200) -> HTMLResponse:
    ctx = {"user": user, "section": "mentees", "vals": vals, "people": _people_choices(db), "programmes": _programme_choices(db),
           "modes": [(k, v) for k, v in MODE_LABELS.items()]}
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "mentee_form.html", ctx, status)


@router.get("/mentees/new", response_class=HTMLResponse)
def mentee_new(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    person_id = request.query_params.get("person_id", "").strip()
    programme = db.query(Programme).filter(Programme.code == "mentorship").first()
    vals = {"person_id": person_id, "programme_id": str(programme.id) if programme else "", "payment_mode": "full",
            "start_date": utcnow().date().isoformat(), "cohort": "", "notes": ""}
    return _mentee_form(request, db, user, vals)


@router.post("/mentees/new", response_class=HTMLResponse)
def mentee_create(request: Request, person_id: str = Form(""), programme_id: str = Form(""), payment_mode: str = Form(""),
                  start_date: str = Form(""), cohort: str = Form(""), notes: str = Form(""),
                  db: Session = Depends(get_db), user: User = Depends(require_admin)):
    vals = {"person_id": person_id.strip(), "programme_id": programme_id.strip(), "payment_mode": payment_mode.strip(),
            "start_date": start_date.strip(), "cohort": cohort.strip()[:60], "notes": notes.strip()[:20_000]}
    errors: list[str] = []
    person = db.get(Person, parse_int(vals["person_id"], -1))
    if person is None:
        errors.append("Choose the person. Add them to the register first if they are not listed.")
    programme = db.get(Programme, parse_int(vals["programme_id"], -1))
    if programme is None:
        errors.append("Choose a programme.")
    elif not programme.active:
        errors.append(f"{programme.name} is not active. Reactivate it under Programmes first.")
    if vals["payment_mode"] not in PAYMENT_MODES:
        errors.append("Choose how they pay: in full or monthly.")
    start: date | None = None
    try:
        start = parse_date(vals["start_date"], "start date")
        if start is None:
            errors.append("A start date is required.")
    except ValueError as e:
        errors.append(str(e))
    if person is not None and programme is not None:
        live = (db.query(Mentee).filter(Mentee.person_id == person.id, Mentee.programme_id == programme.id,
                                        Mentee.status == "active").first())
        if live is not None:
            errors.append(f"{person.person_key} is already active on {programme.name} (mentee #{live.id}). End that row before enrolling them again.")
    if errors:
        return _mentee_form(request, db, user, vals, error=" ".join(errors), status=400)
    assert person is not None and programme is not None and start is not None
    mentee = Mentee(person_id=person.id, programme_id=programme.id, payment_mode=vals["payment_mode"], start_date=start,
                    status="active", cohort=vals["cohort"], notes=vals["notes"])
    db.add(mentee)
    db.flush()
    audit(db, user, "mentee.create", f"mentee:{mentee.id}", after=mentee_snapshot(mentee))
    db.commit()
    return go(f"/mentees/{mentee.id}", ok=f"{person.person_key} enrolled on {programme.name}. Record their first payment below.")


# ---- detail with tabs ------------------------------------------------------------------------
def _grants_for(db: Session, mentee: Mentee) -> list[EntitlementGrant]:
    return (db.query(EntitlementGrant).filter(EntitlementGrant.person_id == mentee.person_id)
            .order_by(EntitlementGrant.product, EntitlementGrant.kind, EntitlementGrant.id).all())


def _payment_defaults(mentee: Mentee) -> dict:
    today = utcnow().date()
    vals = {"amount": "", "paid_at": today.isoformat(), "cleared_at": "", "method": "manual", "period_start": "", "period_end": "",
            "reference": "", "notes": ""}
    if mentee.payment_mode == "monthly":
        last_end = max((p.period_end for p in mentee.payments if p.period_end), default=None)
        start = (last_end + timedelta(days=1)) if last_end else mentee.start_date
        vals["period_start"] = start.isoformat()
        vals["period_end"] = (add_months(start, 1) - timedelta(days=1)).isoformat()
    return vals


def _mentee_page(request: Request, db: Session, user: User, mentee: Mentee, tab: str, *, payment_vals: dict | None = None,
                 detail_vals: dict | None = None, error: str = "", status: int = 200) -> HTMLResponse:
    tab = tab if tab in TABS else "payments"
    grants = _grants_for(db, mentee)
    ctx = {
        "user": user, "section": "mentees", "mentee": mentee, "tab": tab, "tabs": TABS, "money": money,
        "access": access_summary(mentee), "payments": sorted(mentee.payments, key=lambda p: (p.paid_at, p.id), reverse=True),
        "grants": grants, "engine_available": engine_available(), "engine_missing": ENGINE_MISSING,
        "holds": {g.id: hold_of(g, mentee.person) for g in grants} if callable(hold_of) else {},
        "payment_vals": payment_vals or _payment_defaults(mentee), "methods": [(k, v) for k, v in METHOD_LABELS.items()],
        "detail_vals": detail_vals or {"status": mentee.status, "cohort": mentee.cohort, "payment_mode": mentee.payment_mode,
                                       "start_date": mentee.start_date.isoformat(), "notes": mentee.notes},
        "statuses": MENTEE_STATUSES, "status_choices": list(STATUS_CHOICES), "modes": [(k, v) for k, v in MODE_LABELS.items()],
        "mode_labels": MODE_LABELS, "cleared_total": cleared_total(mentee),
        "pending_total": sum((p.amount for p in mentee.payments if not p.cleared_at), Decimal("0")),
        "shortfall": shortfall(mentee), "price": programme_price(mentee), "grace_days": _setting_int("grace_days", 7),
        "full_term_months": min(_setting_int("full_term_months", 12), 13), "holds_available": holds_available(),
    }
    if error:
        ctx.update(flash=error, flash_kind="err")
    return render(request, "mentee.html", ctx, status)


@router.get("/mentees/{mentee_id}", response_class=HTMLResponse)
def mentee_detail(mentee_id: int, request: Request, tab: str = "payments", db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    mentee = _load_mentee(db, mentee_id)
    return _mentee_page(request, db, user, mentee, tab)


# ---- payments tab ---------------------------------------------------------------------------
def _at_midnight(d: date | None) -> datetime | None:
    return datetime.combine(d, time.min) if d else None


def period_problems(mentee: Mentee, start: date | None, end: date | None, today: date | None = None) -> list[str]:
    """Why a period end cannot be right: further away than MAX_PERIOD_DAYS, or for a monthly payer more than a
    month and a week after its start. A mis-typed year would otherwise buy years of access."""
    problems: list[str] = []
    if end is None:
        return problems
    today = today or utcnow().date()
    if end > today + timedelta(days=MAX_PERIOD_DAYS):
        problems.append(f"A period end more than {MAX_PERIOD_DAYS} days away is almost certainly a typo.")
    if mentee.payment_mode == "monthly":
        base = start or end
        latest = add_months(base, 1) + timedelta(days=MONTHLY_PERIOD_SLACK_DAYS)
        if start is not None and end > latest:
            problems.append("A monthly period may run at most one month and a week from its start.")
    return problems


@router.post("/mentees/{mentee_id}/payments", response_class=HTMLResponse)
def payment_add(mentee_id: int, request: Request, amount: str = Form(""), paid_at: str = Form(""), cleared_at: str = Form(""),
                method: str = Form("manual"), period_start: str = Form(""), period_end: str = Form(""), reference: str = Form(""),
                notes: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    mentee = _load_mentee(db, mentee_id)
    vals = {"amount": amount.strip(), "paid_at": paid_at.strip(), "cleared_at": cleared_at.strip(), "method": method.strip(),
            "period_start": period_start.strip(), "period_end": period_end.strip(), "reference": reference.strip()[:120],
            "notes": notes.strip()[:20_000]}
    errors: list[str] = []
    pence = 0
    try:
        pence = pounds_to_pence(vals["amount"])
    except ValueError as e:
        errors.append(str(e))
    dates: dict[str, date | None] = {}
    for key, label in (("paid_at", "paid date"), ("cleared_at", "cleared date"), ("period_start", "period start"), ("period_end", "period end")):
        try:
            dates[key] = parse_date(vals[key], label)
        except ValueError as e:
            dates[key] = None
            errors.append(str(e))
    if dates.get("paid_at") is None and not any("paid date" in e for e in errors):
        errors.append("The paid date is required.")
    if vals["method"] not in PAYMENT_METHODS:
        errors.append("Choose how the payment arrived.")
    if dates.get("period_start") and dates.get("period_end") and dates["period_end"] < dates["period_start"]:
        errors.append("The period end is before the period start.")
    errors.extend(period_problems(mentee, dates.get("period_start"), dates.get("period_end")))
    if mentee.payment_mode == "monthly" and dates.get("period_end") is None and not errors:
        errors.append("A monthly payment needs a period end: that date plus the grace period is when access stops.")
    if errors:
        return _mentee_page(request, db, user, mentee, "payments", payment_vals=vals, error=" ".join(errors), status=400)
    payment = Payment(mentee_id=mentee.id, amount=pence_to_amount(pence), paid_at=_at_midnight(dates["paid_at"]),
                      cleared_at=_at_midnight(dates["cleared_at"]), method=vals["method"], period_start=dates["period_start"],
                      period_end=dates["period_end"], reference=vals["reference"], notes=vals["notes"])
    db.add(payment)
    db.flush()
    audit(db, user, "payment.create", f"payment:{payment.id}", after={**payment_snapshot(payment), "pence": pence})
    db.commit()
    note = " It is cleared, so it counts towards access." if payment.cleared_at else " Mark it cleared once the money has arrived."
    return go(f"/mentees/{mentee.id}?tab=payments", ok=f"Payment of {money(payment.amount)} recorded.{note}")


@router.post("/mentees/{mentee_id}/payments/{payment_id}/clear", response_class=HTMLResponse)
def payment_clear(mentee_id: int, payment_id: int, cleared_at: str = Form(""), db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    mentee = _load_mentee(db, mentee_id)
    payment = db.get(Payment, payment_id)
    if payment is None or payment.mentee_id != mentee.id:
        raise HTTPException(status_code=404, detail=f"Payment {payment_id} was not found on this mentee.")
    back = f"/mentees/{mentee.id}?tab=payments"
    if payment.cleared_at is not None:
        return go(back, ok="That payment was already cleared.")
    try:
        when = parse_date(cleared_at, "cleared date")
    except ValueError as e:
        return go(back, err=str(e))
    problems = period_problems(mentee, payment.period_start, payment.period_end)
    if problems:
        return go(back, err=" ".join(problems) + " Correct the period before clearing it.")
    before = {"cleared_at": None}
    payment.cleared_at = _at_midnight(when) if when else utcnow()
    db.add(payment)
    audit(db, user, "payment.clear", f"payment:{payment.id}", before=before, after={"cleared_at": payment.cleared_at})
    db.commit()
    end = access_end(mentee)
    owed = shortfall(mentee)
    if end:
        note = f" Access now runs to {end:%d %b %Y}."
    elif owed is not None:
        note = f" {money(owed)} more must clear before the term starts."
    else:
        note = ""
    return go(back, ok=f"Payment of {money(payment.amount)} marked cleared.{note}")


# ---- grants tab ------------------------------------------------------------------------------
def engine_available() -> bool:
    return callable(recompute_person) and callable(revoke_person)


# Pause all access counts (entitlements._hold_counts) after which a product may still give access.
PAUSE_NOT_OFF = ("failed", "gave up", "blocked", "not configured")


def holds_available() -> bool:
    return engine_available() and callable(pause_person) and callable(resume_person) and RunSummary is not None


def _call_engine(fn, db: Session, person: Person, user: User, **extra):
    """Call the engine with (db, person) and, when its signature takes one, the acting user's email as actor=.
    Any other keyword the engine's signature does not name is dropped rather than raised on."""
    kwargs = dict(extra)
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    accepts_any = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    if accepts_any or "actor" in params:
        kwargs["actor"] = user.email
    if not accepts_any:
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    return fn(db, person, **kwargs)


def _describe(result) -> str:
    if isinstance(result, dict) and result:
        return " " + ", ".join(f"{k} {v}" for k, v in result.items()) + "."
    if isinstance(result, str) and result:
        return " " + result
    if isinstance(result, (list, tuple)):
        return _describe(_summarise(result)["actions"]) if result else " Nothing to decide."
    return ""


def _summarise(result) -> dict:
    """A list of engine Decisions as counts the flash and the audit row can carry, instead of the whole dump."""
    if not isinstance(result, (list, tuple)):
        return {"result": result}
    actions = Counter(getattr(d, "action", "?") for d in result)
    outcomes = Counter(getattr(d, "outcome", "?") for d in result)
    failed = [getattr(d, "label", str(d)) for d in result if getattr(d, "outcome", "") == "failed"]
    return {"actions": dict(actions), "outcomes": dict(outcomes), "failed": failed or None}


def run_recompute(db: Session, mentee: Mentee, user: User, **extra) -> tuple[object, str]:
    """The Recompute button's work for the mentee's person: the engine, an audit row, a commit. Returns
    (result, "") or, when the engine or a connector raised, (None, the error): the failure is rolled back,
    audited as mentee.recompute_failed and handed back, never raised. The existing-users import calls this
    too (passing its open connectors through `extra`), so both run exactly the same recompute."""
    try:
        result = _call_engine(recompute_person, db, mentee.person, user, **extra)
    except Exception as e:  # noqa: BLE001  (a connector or engine failure must reach the screen, never vanish)
        db.rollback()
        audit(db, user, "mentee.recompute_failed", f"mentee:{mentee.id}", after={"error": str(e)[:500]})
        db.commit()
        return None, str(e) or type(e).__name__
    audit(db, user, "mentee.recompute", f"mentee:{mentee.id}", after={"person_id": mentee.person_id, **_summarise(result)})
    db.commit()
    return result, ""


@router.post("/mentees/{mentee_id}/recompute", response_class=HTMLResponse)
def mentee_recompute(mentee_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    mentee = _load_mentee(db, mentee_id)
    if not engine_available():
        raise HTTPException(status_code=503, detail=ENGINE_MISSING)
    back = f"/mentees/{mentee.id}?tab=grants"
    result, error = run_recompute(db, mentee, user)
    if error:
        return go(back, err=f"Recompute failed: {error[:300]}")
    return go(back, ok=f"Recompute run for {mentee.person.person_key}.{_describe(result)}")


@router.post("/mentees/{mentee_id}/revoke", response_class=HTMLResponse)
def mentee_revoke(mentee_id: int, reason: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    mentee = _load_mentee(db, mentee_id)
    if not engine_available():
        raise HTTPException(status_code=503, detail=ENGINE_MISSING)
    back = f"/mentees/{mentee.id}?tab=grants"
    reason = reason.strip()[:200] or f"Revoked by hand from mentee #{mentee.id}"
    try:
        result = _call_engine(revoke_person, db, mentee.person, user, reason=reason)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        audit(db, user, "mentee.revoke_failed", f"mentee:{mentee.id}", after={"error": str(e)[:500]})
        db.commit()
        return go(back, err=f"Revoke failed: {str(e)[:300]}")
    audit(db, user, "mentee.revoke_all", f"mentee:{mentee.id}", after={"person_id": mentee.person_id, "reason": reason, "result": result})
    db.commit()
    return go(back, ok=f"Revoke requested for every grant held by {mentee.person.person_key}.{_describe(result)}")


@router.post("/mentees/{mentee_id}/pause", response_class=HTMLResponse)
def mentee_pause(mentee_id: int, reason: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Pause all access: every grant the person holds is revoked now and kept off by the recompute, whatever is
    paid, until Resume. The mentee rows are left as they are (Revoke all ends them)."""
    mentee = _load_mentee(db, mentee_id)
    if not holds_available():
        raise HTTPException(status_code=503, detail=ENGINE_MISSING)
    back = f"/mentees/{mentee.id}?tab=grants"
    if mentee.person.access_held_at is not None:
        return go(back, ok=f"All access for {mentee.person.person_key} was already paused.")
    reason = reason.strip()[:200]
    try:
        result = _call_engine(pause_person, db, mentee.person, user, reason=reason)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        audit(db, user, "mentee.pause_failed", f"mentee:{mentee.id}", after={"error": str(e)[:500]})
        db.commit()
        return go(back, err=f"Pause failed: {str(e)[:300]}")
    audit(db, user, "mentee.pause_all", f"mentee:{mentee.id}", after={"person_id": mentee.person_id, "reason": reason or None,
                                                                      "result": result})
    db.commit()
    if isinstance(result, dict) and any(result.get(k) for k in PAUSE_NOT_OFF):
        # a product may still give access: say so as an error, never as done
        return go(back, err=f"All access paused for {mentee.person.person_key}, but not every revoke went through:"
                            f"{_describe(result)} A failed revoke, or one not sent for want of a token, is tried again "
                            "each night; one that gave up needs Retry on its grant page, and a blocked one needs checking "
                            "by hand.")
    return go(back, ok=f"All access paused for {mentee.person.person_key}: the recompute keeps every grant off until you "
                       f"resume it.{_describe(result)}")


@router.post("/mentees/{mentee_id}/resume", response_class=HTMLResponse)
def mentee_resume(mentee_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    """Resume all access and recompute at once. Renders the run page, as a recompute from the Grants screen does,
    because a re-grant to Hello New Home may carry a temporary password that is shown once."""
    mentee = _load_mentee(db, mentee_id)
    if not holds_available():
        raise HTTPException(status_code=503, detail=ENGINE_MISSING)
    back = f"/mentees/{mentee.id}?tab=grants"
    if mentee.person.access_held_at is None:
        return go(back, err=f"Access for {mentee.person.person_key} is not paused.")
    try:
        decisions = _call_engine(resume_person, db, mentee.person, user)
    except Exception as e:  # noqa: BLE001
        db.rollback()
        audit(db, user, "mentee.resume_failed", f"mentee:{mentee.id}", after={"error": str(e)[:500]})
        db.commit()
        return go(back, err=f"Resume failed: {str(e)[:300]}")
    audit(db, user, "mentee.resume_all", f"mentee:{mentee.id}", after={"person_id": mentee.person_id, **_summarise(decisions)})
    db.commit()
    summary = RunSummary(today=utcnow().date(), dry_run=bool(config.CONNECTORS_DRY_RUN), people=1, decisions=list(decisions))
    ctx = {"user": user, "section": "mentees", "summary": summary, "decisions": summary.decisions,
           "scope": f"resume for {mentee.person.person_key}", "back": back}
    return render(request, "grants/run.html", ctx)


# ---- notes tab ---------------------------------------------------------------------------------
@router.post("/mentees/{mentee_id}/details", response_class=HTMLResponse)
def mentee_update(mentee_id: int, request: Request, status: str = Form(""), cohort: str = Form(""), payment_mode: str = Form(""),
                  start_date: str = Form(""), notes: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    mentee = _load_mentee(db, mentee_id)
    vals = {"status": status.strip(), "cohort": cohort.strip()[:60], "payment_mode": payment_mode.strip(),
            "start_date": start_date.strip(), "notes": notes.strip()[:20_000]}
    errors: list[str] = []
    if vals["status"] not in MENTEE_STATUSES:
        errors.append("Choose a status.")
    elif vals["status"] == "lapsed" and mentee.status != "lapsed":
        errors.append(LAPSED_BY_HAND)
    if vals["payment_mode"] not in PAYMENT_MODES:
        errors.append("Choose how they pay.")
    start: date | None = None
    try:
        start = parse_date(vals["start_date"], "start date")
        if start is None:
            errors.append("A start date is required.")
    except ValueError as e:
        errors.append(str(e))
    if errors:
        return _mentee_page(request, db, user, mentee, "notes", detail_vals=vals, error=" ".join(errors), status=400)
    assert start is not None
    before = mentee_snapshot(mentee)
    mentee.status = vals["status"]
    mentee.cohort = vals["cohort"]
    mentee.payment_mode = vals["payment_mode"]
    mentee.start_date = start
    mentee.notes = vals["notes"]
    b, a = changes(before, mentee_snapshot(mentee))
    back = f"/mentees/{mentee.id}?tab=notes"
    if not a:
        return go(back, ok="No changes to save.")
    db.add(mentee)
    audit(db, user, "mentee.update", f"mentee:{mentee.id}", before=b, after=a)
    db.commit()
    hint = " Run Recompute now on the Grants tab so the products follow." if {"status", "payment_mode", "start_date"} & set(a) else ""
    return go(back, ok=f"Mentee saved.{hint}")
