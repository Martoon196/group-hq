"""Existing users to mentees: people who already have a login on Key Properties or Ordris, enrolled in one batch.

The import page (app.routers.mentee_import) lists Key Properties sourcers live and takes pasted lines for anyone
else ('email' or 'name, email', one per line). For each person in a batch, in order:

  1. every product that is live (not in dry run) is asked whether it knows the email: Ordris's lookup, Key
     Properties by email, Hello New Home partners by email, Property Predator's account lookup. A product in
     dry run is not asked and says so. A failed lookup is reported for that person and the batch carries on;
     a product that fails for TRIP_AFTER people running is not asked again in that batch, so one product
     being down cannot hold a batch for a timeout per person;
  2. the person is found by person_key or added; an existing person's row is locked until their commit, so
     a second batch for the same person (a double submit) waits and then sees the first one's enrolment;
     the name comes from the row only when the person is new or has no name;
  3. only EMPTY product ids are filled from what the products answered, so an id Martin set by hand is never
     overwritten (a different value is reported and kept; a Property Predator account already linked to
     someone else is left alone and reported);
  4. the person is enrolled on the chosen programme unless they already have a mentee row on it, whatever
     its status, in which case that enrolment is left exactly as it was, payment included, and the row
     says so;
  5. the payment is recorded when one was entered;
  6. with "Grant access now" ticked and a cleared payment recorded, the mentee page's Recompute
     (app.routers.mentees.run_recompute) runs for the person, so grants go out to live products and are
     planned for products in dry run. Once a product the person's programmes grant has tripped (step 1),
     the recompute is not run for them in this batch and the nightly recompute grants instead.

Every write is audited and committed per person, so a failure for one person leaves everyone else's work in
place. The lookups are reads: nothing a product answers is stored beyond the ids.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from types import SimpleNamespace

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..models import PAYMENT_MODES, Mentee, Payment, Person, Programme, User, utcnow
from ..routers import mentees as mentees_router
from ..routers.people import (LINK_COLUMNS, LINK_FIELDS, find_person_by_key, links_snapshot, parse_date, parse_int,
                              person_key_problem, person_snapshot)
from . import settings
from .audit import audit, changes
from .connectors import BaseConnector, ConnectorError, open_connectors, product_dry_run
from .connectors.kp import KPConnector
from .keys import normalise_email

MAX_BATCH = 25                      # people in one import: small enough that the results page (the only
                                    # place an HNH temporary password is shown) arrives before a proxy gives up
KP_PAGE = 100                       # Key Properties sourcers shown per page
TRIP_AFTER = 3                      # failures in a row before a product is not asked again in the batch
LOOKUP_ORDER = ("ordris", "kp", "hnh", "pp")
MAX_PASTE_CHARS = 100_000
NAME_MAX = 120
VIA = "import"
ENROL_NOTE = "Enrolled by the existing-users import."
PAYMENT_NOTE = "Recorded by the existing-users import."
LINK_LABELS = {col: f"{settings.product_name(product)} {label.lower()}" for col, product, label, _ in LINK_FIELDS}


# ---- the rows a batch is made of -------------------------------------------------------------------
@dataclass
class Entry:
    email: str
    name: str = ""
    origin: str = ""                    # "Key Properties" or "pasted line 3"
    notes: list[str] = field(default_factory=list)


def _snippet(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def parse_line(raw: str) -> tuple[Entry | None, str]:
    """One pasted line, 'email' or 'name, email', as (Entry, "") or (None, the reason it was refused). The
    email is the text after the LAST comma, so a name may carry a comma of its own ("Bloggs, Jo, jo@x.com")."""
    text = (raw or "").strip()
    if "," in text:
        name, email = text.rsplit(",", 1)
        name = " ".join(name.strip().strip('"').split())
    else:
        name, email = "", text
    email = email.strip()
    key = normalise_email(email)
    if not key:
        return None, "there is no email after the comma; use 'email' or 'name, email'"
    if " " in key and "@" in key and "," not in text:
        return None, "put a comma between the name and the email, as in Jo Bloggs, jo@example.com"
    if person_key_problem(key):
        if "@" in name and "@" not in key:
            return None, "the email comes last: write the name first, as in Jo Bloggs, jo@example.com"
        return None, "that email address does not look right (the register keys on plain ASCII addresses)"
    if "@" in name:
        return None, "only one person per line: the part before the comma should be their name"
    if len(name) > NAME_MAX:
        return None, f"the name is longer than {NAME_MAX} characters"
    return Entry(email=key, name=name), ""


def parse_lines(text: str) -> tuple[list[Entry], list[str]]:
    """Every non-blank pasted line as an Entry, and a reason for each line that could not be read."""
    entries: list[Entry] = []
    problems: list[str] = []
    for number, raw in enumerate((text or "")[:MAX_PASTE_CHARS].splitlines(), start=1):
        if not raw.strip():
            continue
        entry, why = parse_line(raw)
        if entry is None:
            problems.append(f"Line {number} ({_snippet(raw)}): {why}.")
            continue
        entry.origin = f"pasted line {number}"
        entries.append(entry)
    return entries, problems


def picked_rows(pick: list[str], emails: list[str], names: list[str]) -> tuple[list[Entry], list[str]]:
    """The ticked Key Properties rows. The page posts every row's email and name in order (kp_email, kp_name)
    and each tick box's value is that row's position, so the two lists stay aligned whatever is ticked."""
    entries: list[Entry] = []
    problems: list[str] = []
    seen: set[int] = set()
    for raw in pick:
        index = parse_int(raw, -1)
        if index in seen:
            continue
        seen.add(index)
        if not 0 <= index < len(emails):
            problems.append("A ticked Key Properties row could not be matched to its email. Reload the page and tick it again.")
            continue
        key = normalise_email(emails[index])
        name = " ".join((names[index] if index < len(names) else "").split())[:NAME_MAX]
        if person_key_problem(key):
            problems.append(f"Key Properties row {_snippet(emails[index] or '(blank)')}: that email address cannot be a register key.")
            continue
        entries.append(Entry(email=key, name=name, origin="Key Properties"))
    return entries, problems


def merge(*groups: list[Entry]) -> list[Entry]:
    """One entry per email, in the order first seen; a later spelling of the same person only fills a missing
    name and is noted."""
    merged: dict[str, Entry] = {}
    for group in groups:
        for entry in group:
            first = merged.get(entry.email)
            if first is None:
                merged[entry.email] = entry
                continue
            if not first.name and entry.name:
                first.name = entry.name
            first.notes.append(f"also listed as {entry.origin}; imported once")
    return list(merged.values())


# ---- the shared fields ------------------------------------------------------------------------------
@dataclass
class Shared:
    programme: Programme
    payment_mode: str
    start: date
    pence: int | None = None
    paid_on: date | None = None
    cleared: bool = False
    period_end: date | None = None
    grant_now: bool = True


def check_shared(db: Session, vals: dict) -> tuple[Shared | None, list[str]]:
    """The programme, payment mode, start date and the optional payment, or the reasons they cannot be used."""
    errors: list[str] = []
    programme = db.get(Programme, parse_int(vals.get("programme_id"), -1))
    if programme is None:
        errors.append("Choose a programme.")
    elif not programme.active:
        errors.append(f"{programme.name} is not active. Reactivate it under Programmes first.")
    mode = vals.get("payment_mode", "")
    if mode not in PAYMENT_MODES:
        errors.append("Choose how they pay: in full or monthly.")
    start: date | None = None
    try:
        start = parse_date(vals.get("start_date", ""), "start date")
        if start is None:
            errors.append("A start date is required.")
    except ValueError as e:
        errors.append(str(e))
    pence: int | None = None
    paid_on = period_end = None
    if (vals.get("amount") or "").strip():
        try:
            pence = mentees_router.pounds_to_pence(vals["amount"])
        except ValueError as e:
            errors.append(str(e))
        try:
            paid_on = parse_date(vals.get("paid_on", ""), "paid date")
            if paid_on is None:
                errors.append("The paid date is required when an amount is entered.")
        except ValueError as e:
            errors.append(str(e))
        if mode == "monthly":
            try:
                period_end = parse_date(vals.get("period_end", ""), "period end")
            except ValueError as e:
                errors.append(str(e))
            else:
                if period_end is None:
                    errors.append("A monthly payment needs a period end: that date plus the grace period is when access stops.")
                elif start is not None and period_end < start:
                    errors.append("The period end is before the start date.")
                else:
                    errors.extend(mentees_router.period_problems(SimpleNamespace(payment_mode=mode), start, period_end))
    if errors:
        return None, errors
    assert programme is not None and start is not None
    return Shared(programme=programme, payment_mode=mode, start=start, pence=pence, paid_on=paid_on,
                  cleared=bool(pence) and vals.get("cleared") == "1", period_end=period_end,
                  grant_now=vals.get("grant_now") == "1"), []


# ---- asking the products ----------------------------------------------------------------------------
@dataclass
class Lookup:
    product: str
    status: str                         # found | not_found | dry_run | not_configured | failed | skipped
    links: dict = field(default_factory=dict)
    detail: str = ""

    @property
    def name(self) -> str:
        return settings.product_name(self.product)


class LookupRunner:
    """Asks each live product about one email at a time, and stops asking a product that has failed for
    TRIP_AFTER people running."""

    def __init__(self, connectors: dict[str, BaseConnector]):
        self.connectors = connectors
        self.streak: Counter = Counter()

    def run(self, email: str) -> dict[str, Lookup]:
        return {product: self._one(product, email) for product in LOOKUP_ORDER}

    def _one(self, product: str, email: str) -> Lookup:
        connector = self.connectors.get(product)
        if connector is None:
            return Lookup(product, "skipped", detail="no connector")
        if connector.dry_run:
            return Lookup(product, "dry_run", detail="in dry run, so not asked")
        if not connector.configured:
            return Lookup(product, "not_configured", detail="no base URL or token, so not asked")
        if self.streak[product] >= TRIP_AFTER:
            return Lookup(product, "skipped", detail=f"not asked: it failed for the previous {TRIP_AFTER} people in this batch")
        try:
            result = connector.lookup(email)
        except ConnectorError as error:
            if error.status == 404:
                self.streak[product] = 0
                return Lookup(product, "not_found")
            self.streak[product] += 1
            return Lookup(product, "failed", detail=error.summary()[:300])
        except Exception as error:  # noqa: BLE001  (one product's surprise must not stop the batch)
            self.streak[product] += 1
            return Lookup(product, "failed", detail=f"{type(error).__name__}: {error}"[:300])
        self.streak[product] = 0
        links = {k: v for k, v in (result.links or {}).items() if k in LINK_COLUMNS and v not in (None, "")}
        return Lookup(product, "found" if links else "not_found", links=links)


def fill_links(db: Session, person: Person, lookups: dict[str, Lookup]) -> tuple[dict, list[str]]:
    """Fill the person's EMPTY product ids from the lookups. Returns (what was filled, notes on what was not)."""
    filled: dict = {}
    notes: list[str] = []
    for product in LOOKUP_ORDER:
        lookup = lookups.get(product)
        if lookup is None:
            continue
        for column, value in lookup.links.items():
            current = getattr(person, column)
            if current not in (None, ""):
                if str(current) != str(value):
                    notes.append(f"kept the recorded {LINK_LABELS[column]} {current} ({lookup.name} answered {value})")
                continue
            if column != "ordris_org_id":       # people in one Ordris organisation share its id; every other id is one person's
                col = getattr(Person, column)
                holder = db.query(Person).filter(col == value, Person.id != person.id).first()
                if holder is not None:
                    notes.append(f"{LINK_LABELS[column]} {value} is already linked to {holder.person_key}, so it was not linked here")
                    continue
            setattr(person, column, value)
            filled[column] = value
    return filled, notes


# ---- one person ---------------------------------------------------------------------------------------
@dataclass
class Outcome:
    entry: Entry
    person_id: int | None = None
    person: str = ""                    # created | existing | failed
    person_notes: list[str] = field(default_factory=list)
    lookups: dict[str, Lookup] = field(default_factory=dict)
    filled: dict = field(default_factory=dict)
    mentee_id: int | None = None
    mentee: str = ""                    # created | skipped
    mentee_note: str = ""
    payment: str = ""
    payment_recorded: bool = False
    access: str = ""
    grants: str = ""                    # run | not_run | failed
    grants_note: str = ""
    decisions: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def found_on(self) -> list[str]:
        return [self.lookups[p].name for p in LOOKUP_ORDER if p in self.lookups and self.lookups[p].status == "found"]

    @property
    def lookup_problems(self) -> list[str]:
        return [f"{lk.name} lookup {lk.status.replace('_', ' ')}: {lk.detail}" for lk in self.lookups.values()
                if lk.status in ("failed", "skipped")]

    @property
    def grant_counts(self) -> Counter:
        counts: Counter = Counter()
        for d in self.decisions:
            outcome = "failed" if d["outcome"] in ("failed", "blocked") else d["outcome"]
            counts[outcome] += 1
        return counts


def decision_rows(result) -> list[dict]:
    """The engine's Decisions as plain rows for the results page, the HNH one-time password included (`once`,
    shown once on that page and never stored)."""
    if not isinstance(result, (list, tuple)):
        return []
    rows = []
    for d in result:
        product = str(getattr(d, "product", "") or "")
        kind = str(getattr(d, "kind", "") or "").replace("_", " ")
        rows.append({"product": product, "product_name": settings.product_name(product) if product else "",
                     "right": f"{kind} {getattr(d, 'value', '') or ''}".strip() or str(getattr(d, "label", "")),
                     "action": str(getattr(d, "action", "") or ""), "outcome": str(getattr(d, "outcome", "") or ""),
                     "detail": str(getattr(d, "detail", "") or ""), "once": str(getattr(d, "once", "") or ""),
                     "dry_run": bool(getattr(d, "dry_run", False))})
    return rows


def _midnight(day: date | None) -> datetime | None:
    return datetime.combine(day, time.min) if day else None


def _locked(db: Session, person_id: int) -> Person:
    """The person's row, locked FOR UPDATE until this person's commit and re-read, so a second batch for the same
    person (a double submit) waits and then sees the first one's enrolment, name and links."""
    return db.query(Person).filter(Person.id == person_id).with_for_update().populate_existing().one()


def _get_or_create_person(db: Session, user: User, entry: Entry, out: Outcome) -> Person:
    person = find_person_by_key(db, entry.email)
    if person is None:
        person = Person(person_key=entry.email, name=entry.name)
        db.add(person)
        try:
            db.flush()
        except IntegrityError:          # added by someone else a moment ago: use theirs
            db.rollback()
            person = find_person_by_key(db, entry.email)
            if person is None:
                raise
        else:
            audit(db, user, "person.create", f"person:{person.id}", after={**person_snapshot(person), "via": VIA})
            out.person = "created"
            return person
    person = _locked(db, person.id)
    out.person = "existing"
    if entry.name and not (person.name or "").strip():
        before = {"name": person.name}
        person.name = entry.name
        db.add(person)
        audit(db, user, "person.update", f"person:{person.id}", before=before, after={"name": person.name, "via": VIA})
        out.person_notes.append("name added from the row")
    return person


def _enrol(db: Session, user: User, person: Person, shared: Shared, out: Outcome) -> Mentee | None:
    programme = shared.programme
    existing = (db.query(Mentee).filter(Mentee.person_id == person.id, Mentee.programme_id == programme.id)
                .order_by(Mentee.id.desc()).first())
    if existing is not None:
        out.mentee, out.mentee_id = "skipped", existing.id
        out.mentee_note = (f"already a mentee on {programme.name} (mentee #{existing.id}, {existing.status}), "
                           "so that enrolment was left as it was")
        if shared.pence:
            out.payment = "not recorded: add it on that mentee's Payments tab if it is new"
        return None
    mentee = Mentee(person=person, programme=programme, payment_mode=shared.payment_mode, start_date=shared.start,
                    status="active", cohort="", notes=ENROL_NOTE)
    db.add(mentee)
    db.flush()
    audit(db, user, "mentee.create", f"mentee:{mentee.id}", after={**mentees_router.mentee_snapshot(mentee), "via": VIA})
    out.mentee, out.mentee_id = "created", mentee.id
    if shared.pence:
        payment = Payment(mentee=mentee, amount=mentees_router.pence_to_amount(shared.pence), paid_at=_midnight(shared.paid_on),
                          cleared_at=_midnight(shared.paid_on) if shared.cleared else None, method="manual",
                          period_start=shared.start if shared.payment_mode == "monthly" else None,
                          period_end=shared.period_end if shared.payment_mode == "monthly" else None,
                          reference="", notes=PAYMENT_NOTE)
        db.add(payment)
        db.flush()
        audit(db, user, "payment.create", f"payment:{payment.id}",
              after={**mentees_router.payment_snapshot(payment), "pence": shared.pence, "via": VIA})
        out.payment_recorded = True
        state = "cleared" if payment.cleared_at else "not yet cleared"
        out.payment = f"{mentees_router.money(payment.amount)} paid {shared.paid_on:%d %b %Y}, {state}"
    return mentee


def _tripped(mentee: Mentee, runner: LookupRunner) -> str:
    """The first product the person's programmes grant that has failed for TRIP_AFTER people running in this batch,
    or "". Recomputing them would only wait on it again."""
    products = {str(item.get("product") or "") for m in mentee.person.mentees if m.programme is not None
                for item in (m.programme.grant_template or []) if isinstance(item, dict)}
    return next((p for p in LOOKUP_ORDER if p in products and runner.streak[p] >= TRIP_AFTER), "")


def _grant(db: Session, user: User, mentee: Mentee | None, shared: Shared, out: Outcome,
           connectors: dict[str, BaseConnector], runner: LookupRunner) -> None:
    if mentee is None:
        out.grants, out.grants_note = "not_run", "not run: no new enrolment"
        return
    cleared = any(p.cleared_at is not None for p in mentee.payments)
    if not cleared:
        out.grants, out.grants_note = "not_run", "nothing to grant until a payment is recorded and cleared"
        return
    if not shared.grant_now:
        out.grants, out.grants_note = "not_run", "not asked for: the nightly recompute at 03:10 will grant"
        return
    if not mentees_router.engine_available():
        out.grants, out.grants_note = "not_run", "not run: the entitlement engine is not available on this server"
        return
    down = _tripped(mentee, runner)
    if down:
        out.grants, out.grants_note = "not_run", (f"not run: {settings.product_name(down)} failed for {TRIP_AFTER} people "
                                                  "in a row in this batch; the nightly recompute at 03:10 will grant")
        return
    result, error = mentees_router.run_recompute(db, mentee, user, connectors=connectors)
    if error:
        out.grants, out.grants_note = "failed", f"recompute failed: {error[:300]}"
        return
    out.grants = "run"
    out.decisions = decision_rows(result)
    if not out.decisions:
        out.grants_note = mentees_router._describe(result).strip()


def import_one(db: Session, user: User, entry: Entry, shared: Shared, runner: LookupRunner,
               connectors: dict[str, BaseConnector]) -> Outcome:
    """Everything for one person, committed on its own. Never raises: a failure is on the Outcome."""
    out = Outcome(entry=entry, person_notes=list(entry.notes))
    out.lookups = runner.run(entry.email)
    try:
        person = _get_or_create_person(db, user, entry, out)
        out.person_id = person.id
        before = links_snapshot(person)
        filled, notes = fill_links(db, person, out.lookups)
        out.person_notes.extend(notes)
        if filled:
            db.add(person)
            b, a = changes(before, links_snapshot(person))
            audit(db, user, "person.links", f"person:{person.id}", before=b, after={**a, "via": "import lookup"})
            out.filled = filled
        mentee = _enrol(db, user, person, shared, out)
        db.commit()
    except Exception as error:  # noqa: BLE001  (one person's failure is reported on their row, never the batch's)
        db.rollback()
        failed = Outcome(entry=entry, person="failed", person_notes=out.person_notes, lookups=out.lookups,
                         error=f"Not imported, nothing was saved for this person: {error}"[:400])
        return failed
    if mentee is not None:
        out.access = mentees_router.access_summary(mentee)["label"]
    _grant(db, user, mentee, shared, out, connectors, runner)
    return out


def run_batch(db: Session, user: User, entries: list[Entry], shared: Shared) -> list[Outcome]:
    """Import every entry (the caller has already capped the batch at MAX_BATCH), then one audit row for the
    batch as a whole."""
    outcomes: list[Outcome] = []
    with open_connectors(switches=settings.product_switches(db)) as connectors:
        runner = LookupRunner(connectors)
        for entry in entries:
            outcomes.append(import_one(db, user, entry, shared, runner, connectors))
    grants = Counter()
    for o in outcomes:
        grants.update(o.grant_counts)
    audit(db, user, "mentee.import", f"programme:{shared.programme.id}", after={
        "programme": shared.programme.code, "people": len(outcomes),
        "people_created": sum(o.person == "created" for o in outcomes),
        "mentees_created": sum(o.mentee == "created" for o in outcomes),
        "mentees_skipped": sum(o.mentee == "skipped" for o in outcomes),
        "payments": sum(o.payment_recorded for o in outcomes),
        "recomputed": sum(o.grants == "run" for o in outcomes),
        "grants": dict(grants),
        "failed": [o.entry.email for o in outcomes if o.person == "failed"] or None,
        "lookup_failures": sum(1 for o in outcomes if o.lookup_problems),
    })
    db.commit()
    return outcomes


# ---- what the page shows before anything is ticked ---------------------------------------------------------
def kp_listing(offset: int = 0, limit: int = KP_PAGE, *, switches: dict | None = None) -> dict:
    """One page of Key Properties sourcers, fetched live, or why there is none: state is ok, dry_run,
    not_configured or failed. Never raises, so the rest of the import page always renders. `switches` are the
    product switches (settings.product_switches)."""
    connector = KPConnector.from_config(dry_run=product_dry_run("kp", switches=switches))
    listing: dict = {"state": "", "rows": [], "total": None, "offset": max(0, offset), "limit": limit, "detail": "", "plan": None}
    try:
        if connector.dry_run:
            listing.update(state="dry_run", plan=connector.list_sourcers(limit, offset).plan)
        elif not connector.configured:
            listing.update(state="not_configured")
        else:
            data = connector.list_sourcers(limit, offset).data
            listing.update(state="ok", rows=data["sourcers"], total=data["total"])
    except ConnectorError as error:
        listing.update(state="failed", detail=error.summary()[:300])
    except Exception as error:  # noqa: BLE001  (a surprise from the portal must not take the page down)
        listing.update(state="failed", detail=f"{type(error).__name__}: {error}"[:300])
    finally:
        connector.close()
    return listing


def register_badges(db: Session, emails: list[str]) -> dict[str, dict]:
    """For each email already on the register: {"person": Person, "mentees": [Mentee, ...]} (programme loaded)."""
    keys = sorted({normalise_email(e) for e in emails if e})
    if not keys:
        return {}
    people = db.query(Person).filter(func.lower(Person.person_key).in_(keys)).all()
    if not people:
        return {}
    mentees = (db.query(Mentee).options(selectinload(Mentee.programme)).filter(Mentee.person_id.in_([p.id for p in people]))
               .order_by(Mentee.id).all())
    out = {normalise_email(p.person_key): {"person": p, "mentees": []} for p in people}
    by_id = {p.id: normalise_email(p.person_key) for p in people}
    for m in mentees:
        out[by_id[m.person_id]]["mentees"].append(m)
    return out


def product_modes(db: Session) -> list[dict]:
    """How each product will be treated by a batch run now: live, dry run, or not configured, as the product
    switches on the Settings screen and the environment decide it (settings.product_modes)."""
    return settings.product_modes(db, LOOKUP_ORDER)


def today() -> date:
    return utcnow().date()
