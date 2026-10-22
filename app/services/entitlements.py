"""The entitlement rule and the recompute that applies it through the connectors.

The rule (plan, Phase 1):

    full     access_ends = start_date + 12 months, once a cleared payment covering the programme exists
             (the cleared total has reached programmes.price; with no price set, any cleared payment counts)
    monthly  access_ends = latest cleared payment.period_end + 7 days of grace; None while nothing is cleared

access_ends is a date. A grant row's ends_at is midnight UTC at the start of that date, so the person
has access up to and including the day before: paid in full on 1 October 2026 means access through
30 September 2027, and a monthly period ending 31 October means access through 7 November. A programme
template item with "months" runs that many months from start_date and never beyond the mentee's
computed end. A mentee marked ended loses access today whatever has been paid.

recompute_person decides, for every item in every programme the person is on (one grant per product,
kind and value; the latest end wins when two programmes give the same thing):

    grant or extend   the end is in the future and the grant is not active, or the product holds a
                      shorter end than the one computed
    revoke            the end has passed (or no longer exists) and the grant is active
    nothing           otherwise, including a blocked grant (paid subscription) and a mentee whose
                      start date has not arrived

Every grant, extension and revoke is a connector_calls row keyed grant:<id>:<ends_at>, or
grant:<id>:revoke:<ends_at> for a revoke, so a re-run never sends the same call twice and a failed
call is retried on the next run, up to MAX_ATTEMPTS, after which the grant is marked failed until the
end date changes or someone presses Retry. A grant sent again after a successful revoke (the founder
undid a Revoke all) carries :after:<revoke call id>, so the earlier succeeded row cannot be mistaken for
it. With dry_run (or CONNECTORS_DRY_RUN=1) the row is written with the planned call as its response and
nothing is sent; the next real run sends it.

Guards that hold whatever the caller asks for: an end more than MAX_GRANT_DAYS away is never sent (the
grant shows why); a grant or extension whose end is not in the real future is never sent; orphaned
grants are revoked before new ones are granted (PP holds one comp per account and revokes by email);
a person is recomputed under a Postgres advisory lock so the nightly run and a button press cannot
send the same call twice; an active grant keeps the window the product confirmed until a new one is
confirmed, so a failed or dry-run extension is sent on the next run.

The database cannot hold a "blocked" state (entitlement_grants.state is checked against
pending|active|revoked|failed), so a 409 paid_subscription becomes state failed with last_error
BLOCKED_ERROR; is_blocked() reads that back and the recompute never retries it on its own.

A hold (Pause on a grant, or Pause all access for a person) overrides the rule: a held grant that is active is
revoked through the outbox like any other revoke, and a held grant that is not active is left alone ("held"),
whatever the payments say, so the nightly run never grants it back. Resume clears the hold and recomputes the
person at once. A revoke of a grant that has been revoked and granted again before carries
:after:<the grant call id>, so pausing, resuming and pausing again on one day asks the product every time.

Which products are sent anything is connectors.product_dry_run with the product switches from the database
(settings.product_switches): the web service and the nightly job read the same switches. A run reads them as it
starts, and again before each person so that a product switched to Dry run while a run is under way is sent
nothing more; a switch to Live waits for the next run.
"""
from __future__ import annotations

import calendar
import json
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Iterator

from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from .. import config
from ..models import ConnectorCall, EntitlementGrant, Mentee, Payment, Person, Programme, User, utcnow
from . import settings
from .audit import _jsonable, audit
from .connectors import (CONNECTORS, BaseConnector, ConnectorError, ConnectorResult, open_connectors, product_dry_run,
                         product_mode)
from .connectors.pp import PP_TIERS, pp_tier

MAX_ATTEMPTS = 5
MAX_GRANT_DAYS = 400            # no grant or extension ever ends further away than this (a typo guard)
MAX_FULL_TERM_MONTHS = 13       # the longest term "paid in full" can be given, whatever the setting says
LOCK_PERSON = 1                 # pg_advisory_lock(LOCK_PERSON, person.id)
LOCK_RUN = 2                    # pg_try_advisory_lock(LOCK_RUN, 0): one nightly run at a time
RUN_IN_PROGRESS = "another recompute is already running; nothing was done"
BLOCKED_ERROR = "paid subscription"
PROGRAMME_SOURCE = "programme:"
JOB_ACTOR = "job:recompute"

ACTIONS = ("grant", "extend", "revoke", "noop", "blocked", "gave_up", "not_started", "held")
OUTCOMES = ("sent", "planned", "failed", "blocked", "skipped")


# ---- the rule --------------------------------------------------------------------------
def grace_days() -> int:
    try:
        return int(settings.get("grace_days") or 7)
    except (TypeError, ValueError):
        return 7


def full_term_months() -> int:
    try:
        months = int(settings.get("full_term_months") or 12)
    except (TypeError, ValueError):
        return 12
    return max(1, min(months, MAX_FULL_TERM_MONTHS))


def real_today() -> date:
    """The clock the hard floor reads: tests may replace it, callers may not."""
    return datetime.now(timezone.utc).date()


def programme_price(mentee: Mentee) -> Decimal | None:
    try:
        price = mentee.programme.price if mentee.programme is not None else None
    except Exception:   # noqa: BLE001  (a detached or half-built mentee has no programme to ask)
        return None
    return Decimal(str(price)) if price is not None else None


def cleared_total(payments: Iterable[Payment]) -> Decimal:
    return sum((Decimal(str(p.amount)) for p in payments if p.cleared_at is not None), Decimal("0"))


def add_months(day: date, months: int) -> date:
    """Calendar months, clamped to the month end: 31 January + 1 month is 28 (or 29) February."""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def at_midnight(day: date | None) -> datetime | None:
    return datetime.combine(day, time.min) if day else None


def compute_access_end(mentee: Mentee, payments: Iterable[Payment], today: date | None = None) -> date | None:
    """The date access ends under the rule, or None while nothing cleared supports any access.

    `today` is accepted for symmetry with the callers (the rule itself does not depend on it)."""
    cleared = [p for p in payments if p.cleared_at is not None]
    if not cleared:
        return None
    if mentee.payment_mode == "full":
        price = programme_price(mentee)
        if price is not None and cleared_total(cleared) < price:
            return None     # a deposit or a part payment does not cover the programme
        return add_months(mentee.start_date, full_term_months())
    if mentee.payment_mode == "monthly":
        ends = [p.period_end for p in cleared if p.period_end is not None]
        if not ends:
            return None
        return max(ends) + timedelta(days=grace_days())
    raise ValueError(f"unknown payment_mode {mentee.payment_mode!r}")


def is_blocked(grant: EntitlementGrant) -> bool:
    return grant.state == "failed" and grant.last_error == BLOCKED_ERROR


def hold_of(grant: EntitlementGrant, person: Person | None = None) -> dict | None:
    """The hold keeping this grant off, for the screens and the rule: the grant's own Pause first, then the
    person's Pause all access. None when neither is set."""
    if grant.held_at is not None:
        return {"scope": "grant", "at": grant.held_at, "by": grant.held_by, "reason": grant.held_reason}
    person = person if person is not None else grant.person
    if person is not None and person.access_held_at is not None:
        return {"scope": "person", "at": person.access_held_at, "by": person.access_held_by,
                "reason": person.access_held_reason}
    return None


def _hold_since(grant: EntitlementGrant, person: Person) -> datetime | None:
    """When the earliest hold on the grant began. The revoke it enforces ends access at midnight that day, so a
    revoke that fails is retried under the same key on the following nights."""
    stamps = [t for t in (grant.held_at, person.access_held_at) if t is not None]
    return min(stamps) if stamps else None


def display_state(grant: EntitlementGrant) -> str:
    """What the screens show: the database state, or "blocked" for a paid-subscription refusal."""
    return "blocked" if is_blocked(grant) else grant.state


def grant_key(grant_id: int, ends_at: datetime | None, after_revoke: int | None = None) -> str:
    """after_revoke is the id of the last succeeded revoke call for the grant, when there is one: a grant
    that follows a revoke must never match the row that was sent before it."""
    key = f"grant:{grant_id}:{ends_at.isoformat() if ends_at else 'none'}"
    return f"{key}:after:{after_revoke}" if after_revoke else key


def revoke_key(grant_id: int, ends_at: datetime | None, after_grant: int | None = None) -> str:
    """after_grant is the id of the succeeded grant or extension that followed the grant's last succeeded revoke,
    when there is one: a revoke that follows a re-grant must never match the earlier revoke's row (pause, resume
    and pause again on one day enforce the same end)."""
    key = f"grant:{grant_id}:revoke:{ends_at.isoformat() if ends_at else 'none'}"
    return f"{key}:after:{after_grant}" if after_grant else key


# ---- what a run produces -----------------------------------------------------------------
@dataclass
class Decision:
    person_id: int
    person_key: str
    grant_id: int | None
    product: str
    kind: str
    value: str
    action: str                 # grant | extend | revoke | noop | blocked | gave_up | not_started | held
    outcome: str                # sent | planned | failed | blocked | skipped
    ends_at: datetime | None = None
    key: str = ""
    call_id: int | None = None
    detail: str = ""
    plan: dict | None = None
    dry_run: bool = False
    once: str = ""              # shown on the run page only, never stored or logged (a temporary password HNH issued)

    @property
    def label(self) -> str:
        return f"{self.product} {self.kind} {self.value}"


@dataclass
class RunSummary:
    today: date
    dry_run: bool
    people: int = 0
    decisions: list[Decision] = field(default_factory=list)
    note: str = ""              # why a run did nothing (another run held the lock)
    products: dict = field(default_factory=dict)    # {product: live | dry_run | not_configured} as this run saw them
    configured: dict = field(default_factory=dict)  # {product: True when this process holds its token and base URL}
    stopped: list = field(default_factory=list)     # products switched to Dry run on Settings while the run was under way

    @property
    def actions(self) -> Counter:
        return Counter(d.action for d in self.decisions)

    @property
    def outcomes(self) -> Counter:
        return Counter(d.outcome for d in self.decisions)

    @property
    def failed(self) -> int:
        return self.outcomes.get("failed", 0)

    @property
    def calls(self) -> list[Decision]:
        return [d for d in self.decisions if d.outcome in ("sent", "planned", "failed", "blocked")]

    @property
    def unconfigured(self) -> int:
        return sum(1 for d in self.decisions if d.outcome == "skipped" and d.detail.startswith("not configured"))

    def line(self) -> str:
        a, o = self.actions, self.outcomes
        mode = "DRY RUN, nothing sent" if self.dry_run else "live"
        extra = f", {self.unconfigured} not configured" if self.unconfigured else ""
        if a.get("held"):
            extra += f", {a['held']} held"
        if self.note:
            extra += f"; {self.note}"
        return (f"recompute {self.today.isoformat()} ({mode}): {self.people} people, {len(self.decisions)} decisions: "
                f"{a.get('grant', 0)} granted, {a.get('extend', 0)} extended, {a.get('revoke', 0)} revoked, "
                f"{a.get('noop', 0) + a.get('not_started', 0)} unchanged, {a.get('blocked', 0)} blocked, "
                f"{a.get('gave_up', 0)} given up; {o.get('sent', 0)} sent, {o.get('planned', 0)} planned, "
                f"{o.get('failed', 0)} failed{extra}")


@dataclass
class _Target:
    ends: date | None
    mentee: Mentee | None
    programme: Programme | None
    item: dict
    starts: date | None = None

    @property
    def extra(self) -> dict:
        return {k: v for k, v in self.item.items() if k not in ("product", "kind", "value", "months")}


# ---- working out what each person should hold ------------------------------------------------
def _later(a: date | None, b: date | None) -> bool:
    """True when a is a later end than b (None is the earliest possible end)."""
    if a is None:
        return False
    return b is None or a > b


def _item_end(item: dict, mentee: Mentee, computed: date | None) -> date | None:
    if computed is None:
        return None
    months = item.get("months")
    if months:
        try:
            fixed = add_months(mentee.start_date, int(months))
        except (TypeError, ValueError):
            return computed
        return min(fixed, computed)
    return computed


def _mentee_end(db: Session, mentee: Mentee, today: date) -> date | None:
    # Queried, not read off the relationship: a payment added in this session by mentee_id would be
    # missing from an already-loaded mentee.payments collection.
    payments = db.query(Payment).filter(Payment.mentee_id == mentee.id).all()
    computed = compute_access_end(mentee, payments, today)
    if mentee.status == "ended" and computed is not None:
        computed = min(computed, today)
    return computed


def _tier_rank(value: str) -> int:
    """Where a PP tier sits in PP_TIERS (free < pro_buyer < pro_investor < pro_business); unknown is lowest."""
    try:
        return PP_TIERS.index(pp_tier(value))
    except ConnectorError:
        return -1


def _slot(key: tuple) -> tuple:
    """PP holds one comp per account, so every pp comp_tier item competes for a single slot; every other
    right is its own slot."""
    product, kind, value = key
    return (product, kind) if (product, kind) == ("pp", "comp_tier") else key


def _wins(key: tuple, end: date | None, current_key: tuple, current: _Target) -> bool:
    """Does (key, end) replace the target already in the slot? For the PP comp slot the higher tier wins
    and the later end breaks a tie; elsewhere the later end wins."""
    if _slot(key) == ("pp", "comp_tier") and key != current_key:
        mine, theirs = _tier_rank(key[2]), _tier_rank(current_key[2])
        if mine != theirs:
            return mine > theirs
    return _later(end, current.ends)


def targets_for(db: Session, person: Person, today: date, *, actor: User | str = JOB_ACTOR) -> dict[tuple, _Target]:
    """One target per (product, kind, value) across every programme the person is on, with the latest
    end; for pp comp_tier only one target survives (the highest tier, PP holds one comp per account).
    Also moves a mentee between active and lapsed as its payments dictate (never out of ended)."""
    targets: dict[tuple, _Target] = {}
    slots: dict[tuple, tuple] = {}          # slot -> the key holding it
    mentees = db.query(Mentee).filter(Mentee.person_id == person.id).order_by(Mentee.id).all()
    for mentee in mentees:
        computed = _mentee_end(db, mentee, today)
        _sync_mentee_status(db, mentee, computed, today, actor)
        for item in mentee.programme.grant_template or []:
            if not isinstance(item, dict) or not all(item.get(k) for k in ("product", "kind", "value")):
                continue
            key = (str(item["product"]), str(item["kind"]), str(item["value"]))
            end = _item_end(item, mentee, computed)
            slot = _slot(key)
            holder = slots.get(slot)
            if holder is not None and not _wins(key, end, holder, targets[holder]):
                continue
            if holder is not None and holder != key:
                del targets[holder]
            slots[slot] = key
            targets[key] = _Target(ends=end, mentee=mentee, programme=mentee.programme, item=item,
                                   starts=mentee.start_date)
    return targets


def _sync_mentee_status(db: Session, mentee: Mentee, computed: date | None, today: date, actor) -> None:
    if mentee.status == "ended" or computed is None:
        return
    wanted = "active" if computed > today else "lapsed"
    if wanted != mentee.status:
        before = {"status": mentee.status}
        mentee.status = wanted
        db.add(mentee)
        audit(db, actor, f"mentee.{'reactivate' if wanted == 'active' else 'lapse'}", f"mentee:{mentee.id}",
              before=before, after={"status": wanted, "access_ends": computed.isoformat()})


# ---- applying the decision through the outbox ---------------------------------------------------
def _json(value):
    """Anything, as JSON the JSONB columns will take."""
    return json.loads(json.dumps(value, default=str))


def _stored_response(value) -> dict:
    """A connector's answer as the connector_calls row keeps it: any key naming a password, secret or token is
    replaced by "[redacted]" first (audit._jsonable), so a product's body can never put a credential into
    the database or onto the grant page. The HNH connector also removes its one-time password before this,
    so the redaction is the second line, not the only one."""
    return _json(_jsonable(value))


def _snapshot(grant: EntitlementGrant) -> dict:
    return {"state": grant.state, "ends_at": grant.ends_at.isoformat() if grant.ends_at else None,
            "remote_ref": grant.remote_ref, "last_error": grant.last_error}


def _find_or_create_grant(db: Session, person: Person, key: tuple, target: _Target,
                          grant: EntitlementGrant | None) -> EntitlementGrant:
    if grant is None:
        product, kind, value = key
        grant = EntitlementGrant(person_id=person.id, product=product, kind=kind, value=value, starts_at=utcnow(),
                                 state="pending")
        db.add(grant)
    if target.programme is not None:
        grant.source = f"{PROGRAMME_SOURCE}{target.programme.code}"[:60]
        mode = {"full": "paid in full", "monthly": "paying monthly"}.get(target.mentee.payment_mode if target.mentee else "", "")
        grant.reason = f"{target.programme.name}{', ' + mode if mode else ''}"[:200]
    if target.mentee is not None:
        grant.mentee_id = target.mentee.id
    db.flush()
    return grant


def _decide(grant: EntitlementGrant, target: _Target, today: date,
            held: datetime | None = None, remote: bool = False) -> tuple[str, datetime | None]:
    """(action, ends_at) for one grant. A grant that is not yet active takes its intended window as a side
    effect; an active grant keeps the window the product confirmed until _execute confirms a new one. A held
    grant (held is when the hold began) is revoked while active, or while the product still holds a window for
    it (remote: an extension failed until the grant was marked failed), and otherwise left exactly as it is."""
    end = target.ends
    ends_at = at_midnight(end)
    if held is not None:
        if grant.state == "active" or remote:
            stop = at_midnight(held.date())
            return "revoke", stop if grant.ends_at is None or grant.ends_at > stop else grant.ends_at
        return "held", grant.ends_at
    if is_blocked(grant):
        return "blocked", grant.ends_at
    if end is not None and end > today:
        if target.starts is not None and target.starts > today:
            if grant.state == "active":
                # the start was moved into the future by hand while the product still has access on
                enforced = at_midnight(today)
                grant.ends_at = enforced
                return "revoke", enforced
            return "not_started", ends_at
        if end > today + timedelta(days=MAX_GRANT_DAYS):
            grant.last_error = (f"end {end.isoformat()} is beyond the {MAX_GRANT_DAYS} day ceiling: "
                                "check the payment periods and the programme term")[:1000]
            return "noop", grant.ends_at
        if grant.state == "active":
            remote_until = grant.ends_at
            if remote_until is None or remote_until < ends_at:
                return "extend", ends_at
            return "noop", ends_at
        if target.starts is not None:
            grant.starts_at = at_midnight(target.starts)
        grant.ends_at = ends_at
        return "grant", ends_at
    # the end has passed, or nothing supports access any more
    if grant.state == "active":
        enforced = ends_at if ends_at is not None and ends_at <= at_midnight(today) else at_midnight(today)
        grant.ends_at = enforced
        return "revoke", enforced
    if ends_at is not None:
        grant.ends_at = ends_at
    return "noop", grant.ends_at


def _supersede(db: Session, grant: EntitlementGrant, key: str) -> None:
    """Older outbox rows for this grant that never went anywhere are dropped; ones that were attempted
    keep their history and are marked superseded so nobody retries them."""
    rows = db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.idempotency_key != key,
                                          ConnectorCall.status != "succeeded").all()
    for row in rows:
        if row.attempts == 0:
            db.delete(row)
        else:
            response = dict(row.response) if isinstance(row.response, dict) else {"previous": row.response}
            response["superseded_by"] = key
            row.response = response
            row.status = "failed"
            db.add(row)


def _apply_links(person: Person, links: dict) -> dict:
    """Fill empty person ids from what the product told us; never overwrite a link already recorded."""
    filled = {}
    for field_name, value in (links or {}).items():
        if value in (None, "") or not hasattr(person, field_name):
            continue
        if getattr(person, field_name) in (None, ""):
            setattr(person, field_name, value)
            filled[field_name] = value
    return filled


def _last_succeeded_revoke(db: Session, grant: EntitlementGrant) -> int | None:
    row = (db.query(ConnectorCall.id).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.action == "revoke",
                                              ConnectorCall.status == "succeeded").order_by(ConnectorCall.id.desc()).first())
    return row[0] if row else None


def _product_holds(db: Session, grant: EntitlementGrant) -> bool:
    """Does the product still hold a window for this grant, as far as the outbox knows? True when a grant or an
    extension succeeded after the last succeeded revoke: a failed extension leaves the old window in place even
    once the grant reads failed."""
    last_revoke = _last_succeeded_revoke(db, grant) or 0
    return (db.query(ConnectorCall.id).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.action.in_(("grant", "extend")),
                                              ConnectorCall.status == "succeeded", ConnectorCall.id > last_revoke)
            .first()) is not None


def _regrant_since_revoke(db: Session, grant: EntitlementGrant) -> int | None:
    """The last succeeded grant or extension sent after the grant's last succeeded revoke, when it has been
    revoked before; None for a grant never revoked, so a first revoke keeps the plain key."""
    last_revoke = _last_succeeded_revoke(db, grant)
    if last_revoke is None:
        return None
    row = (db.query(ConnectorCall.id).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.action.in_(("grant", "extend")),
                                              ConnectorCall.status == "succeeded", ConnectorCall.id > last_revoke)
           .order_by(ConnectorCall.id.desc()).first())
    return row[0] if row else None


def _floor_refusal(action: str, ends_at: datetime | None) -> str:
    """A grant or extension must end in the real future, whatever as-of date the caller used."""
    if action == "revoke":
        return ""
    if ends_at is None or ends_at <= at_midnight(real_today()):
        return f"refused: end {ends_at.date().isoformat() if ends_at else 'none'} is not in the future"
    return ""


def _execute(db: Session, grant: EntitlementGrant, person: Person, target: _Target, action: str,
             ends_at: datetime | None, *, dry_run: bool, connectors: dict[str, BaseConnector], actor,
             reason: str = "") -> Decision:
    key = (revoke_key(grant.id, ends_at, _regrant_since_revoke(db, grant)) if action == "revoke"
           else grant_key(grant.id, ends_at, _last_succeeded_revoke(db, grant)))
    decision = Decision(person_id=person.id, person_key=person.person_key, grant_id=grant.id, product=grant.product,
                        kind=grant.kind, value=grant.value, action=action, outcome="skipped", ends_at=ends_at, key=key,
                        dry_run=dry_run)
    refusal = _floor_refusal(action, ends_at)
    if refusal:
        decision.outcome, decision.detail = "failed", refusal
        return decision
    _supersede(db, grant, key)
    row = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == key).with_for_update().first()
    if row is None:
        row = ConnectorCall(grant_id=grant.id, product=grant.product, action=action, idempotency_key=key,
                            payload={"email": person.person_key, "product": grant.product, "kind": grant.kind,
                                     "value": grant.value, "ends_at": ends_at.isoformat() if ends_at else None,
                                     "action": action}, status="pending")
        db.add(row)
        db.flush()
    decision.call_id = row.id
    if row.status == "succeeded":
        # The same call already went through (a crash between the call and the grant update, or a hand edit).
        grant.state = "revoked" if action == "revoke" else "active"
        grant.last_error = ""
        decision.action, decision.detail = "noop", "already sent"
        return decision
    if row.attempts >= MAX_ATTEMPTS:
        grant.state = "failed"
        decision.action, decision.detail = "gave_up", f"gave up after {row.attempts} attempts: {grant.last_error}"
        return decision

    connector = connectors[grant.product]
    reason = (reason or f"Group HQ: {grant.reason or grant.source or 'manual grant'} (grant {grant.id})")[:200]
    dry_run = dry_run or connector.dry_run       # the product may be in dry run on its own (CONNECTORS_DRY_RUN=ordris,kp,hnh)
    decision.dry_run = dry_run

    def call() -> ConnectorResult:
        """The connector reads the window off the grant row, so the row carries the window being asked for
        during the call only; _execute writes it for good once the product has confirmed it."""
        if action == "revoke":
            return connector.revoke(grant, person, reason=reason)
        held = (grant.starts_at, grant.ends_at)
        grant.ends_at = ends_at
        if target.starts is not None:
            grant.starts_at = at_midnight(target.starts)
        try:
            return connector.grant(grant, person, reason=reason, extra=target.extra)
        finally:
            grant.starts_at, grant.ends_at = held

    if dry_run:
        try:
            result = call()
        except ConnectorError as error:   # an unsupported kind, for example: still only a plan
            row.response = _stored_response(error.as_dict())
            decision.outcome, decision.detail = "failed", error.summary()
            return decision
        row.response = _stored_response(result.as_dict())
        if result.plan:
            row.payload = _json({**row.payload, "request": result.plan})
        decision.outcome, decision.plan = "planned", result.plan
        decision.detail = result.noop or "planned"
        db.add(row)
        return decision

    if not connector.configured:
        # No base URL or token: the attempt is not counted, so the five tries are kept for a real failure.
        decision.detail = f"not configured: {grant.product} has no base URL or token, nothing sent"
        return decision

    before = _snapshot(grant)
    row.attempts += 1
    row.last_attempt_at = utcnow()
    db.add(row)
    db.commit()     # the attempt is on record before the wire is touched
    try:
        result = call()
    except ConnectorError as error:
        row.response = _stored_response(error.as_dict())
        if error.plan:
            row.payload = _json({**row.payload, "request": error.plan})
        row.status = "failed"
        if error.reason == "paid_subscription":
            grant.state = "failed"
            grant.last_error = BLOCKED_ERROR
            row.attempts = MAX_ATTEMPTS        # never retried on its own
            decision.action, decision.outcome, decision.detail = "blocked", "blocked", BLOCKED_ERROR
            audit(db, actor, "grant.block", f"grant:{grant.id}", before=before, after=_snapshot(grant))
        else:
            grant.last_error = error.summary()[:1000]
            # A first grant that failed is failed, and so is anything that has used up its attempts. An
            # active grant whose extension or revoke failed stays active until the retry: the product
            # still holds the old window and the person still has access.
            if row.attempts >= MAX_ATTEMPTS or grant.state != "active":
                grant.state = "failed"
            decision.outcome, decision.detail = "failed", grant.last_error
            audit(db, actor, "grant.fail", f"grant:{grant.id}", before=before,
                  after={**_snapshot(grant), "attempts": row.attempts, "action": action})
        grant.last_sync_at = utcnow()
        db.add(grant)
        return decision

    row.status = "succeeded"
    row.response = _stored_response(result.as_dict())
    if result.plan:
        row.payload = _json({**row.payload, "request": result.plan})
    grant.state = "revoked" if action == "revoke" else "active"
    if action != "revoke":
        grant.ends_at = ends_at                 # the product has confirmed this window
        if target.starts is not None:
            grant.starts_at = at_midnight(target.starts)
    elif ends_at is not None and (grant.ends_at is None or grant.ends_at > ends_at):
        grant.ends_at = ends_at                 # a held grant: access stopped on the day the hold began
    grant.last_error = ""
    grant.last_sync_at = utcnow()
    if result.remote_ref:
        grant.remote_ref = result.remote_ref[:200]
    filled = _apply_links(person, result.links)
    db.add_all([row, grant, person])
    decision.outcome = "sent"
    decision.detail = result.noop or "sent"
    decision.plan = result.plan
    decision.once = result.once
    audit(db, actor, f"grant.{action}", f"grant:{grant.id}", before=before,
          after={**_snapshot(grant), "noop": result.noop or None, "links": filled or None})
    return decision


def _skip(person: Person, grant: EntitlementGrant, action: str, ends_at: datetime | None, detail: str,
          dry_run: bool) -> Decision:
    return Decision(person_id=person.id, person_key=person.person_key, grant_id=grant.id, product=grant.product,
                    kind=grant.kind, value=grant.value, action=action, outcome="skipped", ends_at=ends_at,
                    detail=detail, dry_run=dry_run)


def effective_dry_run(dry_run: bool | None) -> bool:
    """A run is a dry run when asked for, or whenever CONNECTORS_DRY_RUN is on in the environment."""
    return bool(dry_run) or bool(config.CONNECTORS_DRY_RUN)


@contextmanager
def advisory_lock(db: Session, space: int, key: int, *, wait: bool = True) -> Iterator[bool]:
    """A Postgres session-level advisory lock held on a connection of its own for the whole block, so the
    commits inside it (the outbox records an attempt before the wire) do not release it. Yields whether the
    lock was taken (always True when waiting)."""
    conn = db.get_bind().connect()
    got = False
    try:
        if wait:
            conn.execute(text("SELECT pg_advisory_lock(:s, :k)"), {"s": space, "k": key})
            got = True
        else:
            got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:s, :k)"), {"s": space, "k": key}).scalar())
        yield got
    finally:
        if got:
            conn.execute(text("SELECT pg_advisory_unlock(:s, :k)"), {"s": space, "k": key})
        conn.close()


def recompute_person(db: Session, person: Person, today: date | None = None, dry_run: bool | None = None, *,
                     actor: User | str = JOB_ACTOR, connectors: dict[str, BaseConnector] | None = None) -> list[Decision]:
    """Bring one person's grants in line with the rule, under the person's advisory lock. Commits. Returns one
    Decision per grant. Grants no programme gives any more are revoked BEFORE the targets are granted: PP holds
    one comp per account and revokes by email, so a tier change must take the old comp away first."""
    today = today or utc_today()
    dry = effective_dry_run(dry_run)
    own_pool = connectors is None
    if own_pool:
        pool_cm = open_connectors(dry_run=dry, switches=settings.product_switches(db))
        connectors = pool_cm.__enter__()
    assert connectors is not None
    try:
        with advisory_lock(db, LOCK_PERSON, person.id):
            db.refresh(person)      # a Pause all access committed since the person was loaded (mid-run) must count
            targets = targets_for(db, person, today, actor=actor)
            existing = {(g.product, g.kind, g.value): g for g in
                        db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id,
                                                          EntitlementGrant.source.like(PROGRAMME_SOURCE + "%"))
                        .order_by(EntitlementGrant.id).all()}
            reused = {key: existing.pop(key) for key in list(existing) if key in targets}
            decisions: list[Decision] = []
            for grant in existing.values():     # no programme gives this any more: treat as nothing supports it
                orphan = _Target(ends=None, mentee=grant.mentee, programme=None, item={})
                decisions.append(_apply_one(db, person, grant, orphan, today, dry, connectors, actor))
            for key, target in targets.items():
                grant = _find_or_create_grant(db, person, key, target, reused.get(key))
                decisions.append(_apply_one(db, person, grant, target, today, dry, connectors, actor))
            db.commit()
            return decisions
    finally:
        if own_pool:
            pool_cm.__exit__(None, None, None)


def _apply_one(db: Session, person: Person, grant: EntitlementGrant, target: _Target, today: date, dry: bool,
               connectors: dict[str, BaseConnector], actor) -> Decision:
    hold = hold_of(grant, person)
    held = _hold_since(grant, person)
    remote = held is not None and grant.state != "active" and _product_holds(db, grant)
    action, ends_at = _decide(grant, target, today, held, remote)
    db.add(grant)
    why = "paused" if hold and hold["scope"] == "grant" else "all access paused"
    if action == "held":
        return _skip(person, grant, action, ends_at, f"{why}: kept off until resumed", dry)
    # The revoke a hold enforces tells the product only that Group HQ paused it: the reason typed on the pause
    # form stays in Group HQ (the grant, the person and the audit rows), since products such as Hello New Home
    # belong to another company and keep what they are sent.
    reason = f"Group HQ: {why} (grant {grant.id})" if hold is not None and action == "revoke" else ""
    if action == "blocked":
        return _skip(person, grant, action, ends_at, BLOCKED_ERROR + ": not retried automatically", dry)
    if action == "not_started":
        return _skip(person, grant, action, ends_at, f"starts {target.starts.isoformat()}", dry)
    if action == "noop":
        why = grant.last_error if "ceiling" in (grant.last_error or "") else "nothing to change"
        return _skip(person, grant, action, ends_at, why, dry)
    if grant.product not in connectors:
        return _skip(person, grant, "noop", ends_at, f"no connector for {grant.product}", dry)
    return _execute(db, grant, person, target, action, ends_at, dry_run=dry, connectors=connectors, actor=actor,
                    reason=reason)


def people_to_recompute(db: Session) -> list[Person]:
    """Everyone with a programme enrolment or a programme-sourced grant, in id order."""
    with_mentees = select(Mentee.person_id)
    with_grants = select(EntitlementGrant.person_id).where(EntitlementGrant.source.like(PROGRAMME_SOURCE + "%"))
    return (db.query(Person).filter(or_(Person.id.in_(with_mentees), Person.id.in_(with_grants)))
            .order_by(Person.id).all())


def recompute_all(db: Session, today: date | None = None, dry_run: bool | None = None, *,
                  actor: User | str = JOB_ACTOR) -> RunSummary:
    """The nightly job and the "Run nightly now" button. One commit per person, so a crash keeps progress."""
    today = today or utc_today()
    dry = effective_dry_run(dry_run)
    summary = RunSummary(today=today, dry_run=dry)
    with advisory_lock(db, LOCK_RUN, 0, wait=False) as got:
        if not got:
            summary.note = RUN_IN_PROGRESS
            return summary
        switches = settings.product_switches(db)        # the Settings screen's switches, as the run starts
        modes = [product_mode(p, switches=switches) for p in CONNECTORS]
        summary.products = {m["product"]: "dry_run" if dry else m["state"] for m in modes}
        summary.configured = {m["product"]: m["configured"] for m in modes}
        with open_connectors(dry_run=dry, switches=switches) as connectors:
            for person in people_to_recompute(db):
                _stop_switched_off(db, connectors, summary)
                summary.people += 1
                summary.decisions.extend(recompute_person(db, person, today, dry, actor=actor, connectors=connectors))
    audit(db, actor, "recompute.run", "job:recompute",
          after={"today": today.isoformat(), "dry_run": dry, "people": summary.people,
                 "actions": dict(summary.actions), "outcomes": dict(summary.outcomes), "products": summary.products,
                 "configured": summary.configured, "stopped": summary.stopped or None})
    db.commit()
    return summary


def _stop_switched_off(db: Session, connectors: dict[str, BaseConnector], summary: RunSummary) -> None:
    """Before each person of a run: a product switched to Dry run on the Settings screen since the run started is
    sent nothing more by this run (its connector is put in dry run, so its calls are planned). Only that way round:
    a product switched to Live waits for the next run."""
    switches = settings.product_switches(db)
    for product, connector in connectors.items():
        if not connector.dry_run and product_dry_run(product, switches=switches):
            connector.dry_run = True
            summary.stopped.append(product)


def revoke_person(db: Session, person: Person, today: date | None = None, dry_run: bool | None = None, *,
                  actor: User | str = JOB_ACTOR, reason: str = "") -> dict:
    """Switch everything off now (the mentee screen's Revoke all). Every active grant the person holds is
    revoked through its connector with today as the enforced end, and every mentee of theirs not already
    ended is marked ended, so the nightly rule agrees tomorrow instead of granting it all back. Commits.
    Returns counts for the screen, for example {"revoked": 3, "mentees ended": 1}."""
    today = today or utc_today()
    dry = effective_dry_run(dry_run)
    counts: Counter = Counter()
    with advisory_lock(db, LOCK_PERSON, person.id), \
            open_connectors(dry_run=dry, switches=settings.product_switches(db)) as connectors:
        for mentee in db.query(Mentee).filter(Mentee.person_id == person.id).order_by(Mentee.id).all():
            if mentee.status != "ended":
                before = {"status": mentee.status}
                mentee.status = "ended"
                db.add(mentee)
                audit(db, actor, "mentee.end", f"mentee:{mentee.id}", before=before,
                      after={"status": "ended", "reason": reason or None})
                counts["mentees ended"] += 1
        active = (db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id, EntitlementGrant.state == "active")
                  .order_by(EntitlementGrant.id).all())
        for grant in active:
            if grant.product not in connectors:
                counts["unchanged"] += 1
                continue
            enforced = min(grant.ends_at, at_midnight(today)) if grant.ends_at else at_midnight(today)
            grant.ends_at = enforced
            target = _Target(ends=enforced.date(), mentee=grant.mentee, programme=None, item={})
            decision = _execute(db, grant, person, target, "revoke", enforced, dry_run=dry, connectors=connectors,
                                actor=actor, reason=reason)
            counts[{"sent": "revoked", "planned": "planned", "failed": "failed", "blocked": "blocked"}.get(decision.outcome, "unchanged")] += 1
        db.commit()
    return dict(counts)


def reset_for_retry(db: Session, grant: EntitlementGrant, *, actor: User | str) -> None:
    """Clear a failed or blocked grant so the next recompute tries it again. Caller commits."""
    before = _snapshot(grant)
    for row in db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.status == "failed").all():
        row.status = "pending"
        row.attempts = 0
        db.add(row)
    if grant.state == "failed":
        grant.state = "pending"
    grant.last_error = ""
    db.add(grant)
    audit(db, actor, "grant.retry", f"grant:{grant.id}", before=before, after=_snapshot(grant))


# ---- holds: Pause and Resume -------------------------------------------------------------------------------
def _actor_label(actor: User | str | None) -> str:
    return (actor.email if isinstance(actor, User) else str(actor or "system"))[:200]


def _grant_hold_fields(grant: EntitlementGrant) -> dict:
    return {"held_at": grant.held_at, "held_by": grant.held_by or None, "held_reason": grant.held_reason or None}


def _person_hold_fields(person: Person) -> dict:
    return {"access_held_at": person.access_held_at, "access_held_by": person.access_held_by or None,
            "access_held_reason": person.access_held_reason or None}


def _hold_target(grant: EntitlementGrant) -> _Target:
    return _Target(ends=None, mentee=grant.mentee, programme=None, item={})


def _drop_waiting_revokes(db: Session, grant: EntitlementGrant) -> int:
    """On resume, a revoke still in the outbox (planned in dry run, or failed and waiting for a retry) must never
    go out later: a never-attempted row is deleted and an attempted one is closed as superseded."""
    rows = db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.action == "revoke",
                                          ConnectorCall.status != "succeeded").all()
    for row in rows:
        if row.attempts == 0:
            db.delete(row)
        else:
            response = dict(row.response) if isinstance(row.response, dict) else {"previous": row.response}
            response["superseded_by"] = "resume"
            row.response = response
            row.status = "failed"
            db.add(row)
    return len(rows)


def _hold_counts(decisions: list[Decision]) -> dict:
    """What Pause all access did to each grant, as counts for the screen. A revoke that had already used up its
    attempts was not sent again ("gave up"), and one refused by the product is "blocked": in both the product may
    still give access, so neither is counted as already off."""
    labels = {"sent": "revoked", "planned": "planned", "failed": "failed", "blocked": "blocked"}

    def label(d: Decision) -> str:
        if d.outcome in labels:
            return labels[d.outcome]
        if d.action == "gave_up":
            return "gave up"
        if d.detail.startswith("not configured"):
            return "not configured"
        return "already off"
    return dict(Counter(label(d) for d in decisions))


def pause_grant(db: Session, grant: EntitlementGrant, today: date | None = None, dry_run: bool | None = None, *,
                actor: User | str = JOB_ACTOR, reason: str = "") -> Decision:
    """Pause one grant (the grant's page and the mentee screen). The hold is recorded (grant.pause) and, when the
    grant is active, it is revoked in its product now through the outbox, exactly as the rule revokes; from then
    on the recompute keeps it off whatever the payments say, until resume_grant. A product in dry run plans the
    revoke and the next live run sends it. The reason is kept on the grant and in the audit row; the product is
    told only "Group HQ: paused (grant <id>)". Commits. Returns the decision for the grant."""
    today = today or utc_today()
    dry = effective_dry_run(dry_run)
    person = grant.person
    with advisory_lock(db, LOCK_PERSON, person.id), \
            open_connectors(dry_run=dry, switches=settings.product_switches(db)) as connectors:
        db.refresh(grant)
        db.refresh(person)
        if grant.held_at is None:
            before = _grant_hold_fields(grant)
            grant.held_at = utcnow()
            grant.held_by = _actor_label(actor)
            grant.held_reason = (reason or "").strip()[:200]
            db.add(grant)
            audit(db, actor, "grant.pause", f"grant:{grant.id}", before=before,
                  after={**_grant_hold_fields(grant), "state": grant.state})
        decision = _apply_one(db, person, grant, _hold_target(grant), today, dry, connectors, actor)
        db.commit()
    return decision


def resume_grant(db: Session, grant: EntitlementGrant, today: date | None = None, dry_run: bool | None = None, *,
                 actor: User | str = JOB_ACTOR) -> list[Decision]:
    """Clear a grant's hold (grant.resume) and recompute the person at once, so a grant the rule still supports is
    sent again. The re-grant carries :after:<the pause's revoke call id> in its key, so it is a new call and the
    product is asked again rather than matched to the row sent before the pause. A revoke still waiting in the
    outbox is dropped. The grant stays off while the person's Pause all access is on. Commits. Returns the
    recompute's decisions."""
    person = grant.person
    with advisory_lock(db, LOCK_PERSON, person.id):
        db.refresh(grant)
        if grant.held_at is not None:
            before = _grant_hold_fields(grant)
            grant.held_at, grant.held_by, grant.held_reason = None, "", ""
            dropped = _drop_waiting_revokes(db, grant)
            db.add(grant)
            audit(db, actor, "grant.resume", f"grant:{grant.id}", before=before,
                  after={**_grant_hold_fields(grant), "dropped_revokes": dropped or None})
        db.commit()
    return recompute_person(db, person, today, dry_run, actor=actor)


def pause_person(db: Session, person: Person, today: date | None = None, dry_run: bool | None = None, *,
                 actor: User | str = JOB_ACTOR, reason: str = "") -> dict:
    """Pause all access (the mentee screen): the person is marked held (person.pause) and every active grant they
    hold is revoked through its connector now. While the hold is on, the recompute keeps every grant of theirs
    off, including any a programme would create later. Unlike Revoke all, no mentee is ended, so Resume needs
    nothing else changed. The reason stays in Group HQ, as for pause_grant. Commits. Returns counts for the
    screen, for example {"revoked": 3, "already off": 1} (_hold_counts)."""
    today = today or utc_today()
    dry = effective_dry_run(dry_run)
    with advisory_lock(db, LOCK_PERSON, person.id), \
            open_connectors(dry_run=dry, switches=settings.product_switches(db)) as connectors:
        db.refresh(person)
        if person.access_held_at is None:
            before = _person_hold_fields(person)
            person.access_held_at = utcnow()
            person.access_held_by = _actor_label(actor)
            person.access_held_reason = (reason or "").strip()[:200]
            db.add(person)
            audit(db, actor, "person.pause", f"person:{person.id}", before=before, after=_person_hold_fields(person))
        decisions = [_apply_one(db, person, grant, _hold_target(grant), today, dry, connectors, actor)
                     for grant in db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id)
                     .order_by(EntitlementGrant.id).all()]
        db.commit()
    return _hold_counts(decisions)


def resume_person(db: Session, person: Person, today: date | None = None, dry_run: bool | None = None, *,
                  actor: User | str = JOB_ACTOR) -> list[Decision]:
    """Clear Pause all access (person.resume) and recompute the person at once; a grant paused on its own stays
    paused. Revokes still waiting in the outbox for the grants this releases are dropped. Commits. Returns the
    recompute's decisions."""
    with advisory_lock(db, LOCK_PERSON, person.id):
        db.refresh(person)
        if person.access_held_at is not None:
            before = _person_hold_fields(person)
            person.access_held_at, person.access_held_by, person.access_held_reason = None, "", ""
            released = (db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id,
                                                          EntitlementGrant.held_at.is_(None)).all())
            dropped = sum(_drop_waiting_revokes(db, grant) for grant in released)
            db.add(person)
            audit(db, actor, "person.resume", f"person:{person.id}", before=before,
                  after={**_person_hold_fields(person), "dropped_revokes": dropped or None})
        db.commit()
    return recompute_person(db, person, today, dry_run, actor=actor)
