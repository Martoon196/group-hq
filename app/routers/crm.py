"""Founder CRM front door; product lead data is read only from Relaunch72."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, selectinload

from ..auth import check_origin, require_admin
from ..db import get_db
from ..models import CampaignAction, CampaignLead, CampaignPlan, User, utcnow
from ..services import campaigns
from ..services.relaunch72_crm import CrmUnavailable, fetch_leads
from ..templating import go, render

router = APIRouter(dependencies=[Depends(check_origin)])


@router.get("/crm", response_class=HTMLResponse)
def crm(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    cursor = request.query_params.get("cursor")
    try:
        page = fetch_leads(cursor)
        unavailable = False
    except CrmUnavailable:
        page = None
        unavailable = True
    leads = (db.query(CampaignLead).filter_by(brand="pp").order_by(CampaignLead.synced_at.desc(), CampaignLead.id.desc())
             .limit(50).all())
    lead_ids = [lead.id for lead in leads]
    plans = (db.query(CampaignPlan).filter(CampaignPlan.lead_id.in_(lead_ids)).all() if lead_ids else [])
    plan_by_lead = {plan.lead_id: plan for plan in plans}
    due_calls = (db.query(CampaignAction).join(CampaignPlan).join(CampaignLead)
                 .filter(CampaignLead.brand == "pp", CampaignPlan.state == "active", CampaignAction.state == "open",
                         CampaignAction.channel == "call", CampaignAction.due_at <= utcnow())
                 .order_by(CampaignAction.due_at, CampaignAction.id).limit(10).all())
    response = render(request, "crm.html", {"user": user, "section": "crm", "page": page,
                                            "unavailable": unavailable, "leads": leads,
                                            "plan_by_lead": plan_by_lead, "due_calls": due_calls})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/crm/sync", response_class=HTMLResponse)
def sync_pp(cursor: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    try:
        page = fetch_leads(cursor or None)
    except CrmUnavailable:
        return go("/crm", err="Property Predator lead connection is unavailable; no local records changed.")
    created, changed = campaigns.project_pp_page(db, page, user)
    db.commit()
    return go("/crm", ok=f"Property Predator page synced: {created} new, {changed} changed; {len(page.leads)} checked.")


def _lead(db: Session, lead_id: int) -> CampaignLead:
    lead = db.get(CampaignLead, lead_id)
    if lead is None or lead.brand != "pp" or lead.source_system != "relaunch72":
        raise HTTPException(status_code=404, detail="Campaign lead not found")
    return lead


@router.get("/crm/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    lead = _lead(db, lead_id)
    plan = (db.query(CampaignPlan).options(selectinload(CampaignPlan.actions), selectinload(CampaignPlan.activities))
            .filter_by(lead_id=lead.id, blueprint_key=campaigns.PP_BLUEPRINT_KEY,
                       blueprint_version=campaigns.PP_BLUEPRINT_VERSION).first())
    response = render(request, "crm_lead.html", {"user": user, "section": "crm", "lead": lead, "plan": plan,
                                                 "source_url": f"https://hq.propertypredator.com/portal/crm/contacts/{lead.external_id}"})
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/crm/leads/{lead_id}/start", response_class=HTMLResponse)
def start_plan(lead_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    lead = _lead(db, lead_id)
    try:
        plan = campaigns.start_pp_plan(db, lead, user)
    except ValueError as exc:
        return go(f"/crm/leads/{lead_id}", err=str(exc))
    db.commit()
    return go(f"/crm/leads/{lead_id}", ok=f"Follow-up plan #{plan.id} is ready for your review. No messages were sent.")


def _plan(db: Session, plan_id: int) -> CampaignPlan:
    plan = db.get(CampaignPlan, plan_id)
    if plan is None or plan.lead.brand != "pp":
        raise HTTPException(status_code=404, detail="Campaign plan not found")
    return plan


@router.post("/crm/plans/{plan_id}/hold", response_class=HTMLResponse)
def hold_plan(plan_id: int, reason: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_admin)):
    plan = _plan(db, plan_id)
    try:
        changed = campaigns.hold_plan(db, plan, user, reason)
    except ValueError as exc:
        return go(f"/crm/leads/{plan.lead_id}", err=str(exc))
    db.commit()
    return go(f"/crm/leads/{plan.lead_id}", ok="Follow-up held." if changed else "Follow-up was already held.")


@router.post("/crm/plans/{plan_id}/resume", response_class=HTMLResponse)
def resume_plan(plan_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    plan = _plan(db, plan_id)
    try:
        changed = campaigns.resume_plan(db, plan, user)
    except ValueError as exc:
        return go(f"/crm/leads/{plan.lead_id}", err=str(exc))
    db.commit()
    return go(f"/crm/leads/{plan.lead_id}", ok="Follow-up resumed." if changed else "Follow-up was already active.")


@router.post("/crm/actions/{action_id}/call", response_class=HTMLResponse)
def record_call(action_id: int, outcome: str = Form(""), db: Session = Depends(get_db),
                user: User = Depends(require_admin)):
    action = db.get(CampaignAction, action_id)
    if action is None or action.plan.lead.brand != "pp":
        raise HTTPException(status_code=404, detail="Campaign action not found")
    try:
        changed = campaigns.record_call(db, action, user, outcome)
    except ValueError as exc:
        return go(f"/crm/leads/{action.plan.lead_id}", err=str(exc))
    db.commit()
    return go(f"/crm/leads/{action.plan.lead_id}", ok="Call outcome recorded." if changed else "Call was already recorded.")
