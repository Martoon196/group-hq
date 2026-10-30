"""Founder campaign planning in Group HQ, with no provider calls.

Relaunch72 owns lead details, contact permission and every channel effect. Group HQ stores only
the fields in its existing PP read contract, plus founder actions and an audit trail.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import CampaignAction, CampaignActivity, CampaignLead, CampaignPlan, User, utcnow
from .audit import audit
from .relaunch72_crm import LeadPage


@dataclass(frozen=True)
class Step:
    key: str
    day: int
    channel: str
    title: str
    guidance: str


PP_BLUEPRINT_KEY = "pp_first_conversation"
PP_BLUEPRINT_VERSION = 1
PP_STEPS = (
    Step("qualify_call", 0, "call", "Find the decision they are making",
         "Ask what kind of property decision they are facing, their timeframe and what is blocking it. Record the answer. Do not assume this lead asked for a call."),
    Step("useful_email", 1, "email", "Draft one useful answer",
         "Use the lead's own question and a currently approved Property Predator tool or resource. This is a planning prompt only; recipient, permission and content approval must be checked before any email."),
    Step("short_sms", 3, "sms", "Draft a short, relevant check-in",
         "Only if the product records permission for this number and purpose. Ask one question, identify Property Predator, and honour any opt-out. No SMS is sent from this workspace."),
    Step("second_call", 5, "call", "Offer a focused walkthrough",
         "If a call is appropriate, offer to look at one real decision together. Log the outcome and next agreed action."),
    Step("whatsapp_option", 8, "whatsapp", "Consider a WhatsApp follow-up",
         "Use only after the product confirms a valid conversation or approved template, the person's permission, and an owned WhatsApp sender. No WhatsApp is sent from this workspace."),
    Step("close_loop_call", 14, "call", "Close the loop or agree a next date",
         "Ask whether the person wants help now, later, or no further contact. Record the answer and hold the plan if they do not want follow-up."),
)


def project_pp_page(db: Session, page: LeadPage, actor: User) -> tuple[int, int]:
    """Upsert one bounded source page by its stable UUID. Caller commits the whole page."""
    created = changed = 0
    for item in page.leads:
        lead = (db.query(CampaignLead).filter_by(brand="pp", source_system="relaunch72", external_id=item.id).first())
        fields = dict(display_name=item.name, source=item.source, lifecycle=item.lifecycle, stage=item.stage,
                      next_task_at=item.next_task_at, last_activity_at=item.last_activity_at)
        if lead is None:
            lead = CampaignLead(brand="pp", source_system="relaunch72", external_id=item.id,
                                synced_at=utcnow(), **fields)
            db.add(lead)
            db.flush()
            audit(db, actor, "campaign.lead.project", f"campaign_lead:{lead.id}",
                  after={"brand": "pp", "external_id": item.id, "source_system": "relaunch72"})
            created += 1
        else:
            before = {key: getattr(lead, key) for key in fields}
            if before != fields:
                for key, value in fields.items():
                    setattr(lead, key, value)
                lead.synced_at = utcnow()
                audit(db, actor, "campaign.lead.refresh", f"campaign_lead:{lead.id}",
                      before={"stage": before["stage"], "lifecycle": before["lifecycle"]},
                      after={"stage": lead.stage, "lifecycle": lead.lifecycle})
                if lead.lifecycle != "lead":
                    for plan in lead.plans:
                        if plan.state == "active":
                            hold_plan(db, plan, actor, f"Source lifecycle changed to {lead.lifecycle}; review before resuming")
                changed += 1
    return created, changed


def start_pp_plan(db: Session, lead: CampaignLead, actor: User, now: datetime | None = None) -> CampaignPlan:
    if lead.brand != "pp" or lead.source_system != "relaunch72" or lead.lifecycle != "lead":
        raise ValueError("Only an active Property Predator lead can start this plan")
    plan = (db.query(CampaignPlan).filter_by(lead_id=lead.id, blueprint_key=PP_BLUEPRINT_KEY,
                                            blueprint_version=PP_BLUEPRINT_VERSION).first())
    if plan is not None:
        return plan
    now = now or utcnow()
    plan = CampaignPlan(lead_id=lead.id, blueprint_key=PP_BLUEPRINT_KEY,
                        blueprint_version=PP_BLUEPRINT_VERSION, state="active", hold_reason="",
                        started_at=now)
    try:
        with db.begin_nested():
            db.add(plan)
            db.flush()
    except IntegrityError:
        existing = (db.query(CampaignPlan).filter_by(lead_id=lead.id, blueprint_key=PP_BLUEPRINT_KEY,
                                                     blueprint_version=PP_BLUEPRINT_VERSION).first())
        if existing is None:
            raise
        return existing
    for step in PP_STEPS:
        db.add(CampaignAction(plan_id=plan.id, step_key=step.key, channel=step.channel, title=step.title,
                              guidance=step.guidance, due_at=now + timedelta(days=step.day),
                              state="open", completed_by=""))
    audit(db, actor, "campaign.plan.start", f"campaign_plan:{plan.id}",
          after={"brand": "pp", "lead_id": lead.id, "blueprint_key": PP_BLUEPRINT_KEY,
                 "blueprint_version": PP_BLUEPRINT_VERSION})
    return plan


def hold_plan(db: Session, plan: CampaignPlan, actor: User, reason: str) -> bool:
    if plan.state == "held":
        return False
    if plan.state != "active" or not reason.strip() or len(reason.strip()) > 500:
        raise ValueError("An active plan needs a reason of up to 500 characters to be held")
    plan.state = "held"
    plan.hold_reason = reason.strip()
    plan.held_at = utcnow()
    audit(db, actor, "campaign.plan.hold", f"campaign_plan:{plan.id}",
          before={"state": "active"}, after={"state": "held", "reason": plan.hold_reason})
    return True


def resume_plan(db: Session, plan: CampaignPlan, actor: User) -> bool:
    if plan.state == "active":
        return False
    if plan.state != "held":
        raise ValueError("Only a held plan can resume")
    previous_reason = plan.hold_reason
    plan.state = "active"
    plan.hold_reason = ""
    plan.held_at = None
    audit(db, actor, "campaign.plan.resume", f"campaign_plan:{plan.id}",
          before={"state": "held", "reason": previous_reason}, after={"state": "active"})
    return True


def record_call(db: Session, action: CampaignAction, actor: User, outcome: str) -> bool:
    """Record a human call. Other channel actions stay planning prompts in GHQ-OMNI-001."""
    if action.channel != "call" or action.plan.state != "active":
        raise ValueError("Only an active call task can be completed")
    if action.state == "done":
        return False
    if action.state != "open" or not outcome.strip() or len(outcome.strip()) > 2000:
        raise ValueError("A call outcome of up to 2000 characters is required")
    action.state = "done"
    action.completed_at = utcnow()
    action.completed_by = actor.email
    db.add(CampaignActivity(plan_id=action.plan_id, action_id=action.id, kind="call",
                            summary=outcome.strip(), actor=actor.email, at=action.completed_at))
    audit(db, actor, "campaign.call.record", f"campaign_action:{action.id}",
          before={"state": "open"}, after={"state": "done", "plan_id": action.plan_id})
    return True
