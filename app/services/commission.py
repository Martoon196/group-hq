"""The seller commission ledger: what someone earns for selling a Hello New Home territory, when it exists,
when it is clawed back, and how it is gathered into statements (plan, Phase 4; docs/commission-rules.md).

The rules are the commission_rules rows (version 1 is the plan's proposal, seeded with confirmed = false and
"confirm=false" at the start of its note). Every figure this module produces is a proposal until the
founder confirms the rules: rules_confirmed() is what the screens badge [CONFIRM] on, and nothing here may
be quoted to a seller before it is true. Partners never see any of this; Group HQ is admin-only.

    accrue(db, opportunity, payment, actor=...)      an entry, only once the payment has cleared
    clawback(db, entry, reason, actor=...)           a negative entry pointing at the original
    on_refund(db, opportunity, payment, ...)         the clawbacks a refund inside 90 days causes
    generate_statements(db, "2026-10", actor=...)    the previous month's unpaid entries, per seller
    mark_paid(db, statement, paid_at, actor=...)     stamps the statement and every entry on it
    seller_totals(db, seller_id)                     earned, clawed back, cleared, paid and owed
    ensure_mentee_seller(db, person, actor=...)      a seller of kind mentee for a mentee who has none (the recompute)

Money is an integer number of pence throughout; a percentage of pence is rounded half up to the penny.
Nothing here commits: the caller (a route, the job or a test) commits, so an audit row is never written
for a change that did not happen. accrue() is what app.routers.franchise's commission hook calls when a
fee clears (it finds this module by name and passes db, opportunity, payment and actor).
"""
from __future__ import annotations

import calendar
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..models import AuditLog, CommissionEntry, CommissionRule, FranchiseOpportunity, Person, Seller, Statement, User, utcnow
from .audit import audit
from .territory_rules import ensure_commission_rules

CLAWBACK_WINDOW_DAYS = 90          # docs/commission-rules.md section 4: a refund inside this many days of clearing claws back
STATEMENT_DAY = 10                 # heads of terms section 7: the statement is issued by the 10th for the previous month
PAYMENT_DAY = 20                   # and paid by the 20th
BASES = ("upfront", "monthly")
KIND_LABELS = {"mentee": "Mentee", "franchisee": "Franchisee", "outsider": "Outsider"}
JOB_ACTOR = "job:statements"
# Unambiguous letters and digits for a seller code: no 0/O, 1/I/L, so a code read out on the phone survives.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_SUFFIX_LENGTH = 4
_PERIOD_RE = re.compile(r"^(\d{4})-(\d{1,2})$")
_CONFIRM_FALSE_RE = re.compile(r"^\s*confirm\s*=\s*false\.?\s*", re.I)


class CommissionError(Exception):
    """A refusal the caller can show as it is: the message names what was wrong and what to do."""


def article(word: str) -> str:
    """'an outsider', 'a mentee': the indefinite article for a seller kind."""
    return ("an " if (word or "")[:1].lower() in "aeiou" and word else "a ") + str(word)


# ---- money --------------------------------------------------------------------------------------------------
def pence_share(basis_pence: int, pct) -> int:
    """The commission on basis_pence at the rule's fraction (Decimal('0.2000') is 20%), half pennies up."""
    return int((Decimal(int(basis_pence)) * Decimal(str(pct))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def pounds(pence: int | None) -> str:
    """1300000 pence as £13,000.00, -260000 as -£2,600.00; None as an empty string."""
    if pence is None:
        return ""
    value = Decimal(int(pence)).scaleb(-2).quantize(Decimal("0.01"))
    return ("-" if value < 0 else "") + f"£{abs(value):,.2f}"


def percent(pct) -> str:
    """Decimal('0.2000') as 20%, Decimal('0.125') as 12.5%."""
    value = (Decimal(str(pct)) * 100).normalize()
    text = f"{value:f}"
    return (text.rstrip("0").rstrip(".") if "." in text else text) + "%"


# ---- the rules ---------------------------------------------------------------------------------------------
def rule_for(db: Session, seller_kind: str, as_of: date) -> CommissionRule | None:
    """The rule in force for a seller kind on a day: the one with the latest active_from on or before it, the
    highest version on a tie. When no rule of that kind exists at all the version 1 proposal is seeded, as the
    migration would have (a database that predates the seed), still confirmed = false."""
    if db.query(CommissionRule.id).filter(CommissionRule.seller_kind == seller_kind).first() is None:
        ensure_commission_rules(db)
        db.flush()
    return (db.query(CommissionRule)
            .filter(CommissionRule.seller_kind == seller_kind, CommissionRule.active_from <= as_of)
            .order_by(CommissionRule.active_from.desc(), CommissionRule.version.desc()).first())


def ensure_rules(db: Session) -> int:
    """Seed the version 1 proposal when the table is empty for any kind and commit it, so a screen's Confirm
    buttons point at rows that exist. The migration seeds the same rows; this covers a database that predates
    it. Returns how many rules were added."""
    added = ensure_commission_rules(db)
    if added:
        db.commit()
    return added


def current_rules(db: Session, today: date | None = None) -> dict[str, CommissionRule]:
    """{seller_kind: the rule in force today}, for the screens."""
    today = today or utcnow().date()
    return {kind: rule for kind in KIND_LABELS if (rule := rule_for(db, kind, today)) is not None}


def rule_is_confirmed(rule: CommissionRule | None) -> bool:
    """A rule counts as confirmed only when its flag is set and its note no longer starts with "confirm=false"."""
    return rule is not None and bool(rule.confirmed) and not _CONFIRM_FALSE_RE.match(rule.note or "")


def rules_confirmed(db: Session, today: date | None = None) -> bool:
    """True only when every seller kind has a rule in force today and each one is confirmed. The screens badge
    every figure [CONFIRM] while this is false."""
    rules = current_rules(db, today)
    return len(rules) == len(KIND_LABELS) and all(rule_is_confirmed(r) for r in rules.values())


def confirm_rule(db: Session, rule: CommissionRule, actor: User | str, *, today: date | None = None) -> CommissionRule:
    """The founder's sign-off: sets confirmed and replaces the "confirm=false" opener of the note with who
    confirmed it and when. Audited. Caller commits."""
    who = actor.email if isinstance(actor, User) else str(actor or "system")
    today = today or utcnow().date()
    before = {"confirmed": rule.confirmed, "note": rule.note}
    rule.confirmed = True
    rest = _CONFIRM_FALSE_RE.sub("", rule.note or "", count=1).strip()
    rule.note = f"Confirmed by {who} on {today:%d %B %Y}." + (f" {rest}" if rest else "")
    db.add(rule)
    audit(db, actor, "commission.rule_confirmed", f"commission_rule:{rule.id}", before=before,
          after={"confirmed": True, "note": rule.note, "version": rule.version, "seller_kind": rule.seller_kind})
    return rule


def agreement_date(db: Session, opportunity: FranchiseOpportunity) -> date | None:
    """The day the opportunity reached Agreement: the audit trail's first franchise.stage row that moved it
    there (or, failing that, to Paid or Onboarding), or today's stage_changed_at when it sits at Agreement
    now and nothing was recorded. None when it has never got that far."""
    rows = (db.query(AuditLog).filter(AuditLog.action == "franchise.stage", AuditLog.entity == f"opportunity:{opportunity.id}")
            .order_by(AuditLog.id).all())
    for wanted in ("agreement", "paid", "onboarding"):
        for row in rows:
            if isinstance(row.after, dict) and row.after.get("stage") == wanted:
                return row.created_at.date()
    if opportunity.stage in ("agreement", "paid", "onboarding") and opportunity.stage_changed_at:
        return opportunity.stage_changed_at.date()
    return None


def rule_for_opportunity(db: Session, opportunity: FranchiseOpportunity, seller: Seller, payment) -> tuple[CommissionRule | None, date]:
    """(rule, the day it was chosen for). An opportunity that already has a live entry for this seller keeps
    that entry's rule, so a later version never splits one sale across two rates (docs/commission-rules.md
    section 2). Otherwise the rule in force for the seller's kind on the agreement date, or on the day the
    payment cleared when no agreement was recorded."""
    earlier = (db.query(CommissionEntry).filter(CommissionEntry.opportunity_id == opportunity.id, CommissionEntry.seller_id == seller.id,
                                                CommissionEntry.clawback_of.is_(None)).order_by(CommissionEntry.id).first())
    if earlier is not None:
        return db.get(CommissionRule, earlier.rule_id), earlier.earned_at.date()
    as_of = agreement_date(db, opportunity) or (payment.cleared_at.date() if payment.cleared_at else utcnow().date())
    return rule_for(db, seller.kind, as_of), as_of


# ---- entries ------------------------------------------------------------------------------------------------
def live_entry(db: Session, opportunity_id: int, seller_id: int, basis: str, trail_month: int | None) -> CommissionEntry | None:
    q = db.query(CommissionEntry).filter(CommissionEntry.opportunity_id == opportunity_id, CommissionEntry.seller_id == seller_id,
                                         CommissionEntry.basis == basis, CommissionEntry.clawback_of.is_(None))
    q = q.filter(CommissionEntry.trail_month.is_(None)) if trail_month is None else q.filter(CommissionEntry.trail_month == trail_month)
    return q.first()


def entry_label(basis: str, trail_month: int | None) -> str:
    return "upfront fee" if basis == "upfront" else f"monthly fee, month {trail_month}"


def _skip(reason: str, **extra) -> dict:
    return {"entries": 0, "amount_pence": 0, "reason": reason, **extra}


def accrue(db: Session, opportunity: FranchiseOpportunity, payment, *, actor: User | str = "") -> dict:
    """Write the commission entry a cleared fee earns, or say why none is due. payment is a
    franchise_payments row (basis, trail_month, amount_pence, cleared_at, refunded_at).

    Nothing is written when: the payment has not cleared (plan, Phase 4: "Entries only exist once
    payments.cleared_at is set"); it was refunded; no seller is attributed; the seller is the buyer; the fee
    is waived; the month is outside the rule's trail; the rule pays nothing on that basis; or the entry
    already exists (the unique index ux_commission_entries_once is the backstop, so running this twice can
    never pay twice). A missing rule is an error, not a skip: the hook records it as pending.

    Returns {"entries": 0 or 1, "amount_pence", "entry_id", "rule", "confirmed", "reason"} for the
    caller's flash and audit row. Caller commits."""
    if getattr(payment, "basis", None) not in BASES:
        raise CommissionError(f"A fee is upfront or monthly, not {getattr(payment, 'basis', None)!r}.")
    trail_month = getattr(payment, "trail_month", None)
    if payment.basis == "monthly" and (trail_month is None or int(trail_month) < 1):
        raise CommissionError("A monthly fee needs the month number it pays for before commission can accrue.")
    trail_month = int(trail_month) if payment.basis == "monthly" else None
    if payment.cleared_at is None:
        return _skip("the payment has not cleared, so nothing is earned yet")
    if getattr(payment, "refunded_at", None) is not None:
        return _skip("the payment was refunded, so nothing is earned")
    if opportunity.seller_id is None:
        return _skip("no seller is attributed to this opportunity")
    seller = db.get(Seller, opportunity.seller_id)
    if seller is None:
        raise CommissionError(f"Seller #{opportunity.seller_id} is not on the register.")
    if seller.person_id == opportunity.person_id:
        return _skip("a seller earns nothing on their own purchase (docs/commission-rules.md section 3)")
    if int(payment.amount_pence) <= 0:
        return _skip("a waived fee carries no commission")
    existing = live_entry(db, opportunity.id, seller.id, payment.basis, trail_month)
    if existing is not None:
        return _skip("already accrued", entry_id=existing.id, existing_amount_pence=existing.amount_pence)
    rule, as_of = rule_for_opportunity(db, opportunity, seller, payment)
    if rule is None:
        raise CommissionError(f"No commission rule for {article(seller.kind)} seller is in force on {as_of:%d %B %Y}.")
    if payment.basis == "upfront":
        pct = rule.upfront_pct
    else:
        if trail_month > int(rule.trail_months):
            return _skip(f"month {trail_month} is outside the {rule.trail_months}-month trail of rule version {rule.version}",
                         rule=f"v{rule.version} {rule.seller_kind}")
        pct = rule.trail_pct
    if Decimal(str(pct)) <= 0:
        return _skip(f"rule version {rule.version} pays {article(seller.kind)} seller nothing on the {entry_label(payment.basis, trail_month)}",
                     rule=f"v{rule.version} {rule.seller_kind}")
    amount = pence_share(int(payment.amount_pence), pct)
    entry = CommissionEntry(opportunity_id=opportunity.id, seller_id=seller.id, rule_id=rule.id, basis=payment.basis,
                            trail_month=trail_month, basis_pence=int(payment.amount_pence), amount_pence=amount,
                            earned_at=payment.cleared_at, cleared_at=payment.cleared_at)
    db.add(entry)
    db.flush()
    audit(db, actor or JOB_ACTOR, "commission.accrue", f"commission_entry:{entry.id}",
          after={"opportunity_id": opportunity.id, "seller_id": seller.id, "seller_code": seller.code, "rule_id": rule.id,
                 "rule": f"v{rule.version} {rule.seller_kind}", "rule_confirmed": rule_is_confirmed(rule), "basis": payment.basis,
                 "trail_month": trail_month, "basis_pence": entry.basis_pence, "pct": str(pct), "amount_pence": amount,
                 "earned_at": entry.earned_at, "payment_id": getattr(payment, "id", None)})
    return {"entries": 1, "entry_id": entry.id, "amount_pence": amount, "rule": f"v{rule.version} {rule.seller_kind}",
            "confirmed": rule_is_confirmed(rule), "reason": ""}


def clawback(db: Session, entry: CommissionEntry, reason: str, *, actor: User | str = "", at: datetime | None = None) -> CommissionEntry:
    """A second entry with a negative amount whose clawback_of points at the original. One per entry: asking
    twice returns the existing clawback. When the original sits on a statement that is not yet paid, the
    clawback joins that statement and nets it; otherwise it waits for the next one (heads of terms section 7:
    "deducted from the next statement(s)"). Caller commits."""
    if entry.clawback_of is not None:
        raise CommissionError("That entry is itself a clawback and cannot be clawed back again.")
    existing = db.query(CommissionEntry).filter(CommissionEntry.clawback_of == entry.id).first()
    if existing is not None:
        return existing
    reason = (reason or "").strip()[:500]
    if not reason:
        raise CommissionError("Say why the commission is being clawed back; the reason goes on the statement.")
    when = at or utcnow()
    row = CommissionEntry(opportunity_id=entry.opportunity_id, seller_id=entry.seller_id, rule_id=entry.rule_id, basis=entry.basis,
                          trail_month=entry.trail_month, basis_pence=entry.basis_pence, amount_pence=-int(entry.amount_pence),
                          earned_at=when, cleared_at=when, clawback_of=entry.id)
    statement = db.get(Statement, entry.statement_id) if entry.statement_id else None
    if statement is not None and statement.paid_at is None:
        row.statement_id = statement.id
    db.add(row)
    db.flush()
    if row.statement_id:
        recompute_total(db, statement)
    audit(db, actor or JOB_ACTOR, "commission.clawback", f"commission_entry:{row.id}",
          after={"clawback_of": entry.id, "opportunity_id": entry.opportunity_id, "seller_id": entry.seller_id, "basis": entry.basis,
                 "trail_month": entry.trail_month, "amount_pence": row.amount_pence, "reason": reason, "statement_id": row.statement_id})
    return row


def on_refund(db: Session, opportunity: FranchiseOpportunity, payment, *, refunded_at: datetime | None = None,
              actor: User | str = "", window_days: int = CLAWBACK_WINDOW_DAYS) -> dict:
    """What a refund does to the ledger: every live entry earned on that fee is clawed back when the refund
    falls within window_days of the fee clearing (docs/commission-rules.md section 4). A refund after the
    window is left for the founder to decide and nothing is written. Returns {"clawbacks", "amount_pence",
    "reason"}. Caller commits."""
    when = refunded_at or getattr(payment, "refunded_at", None) or utcnow()
    if payment.cleared_at is None:
        return {"clawbacks": 0, "amount_pence": 0, "reason": "the fee never cleared, so no commission was earned on it"}
    trail_month = int(payment.trail_month) if payment.basis == "monthly" and payment.trail_month is not None else None
    q = db.query(CommissionEntry).filter(CommissionEntry.opportunity_id == opportunity.id, CommissionEntry.basis == payment.basis,
                                         CommissionEntry.clawback_of.is_(None))
    q = q.filter(CommissionEntry.trail_month.is_(None)) if trail_month is None else q.filter(CommissionEntry.trail_month == trail_month)
    entries = q.order_by(CommissionEntry.id).all()
    if not entries:
        return {"clawbacks": 0, "amount_pence": 0, "reason": "no commission was earned on that fee"}
    days = (when - payment.cleared_at).days
    if days > window_days:
        return {"clawbacks": 0, "amount_pence": 0,
                "reason": f"refunded {days} days after clearing, outside the {window_days}-day window: whether to claw back is the "
                          f"founder's call [CONFIRM]"}
    label = entry_label(payment.basis, trail_month)
    total = 0
    made = 0
    for entry in entries:
        already = db.query(CommissionEntry.id).filter(CommissionEntry.clawback_of == entry.id).first()
        if already is not None:
            continue
        row = clawback(db, entry, f"Refund of the {label} of {pounds(int(payment.amount_pence))} on {when:%d %B %Y}, "
                                  f"{days} day{'s' if days != 1 else ''} after it cleared.", actor=actor, at=when)
        total += row.amount_pence
        made += 1
    return {"clawbacks": made, "amount_pence": total, "reason": "" if made else "already clawed back"}


# ---- statements ---------------------------------------------------------------------------------------------
def parse_period(text: str) -> tuple[date, date]:
    """'2026-10' as (1 October 2026, 31 October 2026)."""
    m = _PERIOD_RE.match((text or "").strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise CommissionError(f"A period is a month written as YYYY-MM, for example 2026-10, not {text!r}.")
    year, month = int(m.group(1)), int(m.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def previous_month(today: date) -> tuple[date, date]:
    """The calendar month before the one today falls in."""
    first = today.replace(day=1)
    end = first - timedelta(days=1)
    return end.replace(day=1), end


def period_key(start: date) -> str:
    return f"{start:%Y-%m}"


def period_label(start: date) -> str:
    return f"{start:%B %Y}"


def statement_due(today: date) -> bool:
    """The job's rule: statements are generated on the 10th (heads of terms section 7)."""
    return today.day == STATEMENT_DAY


def payment_due_on(statement: Statement) -> date:
    """The 20th of the month after the period."""
    following = statement.period_end + timedelta(days=1)
    return following.replace(day=PAYMENT_DAY)


def recompute_total(db: Session, statement: Statement) -> int:
    db.flush()
    total = db.query(func.coalesce(func.sum(CommissionEntry.amount_pence), 0)).filter(CommissionEntry.statement_id == statement.id).scalar()
    statement.total_pence = int(total or 0)
    db.add(statement)
    return statement.total_pence


def eligible_entries(db: Session, seller_id: int, period_end: date, *, as_of: datetime) -> list[CommissionEntry]:
    """The seller's payable entries that no statement holds yet: those cleared by the end of the period, plus
    any clawback cleared since, up to the moment of generation, so a refund recorded in the days before the
    10th comes off this statement rather than waiting a month."""
    end_of_period = datetime.combine(period_end, time.max)
    rows = (db.query(CommissionEntry).filter(CommissionEntry.seller_id == seller_id, CommissionEntry.paid_at.is_(None),
                                             CommissionEntry.statement_id.is_(None), CommissionEntry.cleared_at.isnot(None))
            .order_by(CommissionEntry.id).all())
    return [e for e in rows if e.cleared_at <= end_of_period or (e.clawback_of is not None and e.cleared_at <= as_of)]


@dataclass
class StatementRun:
    period_start: date
    period_end: date
    generated: list[Statement] = field(default_factory=list)     # new this run
    regenerated: list[Statement] = field(default_factory=list)   # existed, unpaid, recomputed
    held: list[tuple[Seller, int, int]] = field(default_factory=list)   # (seller, entries, pence) without a self-billing agreement
    paid_already: list[Statement] = field(default_factory=list)

    @property
    def statements(self) -> list[Statement]:
        return self.generated + self.regenerated

    def line(self) -> str:
        held = f", {len(self.held)} seller{'s' if len(self.held) != 1 else ''} held for want of a self-billing agreement" if self.held else ""
        skipped = f", {len(self.paid_already)} already paid" if self.paid_already else ""
        return (f"{period_label(self.period_start)}: {len(self.generated)} statement{'s' if len(self.generated) != 1 else ''} generated, "
                f"{len(self.regenerated)} regenerated{held}{skipped}; total {pounds(sum(s.total_pence for s in self.statements))}.")


def generate_statements(db: Session, period, *, actor: User | str = JOB_ACTOR, as_of: datetime | None = None) -> StatementRun:
    """One statement per seller for the period (a 'YYYY-MM' string or a (start, end) pair), holding every
    payable entry not already on a statement (docs/commission-rules.md section 4). A seller with no self-billing
    agreement is held: their entries stay where they are and are reported. An earlier unpaid statement whose
    total is zero or below is carried forward: its entries move onto the new statement and the empty shell is
    removed, so a negative month nets against the next. Running it again for the same period is safe: an
    unpaid statement is recomputed and gains only entries that became payable since; a paid one is never
    touched. Caller commits."""
    start, end = parse_period(period) if isinstance(period, str) else (period[0], period[1])
    if end < start:
        raise CommissionError("The period ends before it starts.")
    as_of = as_of or utcnow()
    run = StatementRun(start, end)
    seller_ids = {sid for (sid,) in db.query(CommissionEntry.seller_id).filter(CommissionEntry.paid_at.is_(None)).distinct().all()}
    seller_ids |= {sid for (sid,) in db.query(Statement.seller_id).filter(Statement.period_start == start, Statement.period_end == end).all()}
    for seller in db.query(Seller).filter(Seller.id.in_(seller_ids)).order_by(Seller.id).all() if seller_ids else []:
        existing = db.query(Statement).filter(Statement.seller_id == seller.id, Statement.period_start == start, Statement.period_end == end).first()
        if existing is not None and existing.paid_at is not None:
            run.paid_already.append(existing)
            continue
        entries = eligible_entries(db, seller.id, end, as_of=as_of)
        carried = (db.query(Statement).filter(Statement.seller_id == seller.id, Statement.paid_at.is_(None), Statement.period_end < start,
                                              Statement.total_pence <= 0).order_by(Statement.period_start).all())
        if existing is None and not entries and not carried:
            continue
        if seller.self_billing_agreed_at is None:
            run.held.append((seller, len(entries), sum(e.amount_pence for e in entries)))
            continue
        statement = existing or Statement(seller_id=seller.id, period_start=start, period_end=end, generated_at=as_of)
        db.add(statement)
        db.flush()
        moved: list[int] = []
        for old in carried:
            if old.id == statement.id:
                continue
            for e in db.query(CommissionEntry).filter(CommissionEntry.statement_id == old.id).all():
                e.statement_id = statement.id
                db.add(e)
            moved.append(old.id)
            audit(db, actor, "statement.carry_forward", f"statement:{old.id}",
                  before={"total_pence": old.total_pence, "period_start": old.period_start, "period_end": old.period_end},
                  after={"into_statement_id": statement.id})
            db.flush()
            db.delete(old)
        for e in entries:
            e.statement_id = statement.id
            db.add(e)
        statement.generated_at = as_of
        total = recompute_total(db, statement)
        audit(db, actor, "statement.generate" if existing is None else "statement.regenerate", f"statement:{statement.id}",
              after={"seller_id": seller.id, "seller_code": seller.code, "period_start": start, "period_end": end, "entries_added": len(entries),
                     "carried_forward_from": moved, "total_pence": total})
        (run.generated if existing is None else run.regenerated).append(statement)
    return run


def unconfirmed_entries(db: Session, statement: Statement) -> list[CommissionEntry]:
    """The lines on a statement earned under a rule the founder has not confirmed."""
    rows = (db.query(CommissionEntry).options(selectinload(CommissionEntry.rule))
            .filter(CommissionEntry.statement_id == statement.id).order_by(CommissionEntry.id).all())
    return [e for e in rows if not rule_is_confirmed(e.rule)]


def mark_paid(db: Session, statement: Statement, paid_at: datetime | None = None, *, actor: User | str = "") -> Statement:
    """Stamps paid_at on the statement and on every entry it carries. A statement whose total is zero or below
    cannot be paid: the balance carries forward to the next one. A statement with a line earned under an
    unconfirmed rule cannot be paid either: the printed statement says nothing on it is payable until the rules
    are confirmed, and money is the one thing a proposal must not drive. Caller commits."""
    if statement.paid_at is not None:
        raise CommissionError(f"Statement #{statement.id} was already paid on {statement.paid_at:%d %B %Y}.")
    if statement.total_pence <= 0:
        raise CommissionError(f"Nothing to pay: the balance of {pounds(statement.total_pence)} carries forward to the next statement.")
    unconfirmed = unconfirmed_entries(db, statement)
    if unconfirmed:
        n = len(unconfirmed)
        raise CommissionError(f"{n} line{'s' if n != 1 else ''} on this statement {'were' if n != 1 else 'was'} earned under a commission "
                              "rule the founder has not confirmed; confirm the rules on /sellers first.")
    when = paid_at or utcnow()
    statement.paid_at = when
    db.add(statement)
    count = 0
    for e in db.query(CommissionEntry).filter(CommissionEntry.statement_id == statement.id).all():
        e.paid_at = when
        db.add(e)
        count += 1
    audit(db, actor or JOB_ACTOR, "statement.paid", f"statement:{statement.id}", before={"paid_at": None},
          after={"paid_at": when, "total_pence": statement.total_pence, "entries": count, "seller_id": statement.seller_id})
    return statement


# ---- totals -------------------------------------------------------------------------------------------------
def seller_totals(db: Session, seller_id: int) -> dict:
    """earned (live entries), clawed_back (as a positive figure), cleared (net payable to date), paid (net paid),
    owed (cleared less paid) and on_statement (attached, awaiting payment), all in pence."""
    rows = db.query(CommissionEntry).filter(CommissionEntry.seller_id == seller_id).all()
    earned = sum(e.amount_pence for e in rows if e.clawback_of is None)
    clawed = -sum(e.amount_pence for e in rows if e.clawback_of is not None)
    cleared = sum(e.amount_pence for e in rows if e.cleared_at is not None)
    paid = sum(e.amount_pence for e in rows if e.paid_at is not None)
    on_statement = sum(e.amount_pence for e in rows if e.statement_id is not None and e.paid_at is None)
    return {"earned": earned, "clawed_back": clawed, "cleared": cleared, "paid": paid, "owed": cleared - paid,
            "on_statement": on_statement, "entries": len(rows)}


def totals_by_seller(db: Session) -> dict[int, dict]:
    """seller_totals for every seller with an entry, in one query, for the list screen."""
    out: dict[int, dict] = {}
    for e in db.query(CommissionEntry).all():
        t = out.setdefault(e.seller_id, {"earned": 0, "clawed_back": 0, "cleared": 0, "paid": 0, "owed": 0, "on_statement": 0, "entries": 0})
        t["entries"] += 1
        if e.clawback_of is None:
            t["earned"] += e.amount_pence
        else:
            t["clawed_back"] -= e.amount_pence
        if e.cleared_at is not None:
            t["cleared"] += e.amount_pence
        if e.paid_at is not None:
            t["paid"] += e.amount_pence
        if e.statement_id is not None and e.paid_at is None:
            t["on_statement"] += e.amount_pence
    for t in out.values():
        t["owed"] = t["cleared"] - t["paid"]
    return out


# ---- seller codes -----------------------------------------------------------------------------------------
def code_prefix(person: Person | None) -> str:
    """Two or three letters a seller will recognise as theirs: the initials of their name, or the first letters
    of the email's local part."""
    letters = ""
    if person is not None and (person.name or "").strip():
        letters = "".join(part[0] for part in re.split(r"[\s\-]+", person.name.strip()) if part and part[0].isalpha())[:3]
    if len(letters) < 2 and person is not None:
        local = (person.person_key or "").split("@")[0]
        letters = (letters + "".join(ch for ch in local if ch.isalpha()))[:2]
    letters = letters.upper() or "HN"
    return letters if len(letters) >= 2 else (letters + "N")[:2]


def generate_code(db: Session, person: Person | None, *, attempts: int = 20) -> str:
    """A short tracked-link code like JA-7K3M: the person's initials, a dash and four characters from an
    alphabet without look-alikes, unique whatever its case."""
    prefix = code_prefix(person)
    for _ in range(attempts):
        suffix = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_SUFFIX_LENGTH))
        code = f"{prefix}-{suffix}"
        if db.query(Seller.id).filter(func.lower(Seller.code) == code.lower()).first() is None:
            return code
    raise CommissionError("Could not find an unused seller code; try again.")


def ensure_mentee_seller(db: Session, person: Person, *, actor: User | str = "") -> Seller | None:
    """Give a mentee a seller record so they hold a franchise referral code (the recompute calls this for every person
    with an active mentee row). Idempotent: a person who already has a seller, of any kind and active or not, is left
    exactly as they are and None is returned. The new seller is kind mentee, active, with a generated code and no
    self-billing agreement, so every statement stays held until that agreement is entered (the existing rule).
    Audited as seller.create. Written in a savepoint: a code or person clash with a concurrent write leaves nothing
    behind and the next recompute tries again. Caller commits."""
    if person.id is None or db.query(Seller.id).filter(Seller.person_id == person.id).first() is not None:
        return None
    try:
        with db.begin_nested():
            seller = Seller(person_id=person.id, kind="mentee", code=generate_code(db, person), bank_reference="",
                            self_billing_agreed_at=None, active=True)
            db.add(seller)
            db.flush()
    except (IntegrityError, CommissionError):
        return None
    audit(db, actor, "seller.create", f"seller:{seller.id}",
          after={"person_id": seller.person_id, "kind": seller.kind, "code": seller.code, "bank_reference": "",
                 "self_billing_agreed_at": None, "active": True, "via": "recompute: active mentee"})
    return seller


def code_problem(code: str) -> str:
    """Why a hand-typed code is refused, or '' when it is acceptable in a tracked link."""
    code = (code or "").strip()
    if not code:
        return "A seller code is required."
    if len(code) > 24:
        return "A seller code is at most 24 characters."
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9\-_]*", code):
        return "A seller code is letters, digits, dashes and underscores only, so it survives in a link."
    return ""
