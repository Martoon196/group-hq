"""Property deals: buying a property, portfolio or site, from the seller's first message to completion.

The LAPS board's machinery on a second pipeline: app.services.laps.move stamps each stage as a deal reaches it,
and laps.reached_in counts what happened in a period. What is particular to buying is here: the stages, a
working checklist for each one, and the rough numbers of a deal.

The checklist is a prompt for the usual steps of a purchase in England and Wales. It is not legal advice and
the solicitor's own list comes first.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (ACQUISITION_KINDS, ACQUISITION_PLANS, ACQUISITION_STAGES, SELLER_TYPES, Acquisition,
                      AcquisitionThread)
from . import laps

PIPELINE = ACQUISITION_STAGES[:-1]              # in order; "lost" sits outside it
OPEN = PIPELINE[:-1]                            # every stage before completed
LABELS = {"lead": "New leads", "appraisal": "Appraising", "offer": "Offer made", "agreed": "Offer accepted",
          "legals": "Legals", "exchanged": "Exchanged", "completed": "Completed", "lost": "Lost"}
MOVES = {"lead": "Back to new leads", "appraisal": "Start appraising", "offer": "Offer made",
         "agreed": "Offer accepted", "legals": "Solicitors instructed", "exchanged": "Exchanged",
         "completed": "Completed", "lost": "Mark lost", "reopen": "Reopen"}
KINDS = {"house": "House", "flat": "Flat", "hmo": "HMO", "block": "Block", "portfolio": "Portfolio",
         "commercial": "Commercial", "land": "Land or site", "other": "Other"}
PLANS = {"undecided": "Undecided", "btl": "Buy to let", "brr": "Buy, refurbish, refinance", "flip": "Buy to sell",
         "hmo": "HMO", "development": "Development", "sourcing": "Source for an investor"}
SELLERS = {"unknown": "Not known yet", "landlord": "Landlord", "portfolio_landlord": "Portfolio landlord",
           "owner": "Owner occupier", "developer": "Developer", "agent": "Agent", "probate": "Probate or executor"}
assert tuple(KINDS) == ACQUISITION_KINDS and tuple(PLANS) == ACQUISITION_PLANS and tuple(SELLERS) == SELLER_TYPES
MAX_PENCE = 100_000_000_000                     # one billion pounds; the columns are BIGINT

# The usual steps while a deal sits at each stage. Keys are stored in Acquisition.checks, so never reuse one.
CHECKLIST = {
    "lead": (("replied", "Replied to the seller"),
             ("why_selling", "Asked why they are selling and how soon"),
             ("addresses", "Got the address of every property and the price they want")),
    "appraisal": (("comparables", "Checked sold prices and likely value"),
                  ("rents_works", "Rent or end value worked out and works costed"),
                  ("viewing", "Viewing done, or photos and video for a portfolio"),
                  ("title", "Ownership checked at the Land Registry"),
                  ("tenancies", "Tenancy agreements, rent schedule and safety certificates requested"),
                  ("ceiling", "Plan chosen and the most we will pay decided")),
    "offer": (("offer_written", "Offer sent in writing, subject to contract and survey"),
              ("funds", "Proof of funds or agreement in principle ready"),
              ("chased", "Followed up on the offer")),
    "agreed": (("heads", "Price and terms confirmed in writing (memorandum of sale)"),
               ("solicitors", "Both solicitors' details exchanged"),
               ("instructed", "Our solicitor instructed, ID and source of funds sent"),
               ("finance_applied", "Finance applied for, if borrowing")),
    "legals": (("searches", "Searches ordered"),
               ("survey", "Survey or lender's valuation done"),
               ("finance_offer", "Finance offer received, if borrowing"),
               ("enquiries", "Solicitor's enquiries answered"),
               ("contract", "Contract approved and signed"),
               ("deposit", "Deposit ready"),
               ("insurance", "Buildings insurance arranged from exchange")),
    "exchanged": (("date_fixed", "Completion date fixed"),
                  ("balance", "Balance sent to the solicitor"),
                  ("handover", "Keys or tenant handover arranged")),
    "completed": (("tax_return", "Stamp duty or land transaction tax return filed by the solicitor"),
                  ("registered", "Registered at the Land Registry"),
                  ("tenants_told", "Tenants told who their new landlord is, if tenanted"),
                  ("accounts", "Insurance, utilities and council tax in our name")),
}
CHECK_KEYS = {key: stage for stage, items in CHECKLIST.items() for key, _ in items}
assert len(CHECK_KEYS) == sum(len(items) for items in CHECKLIST.values()), "checklist keys must be unique"


def parse_price(raw: str) -> int | None:
    return laps.parse_pounds(raw, MAX_PENCE)


def price(deal: Acquisition) -> int | None:
    """The best price known: agreed, else our offer, else the asking price."""
    for amount in (deal.agreed_pence, deal.offer_pence, deal.asking_pence):
        if amount is not None:
            return amount
    return None


def numbers(deal: Acquisition) -> dict:
    """Rough sums from the prices on a deal. below_asking is the share our price sits under the asking price;
    headroom is the value after works less the price and the works, before buying costs, tax and finance."""
    ours = deal.agreed_pence if deal.agreed_pence is not None else deal.offer_pence
    below = None
    if ours is not None and deal.asking_pence:
        below = (deal.asking_pence - ours) / deal.asking_pence
    headroom = None
    paying = price(deal)
    if deal.value_pence is not None and paying is not None:
        headroom = deal.value_pence - paying - (deal.works_pence or 0)
    return {"price": paying, "below_asking": below, "headroom": headroom}


def progress(deal: Acquisition) -> dict:
    """Checklist ticks: per stage [(key, label, ticked ISO date or None)], and done/total for the current stage."""
    checks = deal.checks or {}
    stages = {stage: [(key, label, checks.get(key)) for key, label in items] for stage, items in CHECKLIST.items()}
    current = stages.get(deal.stage, [])
    return {"stages": stages, "done": sum(1 for _, _, at in current if at), "total": len(current)}


def toggle(deal: Acquisition, key: str, today: str) -> bool:
    """Tick or untick one checklist item; returns the new state. JSONB needs a new dict to notice the change."""
    if key not in CHECK_KEYS:
        raise ValueError("Unknown checklist item")
    checks = dict(deal.checks or {})
    ticked = key not in checks
    if ticked:
        checks[key] = today
    else:
        del checks[key]
    deal.checks = checks
    return ticked


def move(deal: Acquisition, target: str, now: datetime | None = None) -> None:
    laps.move(deal, target, now, PIPELINE)


def stage_totals(db: Session) -> list[dict]:
    """Open deals at each stage with the total of their best known prices, for the pipeline chart."""
    best = func.coalesce(Acquisition.agreed_pence, Acquisition.offer_pence, Acquisition.asking_pence, 0)
    rows = dict((stage, (n, int(total))) for stage, n, total in db.execute(
        select(Acquisition.stage, func.count(), func.coalesce(func.sum(best), 0))
        .where(Acquisition.stage.in_(OPEN)).group_by(Acquisition.stage)).all())
    return [{"stage": stage, "count": rows.get(stage, (0, 0))[0], "pence": rows.get(stage, (0, 0))[1]} for stage in OPEN]


def completed_between(db: Session, start: datetime, end: datetime) -> dict:
    n, total = db.execute(select(func.count(), func.coalesce(func.sum(Acquisition.agreed_pence), 0)).where(
        Acquisition.completed_at >= start, Acquisition.completed_at < end)).one()
    return {"count": n, "pence": int(total)}


# ---- Inbox conversations attached to a deal ------------------------------------------------------------

def thread_sha(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode()).hexdigest()


def attach(db: Session, deal: Acquisition, account, bound: dict, user_id: int) -> bool:
    """Attach the Inbox conversation a resolved ticket names (app.services.inbox.resolve) to deal. Returns False
    when it was attached already. The caller commits."""
    key = (deal.id, account.key, thread_sha(bound["thread"]))
    if db.get(AcquisitionThread, key):
        return False
    db.add(AcquisitionThread(deal_id=deal.id, account_key=account.key, thread_sha256=key[2], user_id=user_id,
                             thread_id=bound["thread"], participant_id=bound.get("participant"),
                             name=str(bound.get("name") or "")[:500]))
    return True


def attached(db: Session, deal_id: int) -> list[AcquisitionThread]:
    return list(db.scalars(select(AcquisitionThread).where(AcquisitionThread.deal_id == deal_id)
                           .order_by(AcquisitionThread.created_at, AcquisitionThread.account_key)))


def deals_for(db: Session, account, thread_id: str) -> list[Acquisition]:
    """The deals an Inbox conversation is attached to, newest first."""
    return list(db.scalars(select(Acquisition).join(AcquisitionThread, AcquisitionThread.deal_id == Acquisition.id)
                           .where(AcquisitionThread.account_key == account.key,
                                  AcquisitionThread.thread_sha256 == thread_sha(thread_id))
                           .order_by(Acquisition.id.desc())))


def open_deals(db: Session, limit: int = 100) -> list[Acquisition]:
    return list(db.scalars(select(Acquisition).where(Acquisition.stage.in_(OPEN))
                           .order_by(Acquisition.updated_at.desc(), Acquisition.id.desc()).limit(limit)))
