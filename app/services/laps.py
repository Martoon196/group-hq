"""The LAPS board: Leads, Appointments, Presentations, Sales (the sales rhythm Daniel Priestley describes in
Oversubscribed).

A lead is someone who has asked to talk. An appointment is a time in the diary. A presentation is the offer
actually made. A sale is money taken. Every deal carries the moment it first reached each stage, so:

- the scoreboard counts what happened inside a period (five appointments booked this week), whatever stage
  those deals are at today;
- the conversion rates follow one cohort (the leads that arrived in the last 90 days) through the stages, so
  a rate never exceeds 100%;
- the plan works backwards from an income goal to the sales, presentations, appointments and leads it needs.

Weeks run Monday to Sunday and months by the calendar, both in UK time; timestamps are naive UTC as everywhere
else in this database. Money is whole pence.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (LAPS_BRANDS, AppSetting, FranchisePayment, LapsDeal, Payment, ProductPurchase, utcnow)

LONDON = ZoneInfo("Europe/London")
PIPELINE = ("lead", "appointment", "presentation", "sale")       # in order; "lost" sits outside it
LABELS = {"lead": "Leads", "appointment": "Appointments", "presentation": "Presentations", "sale": "Sales",
          "lost": "Lost"}
BRANDS = {"mhp": "Martin Howard Property", "pp": "Property Predator", "kp": "Key Properties UK",
          "piuk": "Prop Invest UK", "hnh": "Hello New Home", "ordris": "Ordris"}
assert tuple(BRANDS) == LAPS_BRANDS
TARGETS_KEY = "laps.targets"
COHORT_DAYS = 90
MIN_COHORT = 5                  # fewer deals than this at a stage says nothing about its conversion rate
MAX_PENCE = 2_000_000_000       # fits the integer column; £20m is beyond any single sale here
WEEKS_PER_MONTH = Decimal(52) / Decimal(12)


def parse_pounds(raw: str, limit: int = MAX_PENCE) -> int | None:
    """'1,250', '£1250.50' or '' -> pence or None. Raises ValueError with a sentence the form can show."""
    cleaned = re.sub(r"[£,\s]", "", raw or "")
    if not cleaned:
        return None
    try:
        pounds = Decimal(cleaned)
    except InvalidOperation:
        raise ValueError("Enter the amount in pounds, for example 1500 or 1500.00.") from None
    if not pounds.is_finite() or pounds < 0 or pounds != pounds.quantize(Decimal("0.01")):
        raise ValueError("Enter the amount in pounds, for example 1500 or 1500.00.")
    pence = int(pounds * 100)
    if pence > limit:
        raise ValueError("That amount is too large.")
    return pence


def _stamp(stage: str) -> str:
    return stage + "_at"


def furthest(deal, pipeline: tuple[str, ...] = PIPELINE) -> str:
    """The furthest pipeline stage this deal ever reached (what a lost deal reopens at)."""
    reached = [s for s in pipeline if getattr(deal, _stamp(s)) is not None]
    return reached[-1] if reached else pipeline[0]


def move(deal, target: str, now: datetime | None = None, pipeline: tuple[str, ...] = PIPELINE) -> None:
    """Move a deal to target along pipeline (the LAPS stages unless another board passes its own; any row with
    stage, lost_at and one <stage>_at column per stage works). Forward stamps every stage on the way that has no
    stamp yet; backward (a correction) clears the stages after target; lost keeps what was earned; reopening a
    lost deal puts it back at the furthest stage it reached. A deal at the final stage cannot be lost."""
    now = now or utcnow()
    if target == "lost":
        if deal.stage == pipeline[-1]:
            raise ValueError("A finished deal cannot be marked lost. Move it back a stage first if it fell through.")
        deal.stage, deal.lost_at = "lost", deal.lost_at or now
        return
    if target == "reopen":
        target = furthest(deal, pipeline)
    if target not in pipeline:
        raise ValueError("Unknown stage")
    index = pipeline.index(target)
    for stage in pipeline[:index + 1]:
        if getattr(deal, _stamp(stage)) is None:
            setattr(deal, _stamp(stage), now)
    for stage in pipeline[index + 1:]:
        setattr(deal, _stamp(stage), None)
    deal.stage, deal.lost_at = target, None


def reached_in(db: Session, model, pipeline: tuple[str, ...], start: datetime, end: datetime, where=None) -> dict:
    """How many rows of model reached each stage of pipeline inside [start, end)."""
    result = {}
    for stage in pipeline:
        column = getattr(model, _stamp(stage))
        query = select(func.count()).where(column >= start, column < end)
        result[stage] = db.scalar(query.where(where) if where is not None else query) or 0
    return result


def _utc(local: datetime) -> datetime:
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def periods(now: datetime | None = None) -> dict[str, tuple[datetime, datetime]]:
    """Named [start, end) windows in naive UTC, cut on UK midnights."""
    today = (now or utcnow()).replace(tzinfo=timezone.utc).astimezone(LONDON).date()
    monday = today - timedelta(days=today.weekday())
    month = today.replace(day=1)
    last_month = (month - timedelta(days=1)).replace(day=1)

    def at(day: date) -> datetime:
        return _utc(datetime(day.year, day.month, day.day, tzinfo=LONDON))
    return {
        "this_week": (at(monday), at(monday + timedelta(days=7))),
        "last_week": (at(monday - timedelta(days=7)), at(monday)),
        "this_month": (at(month), at((month + timedelta(days=32)).replace(day=1))),
        "last_month": (at(last_month), at(month)),
        "cohort": (at(today - timedelta(days=COHORT_DAYS - 1)), at(today + timedelta(days=1))),
    }


def week_starts(now: datetime | None = None, weeks: int = 8) -> list[tuple[date, datetime, datetime]]:
    """The last `weeks` Monday-to-Sunday weeks, oldest first, as (monday, start, end)."""
    start, _ = periods(now)["this_week"]
    monday = start.replace(tzinfo=timezone.utc).astimezone(LONDON).date()
    result = []
    for back in range(weeks - 1, -1, -1):
        day = monday - timedelta(days=7 * back)
        begin = _utc(datetime(day.year, day.month, day.day, tzinfo=LONDON))
        nxt = day + timedelta(days=7)
        result.append((day, begin, _utc(datetime(nxt.year, nxt.month, nxt.day, tzinfo=LONDON))))
    return result


def _brand(query, brand: str | None):
    return query.where(LapsDeal.brand == brand) if brand else query


def counts(db: Session, start: datetime, end: datetime, brand: str | None = None) -> dict:
    """How many deals reached each stage inside [start, end), and the value of the sales made in it."""
    result = reached_in(db, LapsDeal, PIPELINE, start, end, LapsDeal.brand == brand if brand else None)
    result["income_pence"] = int(db.scalar(_brand(
        select(func.coalesce(func.sum(LapsDeal.value_pence), 0))
        .where(LapsDeal.sale_at >= start, LapsDeal.sale_at < end), brand)) or 0)
    return result


def cohort(db: Session, now: datetime | None = None, brand: str | None = None) -> dict:
    """The leads that arrived in the last COHORT_DAYS days and how far they have got. rates[stage] is the share
    of the stage before that reached this one, or None while there are too few to say."""
    start, end = periods(now)["cohort"]
    base = _brand(select(func.count()).where(LapsDeal.lead_at >= start, LapsDeal.lead_at < end), brand)
    reached = {"lead": db.scalar(base) or 0}
    for stage in PIPELINE[1:]:
        reached[stage] = db.scalar(base.where(getattr(LapsDeal, _stamp(stage)).is_not(None))) or 0
    rates = {}
    for before, stage in zip(PIPELINE, PIPELINE[1:]):
        rates[stage] = reached[stage] / reached[before] if reached[before] >= MIN_COHORT else None
    sold = _brand(select(func.count(), func.coalesce(func.sum(LapsDeal.value_pence), 0))
                  .where(LapsDeal.sale_at >= start, LapsDeal.sale_at < end, LapsDeal.value_pence.is_not(None)), brand)
    n, total = db.execute(sold).one()
    return {"reached": reached, "rates": rates, "average_sale_pence": int(total) // n if n else None, "days": COHORT_DAYS}


def targets(db: Session) -> dict:
    row = db.get(AppSetting, TARGETS_KEY)
    value = row.value if row and isinstance(row.value, dict) else {}
    return {"monthly_goal_pence": value.get("monthly_goal_pence"), "typical_sale_pence": value.get("typical_sale_pence")}


def save_targets(db: Session, goal_pence: int | None, typical_pence: int | None, actor_email: str) -> dict:
    value = {"monthly_goal_pence": goal_pence, "typical_sale_pence": typical_pence}
    row = db.get(AppSetting, TARGETS_KEY)
    if row:
        row.value, row.updated_by = value, actor_email[:200]
    else:
        db.add(AppSetting(key=TARGETS_KEY, value=value, updated_by=actor_email[:200]))
    return value


def plan(goal_pence: int | None, typical_pence: int | None, stats: dict) -> dict | None:
    """Work backwards from the monthly income goal. The sale value is the owner's typical sale when given,
    otherwise the average of the recent sales. A stage whose conversion rate is not known yet (or is zero)
    stops the chain there: the stages before it are None and `missing` names the stage whose rate is unknown
    ("value" when there is no sale value to divide the goal by)."""
    if not goal_pence:
        return None
    sale_pence = typical_pence or stats["average_sale_pence"]
    if not sale_pence:
        return {"goal_pence": goal_pence, "sale_pence": None, "month": {}, "week": {}, "missing": "value"}
    month = {"sale": math.ceil(Decimal(goal_pence) / Decimal(sale_pence))}
    missing = None
    for before, stage in zip(reversed(PIPELINE[:-1]), reversed(PIPELINE[1:])):
        rate = stats["rates"].get(stage)
        if missing or not rate:
            missing = missing or stage
            month[before] = None
        else:
            month[before] = math.ceil(month[stage] / rate - 1e-9)
    week = {s: (math.ceil(Decimal(n) / WEEKS_PER_MONTH) if n is not None else None) for s, n in month.items()}
    return {"goal_pence": goal_pence, "sale_pence": sale_pence, "month": month, "week": week, "missing": missing,
            "from_typical": bool(typical_pence)}


def money_in(db: Session, start: datetime, end: datetime) -> dict:
    """Cleared money recorded elsewhere in Group HQ inside [start, end), in pence: mentee payments by the day
    they cleared, territory fees by the day they cleared (a refunded one is left out), and paid product
    purchases as the Growth screens count them. Read only; nothing here is entered on the board."""
    mentees = db.scalar(select(func.coalesce(func.sum(Payment.amount), 0))
                        .where(Payment.cleared_at >= start, Payment.cleared_at < end)) or 0
    territory = db.scalar(select(func.coalesce(func.sum(FranchisePayment.amount_pence), 0))
                          .where(FranchisePayment.cleared_at >= start, FranchisePayment.cleared_at < end,
                                 FranchisePayment.refunded_at.is_(None))) or 0
    products = db.scalar(select(func.coalesce(func.sum(ProductPurchase.amount_pence), 0))
                         .where(ProductPurchase.occurred_at >= start, ProductPurchase.occurred_at < end,
                                ProductPurchase.status == "paid", ProductPurchase.kind != "refund",
                                ProductPurchase.currency == "gbp")) or 0
    result = {"mentees": int(Decimal(mentees) * 100), "territory": int(territory), "products": int(products)}
    result["total"] = sum(result.values())
    return result
