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

Mentee earnings (alembic/versions/0008_mentee_earnings.py, docs/mentee-earnings.md). Inside one person's recompute
the order is: orphaned grants revoked; then, when the person has a Key Properties sourcer link and no Property
Predator account id yet, PP's account lookup (live PP only); then every PP grant; then the KP sourcer link, which
carries pp_account_id whenever Group HQ knows it (contract 4); then everything else in template order. The PP account
id is PP's users.id, a UUID string, kept and sent as text. An active sourcer link whose Key Properties row has not
confirmed the person's current PP account is sent a "link" call, PATCH {pp_account_id} and nothing else, keyed
grant:<id>:link:pp:<account id>, so a mentee linked before Group HQ knew the id gets it too. The link counts as done only
once Key Properties answers with that id: a 200 that does not carry it (a portal build without contract 4) is a failed
attempt (LINK_UNCONFIRMED), tried again on later runs up to MAX_ATTEMPTS. A 409 pp_account_mismatch from Key Properties
is a failed grant with MISMATCH_ERROR, never retried on its own (Retry on the grant once the ids are put right: a grant
that failed only on its link call goes back to active, so the same run sends the link again); it is still revoked when
access ends. pp_account_in_use (another Key Properties login already holds the PP account) is never retried on its own
either: a 409 on the POST (the portal changed nothing) fails the grant with IN_USE_CREATE_ERROR; a re-grant or
extension the portal applies without the link (200 with link_refused) and a refused link call leave the open sourcer
active with IN_USE_ERROR. Every such refusal is kept on the grant (remote_info.link_refused, with the id sent), so the
same id is not sent again until Retry (which marks it retried_at and keeps it shown until Key Properties answers), and
the grant pages and the earning links panel say what to put right in Key Properties (link_refusal_note); an answer
that carries the person's id removes it. An affiliate grant enrols in a public programme and its revoke is a recorded no-op; a 404 that is not the
product's own refusal (its route not deployed yet) is a failed call that does not use up an attempt (NOT_DEPLOYED). After the decisions, an active Hello New Home partner seat is looked
up (contract 3, live HNH only) and its area, links and admin page are kept on the grant (remote_info), which the
"Needs a region" queue reads; and a person with an active mentee row and no seller gets a Group HQ seller of kind
mentee (commission.ensure_mentee_seller), whose statements stay held until self-billing is agreed.
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
from . import commission, settings
from .audit import _jsonable, audit
from .connectors import (CONNECTORS, BaseConnector, ConnectorError, ConnectorResult, open_connectors, product_dry_run,
                         product_mode)
from .connectors.base import route_missing
from .connectors.kp import IN_USE_REASON, LINK_REFUSALS, MISMATCH_REASON
from .connectors.pp import AFFILIATE_KEPT, PP_TIERS, pp_tier

MAX_ATTEMPTS = 5
MAX_GRANT_DAYS = 400            # no grant or extension ever ends further away than this (a typo guard)
MAX_FULL_TERM_MONTHS = 13       # the longest term "paid in full" can be given, whatever the setting says
LOCK_PERSON = 1                 # pg_advisory_lock(LOCK_PERSON, person.id)
LOCK_RUN = 2                    # pg_try_advisory_lock(LOCK_RUN, 0): one nightly run at a time
RUN_IN_PROGRESS = "another recompute is already running; nothing was done"
BLOCKED_ERROR = "paid subscription"
MISMATCH_ERROR = ("Key Properties links this sourcer to a different Property Predator account (pp_account_mismatch). "
                  "Check the Property Predator account id on the person page against the one Key Properties holds, put "
                  "the wrong one right, then press Retry.")
LINK_UNCONFIRMED = ("Key Properties answered without the Property Predator account on the sourcer, so the link has not "
                    "taken (its build may not accept pp_account_id yet). It is tried again on the next runs; after "
                    f"{MAX_ATTEMPTS} tries press Retry.")
# pp_account_in_use: the PP account is already on another Key Properties login. What the founder does is the same
# whichever call met it; what the mentee has meanwhile is not, so there are two messages.
_IN_USE_FIX = ("In Key Properties, find the login that holds this Property Predator account: if it is the mentee's own "
               "second login, merge it into their sourcer login; if it is someone else's, take the account off it. Key "
               "Properties has no screen for either yet, so it is a change in its database. If the account id on the person "
               "page is wrong, correct it there instead. Then press Retry on the grant.")
IN_USE_ERROR = ("Key Properties already has this Property Predator account on another Key Properties login "
                "(pp_account_in_use), so the sourcer is open but not linked to it: deal packs they forward from Property "
                "Predator land on that other login. " + _IN_USE_FIX)
IN_USE_CREATE_ERROR = ("Key Properties already has this Property Predator account on another Key Properties login "
                       "(pp_account_in_use), so it changed nothing: no sourcer was created or opened for this email, and "
                       "any Key Properties login they already had is left as it was. " + _IN_USE_FIX)
# Every message for a link Key Properties refused: never retried on its own, cleared by Retry (reset_for_retry).
LINK_REFUSED_ERRORS = (MISMATCH_ERROR, IN_USE_ERROR, IN_USE_CREATE_ERROR)
RETRY_PRESSED = " Retry has been pressed since, so the next live run asks Key Properties again."
NOT_DEPLOYED = "{name} answered 404 without its own refusal, so its route is not deployed yet: not counted as an attempt, asked again on the next run. "
PROGRAMME_SOURCE = "programme:"
JOB_ACTOR = "job:recompute"
# The order of a person's targets (contract 4): PP first, so its answers record the account id, then the KP sourcer
# link that carries it, then everything else in template order.
TARGET_ORDER = {"pp": 0, "kp": 1}

ACTIONS = ("grant", "extend", "revoke", "noop", "blocked", "gave_up", "not_started", "held", "link")
KEPT_LABEL = "kept by a public programme"      # an affiliate grant's revoke: recorded here, nothing sent
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


def is_link_refused(grant: EntitlementGrant) -> bool:
    """Key Properties refused the sourcer link: its row is linked to another PP account (pp_account_mismatch), or another
    login already holds the PP account (pp_account_in_use) on a call that then changed nothing. A failed grant that the
    recompute leaves alone until Retry (it is still revoked when access ends). A sourcer left open without the link stays
    active and is not this: it shows the refusal through link_refusal_note()."""
    return grant.state == "failed" and grant.last_error in LINK_REFUSED_ERRORS


def link_refusal_note(grant: EntitlementGrant | None, person: Person | None = None) -> str:
    """The plain message for a Key Properties sourcer link whose Property Predator link the portal refused, for the grant
    pages and the earning links panel, or "" when there is none. A failed grant shows its last error while that is one of
    the refusal messages, since it waits for Retry whatever the person page now says. Otherwise the refusal kept on the
    grant (remote_info.link_refused) is shown only for the person's current PP account id: once the id is corrected on
    the person page, an open sourcer is sent the new id on the next run, so the old refusal is no longer shown. A
    refusal Retry has been pressed for (retried_at) is still shown, saying the next live run asks again, until Key
    Properties answers."""
    if grant is None or (grant.product, grant.kind) != ("kp", "sourcer_link") or grant.state not in ("pending", "active", "failed"):
        return ""
    refused = (grant.remote_info or {}).get("link_refused")
    refused = refused if isinstance(refused, dict) else None
    sent = refused.get("pp_account_id") if refused else None
    corrected = (person is not None and sent not in (None, "") and str(sent) != str(person.pp_account_id or ""))
    if grant.last_error in LINK_REFUSED_ERRORS and (grant.state == "failed" or not corrected):
        return grant.last_error
    if refused is None or corrected:
        return ""
    reason = str(refused.get("reason") or "")
    if reason == IN_USE_REASON:
        note = IN_USE_ERROR if refused.get("sourcer_open", True) else IN_USE_CREATE_ERROR
    elif reason == MISMATCH_REASON:
        note = MISMATCH_ERROR
    else:
        note = (f"Key Properties left the Property Predator link off ({reason or 'no reason given'}). Open the grant for "
                "what it said, put it right in Key Properties, then press Retry on the grant.")
    return note + (RETRY_PRESSED if refused.get("retried_at") else "")


def link_refusal_heading(note: str) -> str:
    """The bold first words of a link_refusal_note() on the earning links panel: a mismatch means the sourcer IS
    linked, only to another Property Predator account."""
    if not note:
        return ""
    return "Linked to a different Property Predator account." if note.startswith(MISMATCH_ERROR) else "Not linked to Property Predator."


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


def link_key(grant_id: int, pp_account_id: str) -> str:
    """The one call that tells Key Properties the PP account of an active sourcer link it has not confirmed."""
    return f"grant:{grant_id}:link:pp:{pp_account_id}"


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
        if a.get("link"):
            extra += f", {a['link']} Key Properties link{'s' if a['link'] != 1 else ''} to PP"
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
    if is_link_refused(grant):
        # Left alone until Retry, except that access the product may still give is taken away when it ends.
        if remote and (end is None or end <= today):
            enforced = ends_at if ends_at is not None and ends_at <= at_midnight(today) else at_midnight(today)
            grant.ends_at = enforced
            return "revoke", enforced
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


UNIQUE_LINKS = ("pp_account_id",)       # uq_people_pp_account_id: one PP account belongs to one person


def _apply_links(person: Person, links: dict, db: Session | None = None) -> dict:
    """Fill empty person ids from what the product told us; never overwrite a link already recorded, and never fill a
    PP account id another person already holds (the unique constraint would refuse the whole person's commit; the
    mentee import refuses it the same way and the founder decides which person it belongs to)."""
    filled = {}
    for field_name, value in (links or {}).items():
        if value in (None, "") or not hasattr(person, field_name):
            continue
        if getattr(person, field_name) in (None, ""):
            if db is not None and field_name in UNIQUE_LINKS:
                column = getattr(Person, field_name)
                if db.query(Person.id).filter(column == value, Person.id != person.id).first() is not None:
                    continue
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


def _merge_info(grant: EntitlementGrant, info: dict) -> None:
    """Keep what the product told us about the grant (ConnectorResult.info) on remote_info, key by key."""
    if info:
        grant.remote_info = _json({**(grant.remote_info or {}), **info})


def _keep_link_refusal(grant: EntitlementGrant, person: Person, reason: str, message: str = "") -> dict:
    """Record on the grant (remote_info.link_refused) that Key Properties refused to link the person's PP account: the
    reason, the portal's own message, the account id that was sent, when, and whether the sourcer is open (the grant is
    active) or the refused call changed nothing. _kp_link_due sends no link for that id again until Retry marks it;
    link_refusal_note() shows it. A fresh refusal replaces any earlier one, Retry mark included."""
    refused = {"reason": (reason or "")[:80], "message": (message or "")[:300] or None,
               "pp_account_id": person.pp_account_id, "at": utcnow().isoformat(timespec="seconds"),
               "sourcer_open": grant.state == "active"}
    grant.remote_info = _json({**(grant.remote_info or {}), "link_refused": refused})
    return refused


def _drop_link_refusal(grant: EntitlementGrant) -> None:
    if "link_refused" in (grant.remote_info or {}):
        grant.remote_info = _json({k: v for k, v in grant.remote_info.items() if k != "link_refused"})


def _mark_link_refusal_retried(grant: EntitlementGrant) -> None:
    """Retry was pressed on a grant whose link Key Properties refused: the refusal stays on record (and on the panel)
    until Key Properties answers again, but no longer stops the call (_kp_link_due), so the next live run asks again.
    A dry-run Retry, or one while Key Properties is in dry run or not configured, sends nothing and so keeps it shown."""
    refused = (grant.remote_info or {}).get("link_refused")
    if isinstance(refused, dict):
        grant.remote_info = _json({**grant.remote_info,
                                   "link_refused": {**refused, "retried_at": utcnow().isoformat(timespec="seconds")}})


def _execute(db: Session, grant: EntitlementGrant, person: Person, target: _Target, action: str,
             ends_at: datetime | None, *, dry_run: bool, connectors: dict[str, BaseConnector], actor,
             reason: str = "", key: str = "") -> Decision:
    """One decided call through the outbox. `key` is given for a "link" call (link_key); otherwise it is worked out
    from the action and the end."""
    key = key or (revoke_key(grant.id, ends_at, _regrant_since_revoke(db, grant)) if action == "revoke"
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
        if action == "link":
            return connector.link(grant, person)       # PATCH {pp_account_id} only: never re-opens or re-shares
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
        elif error.reason in LINK_REFUSALS and grant.product == "kp" and action != "revoke":
            # Key Properties refused the PP account: it holds another one on this sourcer (pp_account_mismatch), or
            # another login already holds this one (pp_account_in_use). Sending the same id again cannot help until the
            # founder puts it right in Key Properties, so it is never retried on its own: a plain message on the grant
            # (and the earning links panel) until Retry. A refused link call leaves an open sourcer active; any other
            # refused call changed nothing in the portal, so the grant is failed.
            if error.reason == MISMATCH_REASON:
                grant.state = "failed"
                grant.last_error = MISMATCH_ERROR
            elif action == "link":
                grant.last_error = IN_USE_ERROR
            else:
                grant.state = "failed"
                grant.last_error = IN_USE_CREATE_ERROR
            row.attempts = MAX_ATTEMPTS
            refused = _keep_link_refusal(grant, person, error.reason, error.detail)
            decision.outcome, decision.detail = "failed", grant.last_error
            audit(db, actor, "grant.fail", f"grant:{grant.id}", before=before,
                  after={**_snapshot(grant), "attempts": row.attempts, "action": action, "reason": error.reason,
                         "pp_account_id": person.pp_account_id, "link_refused": refused})
        else:
            grant.last_error = error.summary()[:1000]
            if grant.kind == "affiliate" and route_missing(error):
                # The product has not deployed its side of the contract yet: the mentee has done nothing wrong, so the
                # attempt is not counted and the grant never gives up on it; the next run asks again.
                row.attempts = max(0, row.attempts - 1)
                grant.last_error = (NOT_DEPLOYED.format(name=settings.product_name(grant.product)) + error.summary())[:1000]
            # A first grant that failed is failed, and so is anything that has used up its attempts. An
            # active grant whose extension or revoke failed stays active until the retry: the product
            # still holds the old window and the person still has access. A "link" call never changes the
            # state: the sourcer is active in Key Properties either way.
            if action != "link" and (row.attempts >= MAX_ATTEMPTS or grant.state != "active"):
                grant.state = "failed"
            decision.outcome, decision.detail = "failed", grant.last_error
            audit(db, actor, "grant.fail", f"grant:{grant.id}", before=before,
                  after={**_snapshot(grant), "attempts": row.attempts, "action": action})
        grant.last_sync_at = utcnow()
        db.add(grant)
        return decision

    if action == "link" and str((result.info or {}).get("pp_account_id") or "") != str(person.pp_account_id or ""):
        # Key Properties answered 200 but its row does not carry the id (a build without contract 4 drops the field):
        # the link has not taken, so it is a failed attempt, tried again on the next run until MAX_ATTEMPTS.
        row.status = "failed"
        row.response = _stored_response({**result.as_dict(), "unconfirmed": True})
        if result.plan:
            row.payload = _json({**row.payload, "request": result.plan})
        grant.last_error = LINK_UNCONFIRMED
        grant.last_sync_at = utcnow()
        db.add_all([row, grant])
        decision.outcome, decision.detail, decision.plan = "failed", LINK_UNCONFIRMED, result.plan
        audit(db, actor, "grant.fail", f"grant:{grant.id}", before=before,
              after={**_snapshot(grant), "attempts": row.attempts, "action": action, "reason": "unconfirmed"})
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
    info = dict(result.info or {})
    refused = info.pop("link_refused", None) if grant.product == "kp" else None
    _merge_info(grant, info)
    kept = None
    if isinstance(refused, dict):
        # Contract 4: the portal opened the sourcer (or set its share) but left the PP account off, because another
        # login already holds it. The access is in place; the link waits for the founder and Retry.
        kept = _keep_link_refusal(grant, person, str(refused.get("reason") or ""), str(refused.get("message") or ""))
        grant.last_error = link_refusal_note(grant, person)
    elif grant.product == "kp" and person.pp_account_id and str(info.get("pp_account_id") or "") == str(person.pp_account_id):
        _drop_link_refusal(grant)               # the portal now holds the person's account: any earlier refusal is over
    filled = _apply_links(person, result.links, db)
    db.add_all([row, grant, person])
    decision.outcome = "sent"
    decision.detail = result.noop or "sent"
    if kept is not None:
        decision.detail = f"sent without the Property Predator link: {grant.last_error}"
    decision.plan = result.plan
    decision.once = result.once
    audit(db, actor, f"grant.{action}", f"grant:{grant.id}", before=before,
          after={**_snapshot(grant), "noop": result.noop or None, "links": filled or None, **({"link_refused": kept} if kept else {})})
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
            lookup = _lookup_pp_account(db, person, targets, connectors, actor)
            if lookup is not None:
                decisions.append(lookup)
            hnh_grants: list[EntitlementGrant] = []
            for key, target in ordered_targets(targets):
                grant = _find_or_create_grant(db, person, key, target, reused.get(key))
                decisions.append(_apply_one(db, person, grant, target, today, dry, connectors, actor))
                if key[:2] == ("hnh", "partner_seat"):
                    hnh_grants.append(grant)
            for grant in hnh_grants:
                refresh_partner_info(db, grant, person, connectors)
            _ensure_seller(db, person, actor)
            db.commit()
            return decisions
    finally:
        if own_pool:
            pool_cm.__exit__(None, None, None)


def ordered_targets(targets: dict[tuple, _Target]) -> list[tuple[tuple, _Target]]:
    """The targets in the order a person's recompute sends them (TARGET_ORDER): every PP item, then Key Properties,
    then the rest; template order is kept within each group (the sort is stable)."""
    return sorted(targets.items(), key=lambda pair: TARGET_ORDER.get(pair[0][0], len(TARGET_ORDER)))


def _lookup_pp_account(db: Session, person: Person, targets: dict[tuple, _Target], connectors: dict[str, BaseConnector],
                       actor) -> Decision | None:
    """Before any grant is sent: when the person will be sent a Key Properties sourcer link and Group HQ does not know
    their PP account yet, ask PP (its account lookup, live and configured PP only) so the link can carry the id. The id
    fills the empty person field (audited as person.links). No account (404) is not a failure. Any other failure is
    reported as a skipped decision on the run page and the recompute goes on: the sourcer link is then sent without
    the id, as it would have been before, and the next run asks again."""
    if person.pp_account_id is not None or person.access_held_at is not None:
        return None
    if not any(key[:2] == ("kp", "sourcer_link") for key in targets):
        return None
    pp = connectors.get("pp")
    if pp is None or pp.dry_run or not pp.configured:
        return None
    try:
        result = pp.lookup(person.person_key)
    except ConnectorError as error:
        if error.status == 404:
            return None
        return Decision(person_id=person.id, person_key=person.person_key, grant_id=None, product="pp", kind="lookup",
                        value="account", action="noop", outcome="skipped",
                        detail=f"account lookup failed ({error.summary()[:200]}): Key Properties is sent no PP account id this time")
    filled = _apply_links(person, result.links, db)
    if filled:
        db.add(person)
        audit(db, actor, "person.links", f"person:{person.id}", before={k: None for k in filled},
              after={**filled, "via": "recompute lookup"})
    return None


def refresh_partner_info(db: Session, grant: EntitlementGrant, person: Person, connectors: dict[str, BaseConnector]) -> None:
    """After a person's decisions: an active, unheld Hello New Home partner seat is looked up (contract 3, live and
    configured HNH only) and what HNH answers is kept on the grant's remote_info with checked_at, which is what the
    "Needs a region" queue lists. A failed lookup keeps the last answer and records check_error; it never fails the
    recompute."""
    hnh = connectors.get("hnh")
    if grant.state != "active" or hold_of(grant, person) is not None:
        return
    if hnh is None or hnh.dry_run or not hnh.configured:
        return
    stamp = utcnow().isoformat(timespec="seconds")
    try:
        result = hnh.partner_status(person.person_key)
    except ConnectorError as error:
        grant.remote_info = _json({**(grant.remote_info or {}), "check_error": error.summary()[:300], "check_failed_at": stamp})
    except Exception as error:  # noqa: BLE001  (a display cache must never undo the grants decided above)
        grant.remote_info = _json({**(grant.remote_info or {}), "check_error": f"unexpected: {type(error).__name__}"[:300],
                                   "check_failed_at": stamp})
    else:
        kept = {k: v for k, v in (grant.remote_info or {}).items() if k not in PARTNER_INFO_KEYS}
        grant.remote_info = _json({**kept, **result.info, "checked_at": stamp, "check_error": None})
    db.add(grant)


# Everything refresh_partner_info replaces on each successful lookup (the connector's partner_info keys).
PARTNER_INFO_KEYS = ("found", "partner_id", "referral_code", "referral_link", "portal_url", "admin_url", "tier", "status",
                     "area", "area_known", "needs_region", "check_error", "check_failed_at")


def _ensure_seller(db: Session, person: Person, actor) -> None:
    """A person with an active mentee row and no seller gets one (kind mentee), so they hold a franchise referral code.
    Statements stay held until self-billing is agreed (the existing rule in app.services.commission)."""
    active = db.query(Mentee.id).filter(Mentee.person_id == person.id, Mentee.status == "active").first()
    if active is not None:
        commission.ensure_mentee_seller(db, person, actor=actor)


def _kp_link_due(db: Session, grant: EntitlementGrant, person: Person) -> str:
    """The link_key to send when an active Key Properties sourcer link has not had the person's current PP account id
    confirmed by Key Properties (remote_info.pp_account_id, compared as text), or "" when nothing is due: no id known,
    already confirmed, or that very call already went through or used up its attempts (an unconfirmed answer counts
    as a failed attempt, so it is tried again until then), or Key Properties has refused to link that very id
    (remote_info.link_refused, until Retry marks it retried_at: sending it again cannot help until the founder puts it
    right there)."""
    if (grant.product, grant.kind) != ("kp", "sourcer_link") or grant.state != "active" or not grant.remote_ref:
        return ""
    pp_account_id = person.pp_account_id
    confirmed = (grant.remote_info or {}).get("pp_account_id")
    if not pp_account_id or (confirmed is not None and str(confirmed) == str(pp_account_id)):
        return ""
    refused = (grant.remote_info or {}).get("link_refused")
    if (isinstance(refused, dict) and not refused.get("retried_at")
            and str(refused.get("pp_account_id") or "") == str(pp_account_id)):
        return ""
    key = link_key(grant.id, pp_account_id)
    row = db.query(ConnectorCall.status, ConnectorCall.attempts).filter(ConnectorCall.idempotency_key == key).first()
    if row is not None and (row.status == "succeeded" or row.attempts >= MAX_ATTEMPTS):
        return ""
    return key


def _apply_one(db: Session, person: Person, grant: EntitlementGrant, target: _Target, today: date, dry: bool,
               connectors: dict[str, BaseConnector], actor) -> Decision:
    hold = hold_of(grant, person)
    held = _hold_since(grant, person)
    remote = grant.state != "active" and (held is not None or is_link_refused(grant)) and _product_holds(db, grant)
    action, ends_at = _decide(grant, target, today, held, remote)
    db.add(grant)
    why = "paused" if hold and hold["scope"] == "grant" else "all access paused"
    if action == "held":
        return _skip(person, grant, action, ends_at, f"{why}: kept off until resumed", dry)
    # The revoke a hold enforces tells the product only that Group HQ paused it: the reason typed on the pause
    # form stays in Group HQ (the grant, the person and the audit rows), since products such as Hello New Home
    # belong to another company and keep what they are sent.
    reason = f"Group HQ: {why} (grant {grant.id})" if hold is not None and action == "revoke" else ""
    if action == "blocked" and is_link_refused(grant):
        return _skip(person, grant, action, ends_at, "Key Properties refused the Property Predator link: not retried automatically", dry)
    if action == "blocked":
        return _skip(person, grant, action, ends_at, BLOCKED_ERROR + ": not retried automatically", dry)
    if action == "not_started":
        return _skip(person, grant, action, ends_at, f"starts {target.starts.isoformat()}", dry)
    if action == "noop" and hold is None and grant.product in connectors:
        key = _kp_link_due(db, grant, person)
        if key:
            return _execute(db, grant, person, target, "link", grant.ends_at, dry_run=dry, connectors=connectors,
                            actor=actor, key=key)
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
            if decision.outcome == "sent" and decision.detail == AFFILIATE_KEPT:
                counts[KEPT_LABEL] += 1         # an affiliate: recorded here, the public programme keeps the member
                continue
            counts[{"sent": "revoked", "planned": "planned", "failed": "failed", "blocked": "blocked"}.get(decision.outcome, "unchanged")] += 1
        db.commit()
    return dict(counts)


def reset_for_retry(db: Session, grant: EntitlementGrant, *, actor: User | str) -> None:
    """Clear a failed or blocked grant so the next recompute tries it again. Caller commits. A Key Properties sourcer
    link that failed on its link call (pp_account_mismatch) goes back to active rather than pending when the portal
    still holds the sourcer: the link call is what is retried, and an active grant is what sends it (_kp_link_due).
    A link Key Properties refused (remote_info.link_refused) is marked retried_at rather than forgotten: the next live
    run asks again (the same run, unless it is a dry run or Key Properties is in dry run or not configured), so an open
    sourcer whose re-grant was applied without the link is sent the link call, while the refusal stays on record and
    on the panel until Key Properties answers: linked, it goes; refused again, it is recorded afresh."""
    before = _snapshot(grant)
    _mark_link_refusal_retried(grant)
    failed_link = False
    for row in db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id, ConnectorCall.status == "failed").all():
        failed_link = failed_link or row.action == "link"
        row.status = "pending"
        row.attempts = 0
        db.add(row)
    if grant.state == "failed":
        holds = failed_link and bool(grant.remote_ref) and _product_holds(db, grant)
        grant.state = "active" if holds else "pending"
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
        if d.outcome == "sent" and d.detail == AFFILIATE_KEPT:
            return KEPT_LABEL
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
