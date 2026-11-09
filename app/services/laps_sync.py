"""Fill the LAPS board from the leads Group HQ already holds, so nobody types a lead in twice.

Two sources feed it:

- CRM leads, for every business with a CRM workspace (CRM_BRANDS): the CRM's signed `leads` listing
  (app.services.inbox_crm), every page, projected into campaign_leads and then onto the board. A CRM release
  from before that listing existed refuses it; Property Predator then falls back to the older read bridge
  (app.services.relaunch72_crm) and the other businesses simply show as not answering yet.
- The franchise pipeline: every territory opportunity in this database (Hello New Home).

A deal made here carries origin ("crm:<campaign lead id>", "franchise:<opportunity id>") and is made once. After
that the source can only move it FORWARD or mark it lost: the owner's own moves, notes, values and next steps on
the board are never overwritten, and a deal the owner marked lost stays lost. Mentee enrolments are not copied
in: their money is already in the Money panel, and a sale with no lead, appointment or presentation behind it
would make every conversion rate look better than it is.

Dates. The listing gives the day a contact was created and the day its deal last changed stage, and those are
used. The older bridge gives neither, so a lead that arrives through it is dated by its last CRM activity (or
by now when it has none) and its later stage changes by the moment the refresh saw them. Franchise
opportunities carry their own dates.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..models import AppSetting, CampaignLead, FranchiseOpportunity, LapsDeal, User, utcnow
from . import campaigns, inbox_crm, laps, relaunch72_crm
from .audit import audit

LOG = logging.getLogger(__name__)
SYNC_KEY = "laps.sync"
STALE = timedelta(minutes=10)
MAX_PAGES = 40                      # a refresh reads at most this many pages of one business's CRM
CRM_BRANDS = ("pp", "mhp", "kp", "piuk")        # the businesses with a CRM workspace of their own
LOCK = 741935840
ACTOR = "system:laps-sync"

# A CRM stage is whatever the workspace named it. These words place it on the board; anything else is a lead.
PRESENTATION = re.compile(r"proposal|present|pitch|quote|offer|pack sent|demo", re.I)
APPOINTMENT = re.compile(r"qualif|book|appointment|meeting|discovery|diary|scheduled|call", re.I)
FRANCHISE_STAGES = {"enquiry": "lead", "discovery": "appointment", "pack_sent": "presentation",
                    "follow_up": "presentation", "agreement": "presentation", "paid": "sale", "onboarding": "sale",
                    "lost": "lost"}


def crm_stage(lifecycle: str, stage_name: str | None, opportunity_status: str | None = None) -> str:
    """Where a CRM contact sits on the board: a customer or a won deal is a sale, an archived contact or a lost
    deal is lost, and an open one follows the name of its pipeline stage."""
    if lifecycle == "customer" or opportunity_status == "won":
        return "sale"
    if lifecycle == "archived" or opportunity_status == "lost":
        return "lost"
    name = stage_name or ""
    if PRESENTATION.search(name):
        return "presentation"
    if APPOINTMENT.search(name):
        return "appointment"
    return "lead"


def _when(raw: str | None, fallback: datetime, now: datetime) -> datetime:
    """An ISO time from the bridge as naive UTC, never in the future; fallback when absent or unreadable."""
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return min(parsed, now)
        except ValueError:
            pass
    return min(fallback, now)


def place(db: Session, existing: dict[str, LapsDeal], origin: str, *, stage: str, first_seen: datetime,
          now: datetime, stage_at: datetime | None = None, **fields) -> str | None:
    """Create the deal for origin, or move it forward. stage_at is when the source says the stage was reached,
    when it knows. Returns "created", "moved" or None."""
    deal = existing.get(origin)
    if deal is None:
        if stage == "lost":
            return None                             # never bring in a lead that is already dead
        deal = LapsDeal(origin=origin, stage="lead", lead_at=first_seen, **fields)
        if stage != "lead":
            laps.move(deal, stage, max(stage_at or first_seen, first_seen))
        db.add(deal)
        existing[origin] = deal
        return "created"
    if deal.value_pence is None and fields.get("value_pence") is not None:
        deal.value_pence = fields["value_pence"]
    if deal.stage in ("lost", "sale") or stage == deal.stage:
        return None
    if stage == "lost" or laps.PIPELINE.index(stage) > laps.PIPELINE.index(deal.stage):
        laps.move(deal, stage, max(stage_at, deal.lead_at) if stage_at else now)
        return "moved"
    return None                                     # the source is behind the board: the owner's move stands


def from_franchise(db: Session, existing: dict[str, LapsDeal], now: datetime) -> dict:
    counts = {"created": 0, "moved": 0}
    rows = db.scalars(select(FranchiseOpportunity).options(selectinload(FranchiseOpportunity.person))).all()
    for row in rows:
        districts = ", ".join(str(d) for d in (row.districts or [])[:6])
        outcome = place(
            db, existing, f"franchise:{row.id}", stage=FRANCHISE_STAGES[row.stage], now=now,
            first_seen=min(row.created_at, now) if FRANCHISE_STAGES[row.stage] == "lead" else min(row.stage_changed_at, now),
            name=(row.person.name or row.person.person_key)[:200], brand="hnh",
            offer=("Territory: " + districts if districts else "Territory")[:120],
            source=(row.source or "Franchise pipeline")[:100], value_pence=row.quoted_upfront_pence)
        if outcome:
            counts[outcome] += 1
    return counts


def from_crm_leads(db: Session, existing: dict[str, LapsDeal], now: datetime) -> dict:
    counts = {"created": 0, "moved": 0}
    for lead in db.scalars(select(CampaignLead).where(CampaignLead.brand.in_(laps.BRANDS))).all():
        first_seen = _when(lead.source_created_at or lead.last_activity_at, lead.created_at, now)
        outcome = place(
            db, existing, f"crm:{lead.id}", stage=crm_stage(lead.lifecycle, lead.stage, lead.opportunity_status),
            now=now, first_seen=first_seen,
            stage_at=_when(lead.stage_changed_at, first_seen, now) if lead.stage_changed_at else None,
            name=lead.display_name[:200], brand=lead.brand, source=(lead.source or "CRM")[:100],
            value_pence=lead.value_minor or None)
        if outcome:
            counts[outcome] += 1
    return counts


def _pull_bridge(db: Session, actor) -> dict:
    """Property Predator through the older read bridge, 50 contacts a page."""
    checked, cursor = 0, None
    for _ in range(MAX_PAGES):
        try:
            page = relaunch72_crm.fetch_leads(cursor)
        except relaunch72_crm.CrmUnavailable:
            db.rollback()
            return {"checked": checked, "via": "bridge" if checked else None, "problem": "The CRM did not answer."}
        campaigns.project_pp_page(db, page, actor)
        db.commit()
        checked += len(page.leads)
        cursor = page.next_cursor
        if not cursor:
            return {"checked": checked, "via": "bridge", "problem": ""}
    return {"checked": checked, "via": "bridge", "problem": "More contacts than one refresh reads; the rest wait for the next."}


def pull_brand(db: Session, brand: str, asker: User | None, actor) -> dict:
    """Read every page of one business's CRM into campaign_leads. asker is the owner the CRM authorises; actor
    is who the audit rows name. Returns {"checked", "via", "problem"}."""
    checked, cursor = 0, None
    if asker is None:                       # the bridge needs no person; the listing does
        return _pull_bridge(db, actor) if brand == "pp" else {
            "checked": 0, "via": None, "problem": "No owner account to ask the CRM with."}
    for _ in range(MAX_PAGES):
        try:
            page = inbox_crm.command(asker, brand, "leads", **({"cursor": cursor} if cursor else {}))
        except inbox_crm.CrmUnavailable:
            db.rollback()
            if checked == 0 and brand == "pp":
                return _pull_bridge(db, actor)       # a CRM release from before the listing still has the bridge
            return {"checked": checked, "via": "listing" if checked else None,
                    "problem": "The CRM did not answer for this business."}
        campaigns.project_leads(db, brand, page["leads"], actor)
        db.commit()
        checked += len(page["leads"])
        cursor = page["next_cursor"]
        if not cursor:
            return {"checked": checked, "via": "listing", "problem": ""}
    return {"checked": checked, "via": "listing", "problem": "More contacts than one refresh reads; the rest wait for the next."}


def owner(db: Session) -> User | None:
    """The account whose email the CRM's brand scopes name: the inbox owner."""
    email = (config.GHQ_PERSONAL_INBOX_OWNER_EMAIL or "").strip().lower()
    return db.scalars(select(User).where(func.lower(User.email) == email)).first() if email else None


def _origins(db: Session) -> dict[str, LapsDeal]:
    return {d.origin: d for d in db.scalars(select(LapsDeal).where(LapsDeal.origin.is_not(None))).all()}


def refresh_local(db: Session, actor=ACTOR) -> dict:
    """The part that needs no network: the franchise pipeline and the CRM leads already in this database."""
    now = utcnow()
    existing = _origins(db)
    franchise, crm = from_franchise(db, existing, now), from_crm_leads(db, existing, now)
    totals = {"created": franchise["created"] + crm["created"], "moved": franchise["moved"] + crm["moved"]}
    if totals["created"] or totals["moved"]:
        audit(db, actor, "laps.sync", "laps", after={"franchise": franchise, "crm": crm})
    db.commit()
    return totals


def refresh(db: Session, actor=ACTOR) -> dict:
    """The whole refresh: pull every business's CRM, then place everything. Records what happened for the board
    to show. `problem` is set only when no CRM answered at all; each business's own outcome is under `brands`."""
    asker = actor if isinstance(actor, User) else owner(db)
    brands = {brand: pull_brand(db, brand, asker, actor) for brand in CRM_BRANDS}
    totals = refresh_local(db, actor)
    answered = [b for b in brands.values() if b["via"]]
    value = {"at": utcnow().isoformat(timespec="seconds"), "brands": brands,
             "checked": sum(b["checked"] for b in brands.values()),
             "problem": "" if answered else "No CRM answered; leads already brought in are unchanged.", **totals}
    row = db.get(AppSetting, SYNC_KEY)
    if row:
        row.value, row.updated_by = value, str(getattr(actor, "email", actor))[:200]
    else:
        db.add(AppSetting(key=SYNC_KEY, value=value, updated_by=str(getattr(actor, "email", actor))[:200]))
    db.commit()
    return value


def state(db: Session) -> dict:
    row = db.get(AppSetting, SYNC_KEY)
    value = dict(row.value) if row and isinstance(row.value, dict) else {}
    try:
        value["when"] = datetime.fromisoformat(value["at"]) if value.get("at") else None
    except ValueError:
        value["when"] = None
    return value


def due(db: Session, now: datetime | None = None) -> bool:
    when = state(db).get("when")
    return when is None or (now or utcnow()) - when >= STALE


def run_background() -> None:
    """A refresh after the response has gone, on its own session. One at a time across processes: a second
    caller finds the lock taken and leaves."""
    from ..db import SessionLocal, engine
    try:
        with engine.connect() as lock, lock.begin():
            if not lock.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK}):
                return
            with SessionLocal() as db:
                if due(db):
                    refresh(db)
    except Exception as error:      # noqa: BLE001  (a failed refresh must never surface in someone's page load)
        LOG.warning("LAPS refresh deferred (%s)", type(error).__name__)
