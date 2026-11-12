"""Today: everything waiting for the founder in one list, ordered by what matters, and the health of the parts
that feed it, in plain English.

Read only. Every item links to the screen where it is dealt with; nothing here changes a record. Dates are UK
dates: a next step "due" is one dated today or earlier in London. The inbox part is passed in by the router
(its conversation read is live and may fail on its own); everything else is read from this database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config
from ..models import (Acquisition, CampaignAction, CampaignLead, CampaignPlan, ConnectorCall, EntitlementGrant,
                      FranchiseOpportunity, LapsDeal, MetricSource, Mentee, Payment, Person, Task, utcnow)
from . import acquisitions as acquisitions_service, laps as laps_service, laps_sync

LONDON = ZoneInfo("Europe/London")
ROWS = 8                        # each group shows this many; its screen lists the rest
SOON_DAYS = 7                   # a monthly mentee paid up to within this many days is "due soon"
SYNC_STALE = timedelta(hours=1)
SOURCE_STALE = timedelta(days=2)
BRANDS = laps_service.BRANDS
CHANNEL_WORDS = {"call": "Call", "email": "Email", "sms": "Text", "whatsapp": "WhatsApp"}


@dataclass
class Item:
    title: str
    detail: str
    href: str
    when: date | None = None       # the date it is due, for "overdue" / "today" wording
    brand: str = ""

    def due_words(self, today: date) -> str:
        if self.when is None:
            return ""
        days = (today - self.when).days
        if days > 1:
            return f"{days} days overdue"
        return {1: "Due yesterday", 0: "Due today"}.get(days, "Due " + self.when.strftime("%-d %b"))

    def overdue(self, today: date) -> bool:
        return self.when is not None and self.when < today


@dataclass
class Group:
    key: str
    title: str
    href: str
    items: list[Item] = field(default_factory=list)
    total: int = 0
    empty: str = ""

    @property
    def more(self) -> int:
        return max(0, self.total - len(self.items))


@dataclass
class Health:
    level: str          # "bad" stops something working, "warn" will if left, "ok" is fine
    title: str
    why: str
    action: str = ""
    href: str = ""


def uk_today(now: datetime | None = None) -> date:
    return (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(LONDON).date()


def _end_of_uk_day(day: date) -> datetime:
    """The naive-UTC instant the UK day ends, for comparing with stored timestamps."""
    local = datetime(day.year, day.month, day.day, tzinfo=LONDON) + timedelta(days=1)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _brand(code: str) -> str:
    return BRANDS.get(code, code.upper() if code else "")


# ---- what needs you ------------------------------------------------------------------------------------------
def replies(inbox_threads: list[dict]) -> Group:
    """Conversations the inbox marks "needs me" (Claude's triage asked for the owner, or an automation is held)."""
    flagged = [t for t in inbox_threads if t.get("needs_owner")]
    items = []
    for thread in flagged[:ROWS]:
        account = thread["account"]
        items.append(Item(title=thread.get("name") or "Conversation",
                          detail=f"{account.label} · {(thread.get('preview') or '').strip()[:120]}",
                          href="/inbox?" + urlencode({"account": account.key, "thread": thread["ticket"]}),
                          brand=account.brand))
    return Group("replies", "Replies waiting for you", "/inbox?triage=handoff", items, len(flagged),
                 "Nothing in the inbox is waiting for you.")


def sales_steps(db: Session, today: date) -> Group:
    open_stages = laps_service.PIPELINE[:-1]
    query = (select(LapsDeal).where(LapsDeal.stage.in_(open_stages), LapsDeal.next_step_on.is_not(None),
                                    LapsDeal.next_step_on <= today)
             .order_by(LapsDeal.next_step_on, LapsDeal.id))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = [Item(title=d.name, detail=f"{laps_service.LABELS[d.stage][:-1]} · {d.next_step or 'Next step'}",
                  href=f"/laps/deals/{d.id}", when=d.next_step_on, brand=d.brand)
             for d in db.scalars(query.limit(ROWS))]
    return Group("sales", "Sales next steps due", "/laps", items, total, "No sales next step is due.")


def new_leads(db: Session) -> Group:
    """Leads with no next step set: the board cannot remind you about them until you give them one."""
    query = (select(LapsDeal).where(LapsDeal.stage == "lead", LapsDeal.next_step_on.is_(None))
             .order_by(LapsDeal.lead_at.desc(), LapsDeal.id.desc()))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = [Item(title=d.name, detail="New lead, no next step set yet" + (f" · from {d.source}" if d.source else ""),
                  href=f"/laps/deals/{d.id}", brand=d.brand)
             for d in db.scalars(query.limit(ROWS))]
    return Group("leads", "New leads to contact", "/laps", items, total, "Every lead has a next step.")


def property_steps(db: Session, today: date) -> Group:
    query = (select(Acquisition).where(Acquisition.stage.in_(acquisitions_service.OPEN),
                                       Acquisition.next_step_on.is_not(None), Acquisition.next_step_on <= today)
             .order_by(Acquisition.next_step_on, Acquisition.id))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = [Item(title=a.title, detail=f"{acquisitions_service.LABELS.get(a.stage, a.stage)} · {a.next_step or 'Next step'}",
                  href=f"/acquisitions/deals/{a.id}", when=a.next_step_on)
             for a in db.scalars(query.limit(ROWS))]
    return Group("property", "Property deal next steps due", "/acquisitions", items, total,
                 "No property deal next step is due.")


def crm_actions(db: Session, today: date) -> Group:
    """Open steps of active follow-up plans due by the end of today (calls first, then the rest by time)."""
    query = (select(CampaignAction, CampaignLead).join(CampaignPlan, CampaignAction.plan_id == CampaignPlan.id)
             .join(CampaignLead, CampaignPlan.lead_id == CampaignLead.id)
             .where(CampaignPlan.state == "active", CampaignAction.state == "open",
                    CampaignAction.due_at < _end_of_uk_day(today))
             .order_by(CampaignAction.due_at, CampaignAction.id))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = []
    for action, lead in db.execute(query.limit(ROWS)).all():
        due = action.due_at.replace(tzinfo=timezone.utc).astimezone(LONDON).date()
        items.append(Item(title=lead.display_name, detail=f"{CHANNEL_WORDS.get(action.channel, action.channel)} · {action.title}",
                          href=f"/crm/leads/{lead.id}", when=due, brand=lead.brand))
    return Group("crm", "Follow-up steps due", "/crm", items, total, "No follow-up step is due.")


def franchise_tasks(db: Session, today: date) -> Group:
    query = (select(Task, FranchiseOpportunity, Person).join(FranchiseOpportunity, Task.opportunity_id == FranchiseOpportunity.id)
             .join(Person, FranchiseOpportunity.person_id == Person.id)
             .where(Task.state == "open", Task.due_on <= today).order_by(Task.due_on, Task.id))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = [Item(title=person.name or person.person_key, detail=task.title, href=f"/franchise/{opp.id}", when=task.due_on)
             for task, opp, person in db.execute(query.limit(ROWS)).all()]
    return Group("franchise", "Franchise tasks due", "/franchise", items, total, "No franchise task is due.")


def mentee_money(db: Session, today: date) -> Group:
    """Monthly mentees whose cleared payments run out within SOON_DAYS (or already have), and payments recorded
    but not yet cleared (they give no access until they clear)."""
    items: list[Item] = []
    paid_to = (select(Payment.mentee_id, func.max(Payment.period_end).label("paid_to"))
               .where(Payment.cleared_at.is_not(None), Payment.period_end.is_not(None))
               .group_by(Payment.mentee_id).subquery())
    rows = db.execute(select(Mentee, Person, paid_to.c.paid_to)
                      .join(Person, Mentee.person_id == Person.id)
                      .outerjoin(paid_to, paid_to.c.mentee_id == Mentee.id)
                      .where(Mentee.status == "active", Mentee.payment_mode == "monthly")
                      .order_by(paid_to.c.paid_to.nulls_first(), Mentee.id)).all()
    for mentee, person, until in rows:
        if until is None:
            items.append(Item(title=person.name or person.person_key, detail="Monthly mentee with no cleared payment yet",
                              href=f"/mentees/{mentee.id}", when=mentee.start_date))
        elif until <= today + timedelta(days=SOON_DAYS):
            items.append(Item(title=person.name or person.person_key,
                              detail=f"Paid up to {until.strftime('%-d %b')}; next payment due",
                              href=f"/mentees/{mentee.id}", when=until + timedelta(days=1)))
    pending = db.execute(select(Payment, Mentee, Person).join(Mentee, Payment.mentee_id == Mentee.id)
                         .join(Person, Mentee.person_id == Person.id)
                         .where(Payment.cleared_at.is_(None)).order_by(Payment.paid_at, Payment.id)).all()
    for payment, mentee, person in pending:
        items.append(Item(title=person.name or person.person_key,
                          detail=f"£{payment.amount:,.2f} recorded {payment.paid_at.strftime('%-d %b')}, not cleared yet",
                          href=f"/mentees/{mentee.id}"))
    return Group("mentees", "Mentee payments", "/mentees", items[:ROWS], len(items), "No mentee payment needs you.")


def regions(region_rows: list[dict]) -> Group:
    items = [Item(title=r["person"].name or r["person"].person_key, detail="Hello New Home partner with no area yet",
                  href=r["admin_url"] or (f"/mentees/{r['mentee'].id}" if r["mentee"] else f"/people/{r['person'].id}"))
             for r in region_rows[:ROWS]]
    return Group("regions", "Needs a region", "/mentees?region=needed", items, len(region_rows),
                 "Every Hello New Home partner has an area.")


# ---- health ------------------------------------------------------------------------------------------------
def health(db: Session, inbox_results: list[dict], inbox_error: str | None, scanning: bool | None,
           now: datetime | None = None) -> list[Health]:
    """Problems first, each with why it matters and the next useful step. An empty problem list ends with one
    "all fine" line so the screen never looks unfinished."""
    now = now or utcnow()
    out: list[Health] = []
    if inbox_error:
        out.append(Health("bad", "The inbox could not be read", inbox_error + " Replies waiting cannot be counted until it can.",
                          "Open the inbox", "/inbox"))
    for result in inbox_results:
        if result.get("error"):
            account = result["account"]
            out.append(Health("bad", f"{account.label} is not connected",
                              f"Messages to {_brand(account.brand)} on {account.channel.title()} are not reaching Growth HQ ({result['error'].rstrip('.')}).",
                              "Check the connection", "/settings"))
    if scanning is False:
        out.append(Health("warn", "Inbox sorting is paused",
                          "New conversations are not sorted, so nothing is flagged as needing you.", "Turn it back on", "/inbox"))
    if not config.R72_GROUP_HQ_INBOX_SECRET:
        out.append(Health("warn", "The business CRMs are not linked",
                          "Leads cannot be pulled onto the LAPS board and conversations cannot be added to a CRM.",
                          "See settings", "/settings"))
    sync = laps_sync.state(db)
    if config.R72_GROUP_HQ_INBOX_SECRET:
        when = sync.get("when")
        if when is None:
            out.append(Health("warn", "The LAPS board has not pulled leads yet",
                              "New leads from the business CRMs only appear after a refresh.", "Refresh the board", "/laps"))
        elif now - when > SYNC_STALE:
            hours = int((now - when).total_seconds() // 3600)
            out.append(Health("warn", "The LAPS board is out of date",
                              f"It last pulled leads {hours} hour{'s' if hours != 1 else ''} ago; it normally refreshes every few minutes while you use it.",
                              "Refresh the board", "/laps"))
        if sync.get("problem"):
            out.append(Health("bad", "No business CRM answered the last refresh", sync["problem"], "Refresh the board", "/laps"))
        for code, outcome in (sync.get("brands") or {}).items():
            if outcome.get("via") is None and outcome.get("problem") and not sync.get("problem"):
                out.append(Health("warn", f"{_brand(code)}'s CRM did not answer", outcome["problem"], "Refresh the board", "/laps"))
    failed_grants = db.scalar(select(func.count()).select_from(EntitlementGrant).where(EntitlementGrant.state == "failed")) or 0
    if failed_grants:
        out.append(Health("bad", f"{failed_grants} access grant{'s' if failed_grants != 1 else ''} failed",
                          "Someone who has paid may not have their product access.", "See the failed grants", "/grants?state=failed"))
    failed_calls = db.scalar(select(func.count()).select_from(ConnectorCall).where(ConnectorCall.status == "failed")) or 0
    if failed_calls:
        out.append(Health("warn", f"{failed_calls} product call{'s' if failed_calls != 1 else ''} failed",
                          "A change Growth HQ asked another product to make did not happen.", "See the audit log", "/audit"))
    for source in db.scalars(select(MetricSource).where(MetricSource.status == "failing").order_by(MetricSource.source)):
        out.append(Health("warn", f"Growth figures from {_source_name(source.source)} are failing",
                          f"The Growth screens show older numbers for it. Last error: {source.last_error or 'not recorded'}.",
                          "See Growth sources", "/growth"))
    stale_sources = [s for s in db.scalars(select(MetricSource).where(MetricSource.status == "ok", MetricSource.last_ok_at.is_not(None)))
                     if now - s.last_ok_at > SOURCE_STALE]
    if stale_sources:
        names = ", ".join(_source_name(s.source) for s in stale_sources[:4])
        out.append(Health("warn", "Some Growth figures are more than two days old",
                          f"{names} {'has' if len(stale_sources) == 1 else 'have'} not refreshed since the daily run.", "See Growth sources", "/growth"))
    order = {"bad": 0, "warn": 1, "ok": 2}
    out.sort(key=lambda h: order[h.level])
    if not out:
        out.append(Health("ok", "Everything is connected", "Every inbox, CRM link and scheduled job that Growth HQ watches looks healthy."))
    return out


def _source_name(source: str) -> str:
    try:
        from .growth.collectors import SOURCE_INFO
        return SOURCE_INFO.get(source, {}).get("name", source)
    except Exception:  # noqa: BLE001  (a missing description never breaks the health list)
        return source


def build(db: Session, inbox_threads: list[dict], inbox_results: list[dict], inbox_error: str | None,
          scanning: bool | None, region_rows: list[dict], now: datetime | None = None) -> dict:
    today = uk_today(now)
    groups = [replies(inbox_threads), sales_steps(db, today), new_leads(db), crm_actions(db, today),
              property_steps(db, today), mentee_money(db, today), franchise_tasks(db, today), regions(region_rows)]
    waiting = [g for g in groups if g.total]
    return {"today": today, "waiting": waiting, "clear": [g for g in groups if not g.total],
            "total": sum(g.total for g in waiting), "health": health(db, inbox_results, inbox_error, scanning, now)}
