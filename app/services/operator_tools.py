"""The tools the operator API offers over MCP (docs/operator-api.md), each bounded and run as one admin user.

A read tool answers from the same queries the screens use and returns compact JSON: ids, names, states and ISO
timestamps, never a password hash, authenticator secret, token or product credential. A CRM write tool goes
through app.services.campaigns exactly as app.routers.crm does, so the same rules, audit rows and refusals apply; the
caller commits. Every other screen's write tools are in app.services.operator_writes and run that screen's own code.
Write tools are offered only while GHQ_OPERATOR_WRITES is "enabled".

A ToolError is a sentence for the model ("Campaign lead 12 was not found"); anything else that goes wrong is
reported as a generic failure by the router, never as a traceback.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .. import config
from ..models import (Acquisition, AuditLog, CampaignAction, CampaignLead, CampaignPlan, ConnectorCall, EntitlementGrant,
                      LapsDeal, Mentee, Person, Programme, User, utcnow)
from . import campaigns, settings
from . import acquisitions as acquisitions_service
from . import laps as laps_service
from .operator_base import Tool, ToolError, _int, _schema, _text
from .operator_writes import WRITE_TOOLS as SCREEN_WRITE_TOOLS
from .relaunch72_crm import CrmUnavailable, fetch_leads
from .relaunch72_sso import BRANDS as WEBINAR_BRANDS
from .relaunch72_webinars import WebinarsUnavailable, fetch_webinars

MAX_LIMIT = 100
DEFAULT_LIMIT = 25
MAX_QUERY = 120
SECRET_WORDS = ("token", "secret", "key", "password", "credential")
WRITES_DISABLED = "writes are disabled on this deployment (GHQ_OPERATOR_WRITES is not \"enabled\")"


# ---- argument helpers -------------------------------------------------------------------------------------------
def _limit(args: dict, default: int = DEFAULT_LIMIT, cap: int = MAX_LIMIT) -> int:
    raw = args.get("limit", default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ToolError(f"limit must be a whole number from 1 to {cap}")
    return min(raw, cap)


def _like(term: str) -> str:
    return "%" + term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


LIMIT_PROP = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "description": f"How many rows, at most {MAX_LIMIT}; default {DEFAULT_LIMIT}."}
QUERY_PROP = {"type": "string", "maxLength": MAX_QUERY, "description": "Case-insensitive text to match."}


# ---- serialisers -------------------------------------------------------------------------------------------------
def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


def _lead_row(lead: CampaignLead, plan: CampaignPlan | None = None) -> dict:
    row = {"id": lead.id, "brand": lead.brand, "name": lead.display_name, "source": lead.source, "lifecycle": lead.lifecycle,
           "stage": lead.stage, "external_id": lead.external_id, "next_task_at": lead.next_task_at,
           "last_activity_at": lead.last_activity_at, "opportunity_status": lead.opportunity_status,
           "value_minor": lead.value_minor, "synced_at": _iso(lead.synced_at),
           "source_url": f"https://hq.propertypredator.com/portal/crm/contacts/{lead.external_id}"}
    if plan is not None:
        row["plan"] = {"id": plan.id, "state": plan.state, "hold_reason": plan.hold_reason or None, "started_at": _iso(plan.started_at)}
    else:
        row["plan"] = None
    return row


def _action_row(action: CampaignAction) -> dict:
    return {"id": action.id, "step": action.step_key, "channel": action.channel, "title": action.title, "guidance": action.guidance,
            "due_at": _iso(action.due_at), "state": action.state, "completed_at": _iso(action.completed_at),
            "completed_by": action.completed_by or None}


def _plan_row(plan: CampaignPlan) -> dict:
    return {"id": plan.id, "lead_id": plan.lead_id, "blueprint": f"{plan.blueprint_key} v{plan.blueprint_version}", "state": plan.state,
            "hold_reason": plan.hold_reason or None, "held_at": _iso(plan.held_at), "started_at": _iso(plan.started_at),
            "actions": [_action_row(a) for a in plan.actions],
            "activities": [{"id": x.id, "action_id": x.action_id, "kind": x.kind, "summary": x.summary, "actor": x.actor, "at": _iso(x.at)}
                           for x in plan.activities]}


def _person_row(person: Person) -> dict:
    return {"id": person.id, "email": person.person_key, "name": person.name, "phone": person.phone or None,
            "product_ids": {"pp_account_id": person.pp_account_id, "ordris_user_id": person.ordris_user_id,
                            "ordris_org_id": person.ordris_org_id, "kp_user_id": person.kp_user_id,
                            "kp_investor_id": person.kp_investor_id, "hnh_partner_id": person.hnh_partner_id,
                            "hnh_user_id": person.hnh_user_id},
            "access_held": bool(person.access_held_at), "access_held_reason": person.access_held_reason or None,
            "created_at": _iso(person.created_at)}


def _grant_row(grant: EntitlementGrant) -> dict:
    return {"id": grant.id, "person_id": grant.person_id, "mentee_id": grant.mentee_id, "product": grant.product, "kind": grant.kind,
            "value": grant.value, "state": grant.state, "starts_at": _iso(grant.starts_at), "ends_at": _iso(grant.ends_at),
            "source": grant.source, "reason": grant.reason or None, "held": bool(grant.held_at), "held_reason": grant.held_reason or None,
            "last_sync_at": _iso(grant.last_sync_at), "last_error": (grant.last_error or "")[:300] or None}


def _deal_row(deal: LapsDeal) -> dict:
    return {"id": deal.id, "name": deal.name, "contact": deal.contact or None, "brand": deal.brand, "offer": deal.offer or None,
            "source": deal.source or None, "stage": deal.stage, "value_pence": deal.value_pence, "lead_at": _iso(deal.lead_at),
            "appointment_at": _iso(deal.appointment_at), "presentation_at": _iso(deal.presentation_at), "sale_at": _iso(deal.sale_at),
            "lost_at": _iso(deal.lost_at), "next_step": deal.next_step or None, "next_step_on": _iso(deal.next_step_on),
            "origin": deal.origin}


def _acquisition_row(deal: Acquisition) -> dict:
    return {"id": deal.id, "title": deal.title, "postcode": deal.postcode or None, "kind": deal.kind, "units": deal.units, "plan": deal.plan,
            "seller_name": deal.seller_name or None, "seller_type": deal.seller_type, "situation": deal.situation or None,
            "source": deal.source or None, "stage": deal.stage, "asking_pence": deal.asking_pence, "offer_pence": deal.offer_pence,
            "agreed_pence": deal.agreed_pence, "value_pence": deal.value_pence, "works_pence": deal.works_pence,
            "lead_at": _iso(deal.lead_at), "completed_at": _iso(deal.completed_at), "lost_at": _iso(deal.lost_at),
            "lost_reason": deal.lost_reason or None, "target_completion": _iso(deal.target_completion),
            "next_step": deal.next_step or None, "next_step_on": _iso(deal.next_step_on), "checks_done": sorted(deal.checks or {})}


def _audit_row(row: AuditLog) -> dict:
    return {"id": row.id, "at": _iso(row.created_at), "actor": row.actor, "action": row.action, "entity": row.entity}


# ---- read tools -------------------------------------------------------------------------------------------------
def group_hq_overview(db: Session, user: User, args: dict) -> dict:
    """The dashboard's counts (app.routers.dashboard), the campaign, LAPS and property-deal counts, and the latest changes."""
    stats = {
        "people": db.query(Person).count(),
        "mentees_active": db.query(Mentee).filter(Mentee.status == "active").count(),
        "mentees_lapsed": db.query(Mentee).filter(Mentee.status == "lapsed").count(),
        "grants_active": db.query(EntitlementGrant).filter(EntitlementGrant.state == "active").count(),
        "grants_pending": db.query(EntitlementGrant).filter(EntitlementGrant.state == "pending").count(),
        "grants_failed": db.query(EntitlementGrant).filter(EntitlementGrant.state == "failed").count(),
        "calls_failed": db.query(ConnectorCall).filter(ConnectorCall.status == "failed").count(),
        "calls_pending": db.query(ConnectorCall).filter(ConnectorCall.status == "pending").count(),
        "programmes": db.query(Programme).filter(Programme.active.is_(True)).count(),
        "crm_leads_pp": db.query(CampaignLead).filter(CampaignLead.brand == "pp").count(),
        "crm_plans_active": db.query(CampaignPlan).filter(CampaignPlan.state == "active").count(),
        "crm_calls_due": db.query(CampaignAction).join(CampaignPlan).filter(
            CampaignPlan.state == "active", CampaignAction.state == "open", CampaignAction.channel == "call",
            CampaignAction.due_at <= utcnow()).count(),
        "laps_open_deals": db.query(LapsDeal).filter(LapsDeal.stage.in_(laps_service.PIPELINE[:-1])).count(),
        "acquisitions_open": db.query(Acquisition).filter(Acquisition.stage.in_(acquisitions_service.OPEN)).count(),
    }
    recent = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(12).all()
    return {"as_of": _iso(utcnow()), "acting_as": user.email, "writes_enabled": config.operator_writes_enabled(),
            "stats": stats, "recent_changes": [_audit_row(r) for r in recent]}


def crm_leads(db: Session, user: User, args: dict) -> dict:
    limit = _limit(args)
    query = _text(args, "query", MAX_QUERY)
    q = db.query(CampaignLead).filter(CampaignLead.brand == "pp")
    if query:
        q = q.filter(func.lower(CampaignLead.display_name).like(_like(query), escape="\\"))
    leads = q.order_by(CampaignLead.synced_at.desc(), CampaignLead.id.desc()).limit(limit).all()
    ids = [lead.id for lead in leads]
    plans = db.query(CampaignPlan).filter(CampaignPlan.lead_id.in_(ids)).all() if ids else []
    by_lead = {plan.lead_id: plan for plan in plans}
    return {"brand": "pp", "count": len(leads), "leads": [_lead_row(lead, by_lead.get(lead.id)) for lead in leads]}


def _pp_lead(db: Session, lead_id: int) -> CampaignLead:
    lead = db.get(CampaignLead, lead_id)
    if lead is None or lead.brand != "pp" or lead.source_system != "relaunch72":
        raise ToolError(f"Campaign lead {lead_id} was not found")
    return lead


def crm_lead(db: Session, user: User, args: dict) -> dict:
    lead = _pp_lead(db, _int(args, "lead_id"))
    plan = (db.query(CampaignPlan).options(selectinload(CampaignPlan.actions), selectinload(CampaignPlan.activities))
            .filter_by(lead_id=lead.id, blueprint_key=campaigns.PP_BLUEPRINT_KEY, blueprint_version=campaigns.PP_BLUEPRINT_VERSION).first())
    row = _lead_row(lead, plan)
    row["plan"] = _plan_row(plan) if plan is not None else None
    return row


def people_search(db: Session, user: User, args: dict) -> dict:
    query = _text(args, "query", MAX_QUERY, required=True)
    limit = _limit(args)
    like = _like(query)
    rows = (db.query(Person).filter(or_(func.lower(Person.person_key).like(like, escape="\\"),
                                        func.lower(Person.name).like(like, escape="\\")))
            .order_by(Person.person_key).limit(limit).all())
    return {"count": len(rows), "people": [_person_row(p) for p in rows]}


def mentees_list(db: Session, user: User, args: dict) -> dict:
    limit = _limit(args)
    query = _text(args, "query", MAX_QUERY)
    status = _text(args, "status", 10)
    q = db.query(Mentee).options(selectinload(Mentee.person), selectinload(Mentee.programme))
    if status:
        if status not in ("active", "lapsed", "ended"):
            raise ToolError("status is active, lapsed or ended")
        q = q.filter(Mentee.status == status)
    if query:
        like = _like(query)
        q = q.join(Person, Person.id == Mentee.person_id).filter(or_(
            func.lower(Person.person_key).like(like, escape="\\"), func.lower(Person.name).like(like, escape="\\"),
            func.lower(Mentee.cohort).like(like, escape="\\")))
    rows = q.order_by(Mentee.id.desc()).limit(limit).all()
    return {"count": len(rows), "mentees": [
        {"id": m.id, "person": {"id": m.person.id, "email": m.person.person_key, "name": m.person.name},
         "programme": {"id": m.programme.id, "code": m.programme.code, "name": m.programme.name},
         "payment_mode": m.payment_mode, "start_date": _iso(m.start_date), "status": m.status, "cohort": m.cohort or None,
         "created_at": _iso(m.created_at)} for m in rows]}


def grants_list(db: Session, user: User, args: dict) -> dict:
    limit = _limit(args)
    state = _text(args, "state", 10)
    product = _text(args, "product", 10)
    q = db.query(EntitlementGrant)
    if state:
        if state not in ("pending", "active", "revoked", "failed"):
            raise ToolError("state is pending, active, revoked or failed")
        q = q.filter(EntitlementGrant.state == state)
    if product:
        if product not in ("pp", "ordris", "kp", "hnh"):
            raise ToolError("product is pp, ordris, kp or hnh")
        q = q.filter(EntitlementGrant.product == product)
    rows = q.order_by(EntitlementGrant.id.desc()).limit(limit).all()
    return {"count": len(rows), "grants": [_grant_row(g) for g in rows]}


def laps_board(db: Session, user: User, args: dict) -> dict:
    """The open columns of the LAPS board, each capped, with the counts and the score the screen shows."""
    brand = _text(args, "brand", 8)
    if brand and brand not in laps_service.BRANDS:
        raise ToolError("brand is one of " + ", ".join(laps_service.BRANDS))
    per_stage = 40
    scoped = (lambda stmt: stmt.where(LapsDeal.brand == brand)) if brand else (lambda stmt: stmt)
    columns: dict[str, list] = {}
    counts: dict[str, int] = {}
    for stage in laps_service.PIPELINE[:-1]:
        stmt = scoped(select(LapsDeal).where(LapsDeal.stage == stage)).order_by(
            LapsDeal.next_step_on.asc().nulls_last(), LapsDeal.lead_at.desc(), LapsDeal.id.desc()).limit(per_stage)
        columns[stage] = [_deal_row(d) for d in db.scalars(stmt).all()]
        counts[stage] = int(db.scalar(scoped(select(func.count()).where(LapsDeal.stage == stage))) or 0)
    counts["sale"] = int(db.scalar(scoped(select(func.count()).where(LapsDeal.stage == "sale"))) or 0)
    counts["lost"] = int(db.scalar(scoped(select(func.count()).where(LapsDeal.stage == "lost"))) or 0)
    now = utcnow()
    windows = laps_service.periods(now)
    score = {name: laps_service.counts(db, *windows[name], brand or None) for name in ("this_week", "last_week", "this_month", "last_month")}
    return {"brand": brand or None, "brands": laps_service.BRANDS, "counts": counts, "columns": columns,
            "score": {k: {kk: _plain(vv) for kk, vv in v.items()} for k, v in score.items()}}


def acquisitions_list(db: Session, user: User, args: dict) -> dict:
    limit = _limit(args)
    stage = _text(args, "stage", 12)
    q = db.query(Acquisition)
    if stage:
        if stage not in acquisitions_service.LABELS:
            raise ToolError("stage is one of " + ", ".join(acquisitions_service.LABELS))
        q = q.filter(Acquisition.stage == stage)
    else:
        q = q.filter(Acquisition.stage.in_(acquisitions_service.OPEN))
    rows = q.order_by(Acquisition.updated_at.desc(), Acquisition.id.desc()).limit(limit).all()
    return {"count": len(rows), "stages": acquisitions_service.LABELS, "deals": [_acquisition_row(d) for d in rows]}


def webinars_list(db: Session, user: User, args: dict) -> dict:
    brand = _text(args, "brand", 8)
    if brand and brand not in WEBINAR_BRANDS:
        raise ToolError("brand is pp or mhp")
    out = {}
    for code in ([brand] if brand else list(WEBINAR_BRANDS)):
        try:
            listing = fetch_webinars(code)
        except WebinarsUnavailable:
            out[code] = {"name": WEBINAR_BRANDS[code], "availability": "unavailable", "webinars": []}
            continue
        out[code] = {"name": WEBINAR_BRANDS[code], "availability": listing.availability, "workspace": listing.workspace_name,
                     "webinars": [{"id": w.id, "title": w.title, "kind": w.kind, "status": w.status, "starts_at": w.starts_at,
                                   "timezone": w.timezone, "duration_minutes": w.duration_minutes, "session_state": w.session_state,
                                   "registered": w.registered, "crm_linked": w.crm_linked, "public_url": w.public_url,
                                   "manage_path": w.manage_path} for w in listing.webinars]}
    return {"brands": out}


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(word in lowered for word in SECRET_WORDS)


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if not _looks_secret(str(k))}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return _plain(value)


def settings_get(db: Session, user: User, args: dict) -> dict:
    return {"settings": _scrub(settings.get_all()), "product_modes": settings.product_modes(db),
            "writes_enabled": config.operator_writes_enabled()}


# ---- write tools (app.routers.crm's actions, through app.services.campaigns) -------------------------------------
def crm_start_plan(db: Session, user: User, args: dict) -> dict:
    lead = _pp_lead(db, _int(args, "lead_id"))
    try:
        plan = campaigns.start_pp_plan(db, lead, user)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    db.flush()
    return {"ok": True, "lead_id": lead.id, "plan_id": plan.id, "state": plan.state,
            "note": "Follow-up plan is ready for the founder's review. No messages were sent."}


def _pp_plan(db: Session, plan_id: int) -> CampaignPlan:
    plan = db.get(CampaignPlan, plan_id)
    if plan is None or plan.lead.brand != "pp":
        raise ToolError(f"Campaign plan {plan_id} was not found")
    return plan


def crm_hold_plan(db: Session, user: User, args: dict) -> dict:
    plan = _pp_plan(db, _int(args, "plan_id"))
    reason = _text(args, "reason", 500, required=True)
    try:
        changed = campaigns.hold_plan(db, plan, user, reason)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {"ok": True, "plan_id": plan.id, "state": plan.state, "changed": changed}


def crm_resume_plan(db: Session, user: User, args: dict) -> dict:
    plan = _pp_plan(db, _int(args, "plan_id"))
    try:
        changed = campaigns.resume_plan(db, plan, user)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {"ok": True, "plan_id": plan.id, "state": plan.state, "changed": changed}


def crm_record_call(db: Session, user: User, args: dict) -> dict:
    action_id = _int(args, "action_id")
    outcome = _text(args, "outcome", 2000, required=True)
    action = db.get(CampaignAction, action_id)
    if action is None or action.plan.lead.brand != "pp":
        raise ToolError(f"Campaign action {action_id} was not found")
    try:
        changed = campaigns.record_call(db, action, user, outcome)
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    return {"ok": True, "action_id": action.id, "plan_id": action.plan_id, "state": action.state, "changed": changed}


def crm_sync_property_predator(db: Session, user: User, args: dict) -> dict:
    cursor = _text(args, "cursor", 200)
    try:
        page = fetch_leads(cursor or None)
    except CrmUnavailable:
        raise ToolError("Property Predator lead connection is unavailable; no local records changed") from None
    created, changed = campaigns.project_pp_page(db, page, user)
    return {"ok": True, "checked": len(page.leads), "created": created, "changed": changed, "next_cursor": page.next_cursor}


# ---- the catalogue -------------------------------------------------------------------------------------------
READ_TOOLS: tuple[Tool, ...] = (
    Tool("group_hq_overview", "The Group HQ dashboard in numbers: people, mentees, grants, connector calls, programmes, "
         "CRM leads and plans, calls due, open LAPS deals and property deals, plus the latest changes in the audit log.",
         _schema(), group_hq_overview),
    Tool("crm_leads", "Property Predator CRM leads synced into Group HQ, newest first, each with its follow-up plan state.",
         _schema({"limit": LIMIT_PROP, "query": {**QUERY_PROP, "description": "Match on the lead's name."}}), crm_leads),
    Tool("crm_lead", "One Property Predator lead with its follow-up plan, the plan's actions and the calls recorded.",
         _schema({"lead_id": {"type": "integer", "minimum": 1, "description": "The Group HQ campaign lead id."}}, ["lead_id"]), crm_lead),
    Tool("people_search", "Search the people register by email or name.",
         _schema({"query": QUERY_PROP, "limit": LIMIT_PROP}, ["query"]), people_search),
    Tool("mentees_list", "Mentees with their person and programme, newest first; optionally filtered by status or a search.",
         _schema({"limit": LIMIT_PROP, "query": {**QUERY_PROP, "description": "Match on email, name or cohort."},
                  "status": {"type": "string", "enum": ["active", "lapsed", "ended"]}}), mentees_list),
    Tool("grants_list", "Entitlement grants (what each person holds in each product), newest first.",
         _schema({"limit": LIMIT_PROP, "state": {"type": "string", "enum": ["pending", "active", "revoked", "failed"]},
                  "product": {"type": "string", "enum": ["pp", "ordris", "kp", "hnh"]}}), grants_list),
    Tool("laps_board", "The LAPS sales board: open deals by stage (leads, appointments, presentations), counts and the weekly "
         "and monthly score, optionally for one business.",
         _schema({"brand": {"type": "string", "enum": list(laps_service.BRANDS)}}), laps_board),
    Tool("acquisitions_list", "Property deals (the buying side): open deals by default, or one stage.",
         _schema({"limit": LIMIT_PROP, "stage": {"type": "string", "enum": list(acquisitions_service.LABELS)}}), acquisitions_list),
    Tool("webinars_list", "The webinars Relaunch72 runs for Property Predator and Martin Howard Property, read live.",
         _schema({"brand": {"type": "string", "enum": list(WEBINAR_BRANDS)}}), webinars_list),
    Tool("settings_get", "The non-secret display settings, product facts and each product's Live / Dry run mode.",
         _schema(), settings_get),
)

CRM_WRITE_TOOLS: tuple[Tool, ...] = (
    Tool("crm_start_plan", "Start the Property Predator first-conversation follow-up plan for a lead. Plans only; nothing is sent.",
         _schema({"lead_id": {"type": "integer", "minimum": 1}}, ["lead_id"]), crm_start_plan, write=True),
    Tool("crm_hold_plan", "Hold an active follow-up plan with a reason.",
         _schema({"plan_id": {"type": "integer", "minimum": 1}, "reason": {"type": "string", "maxLength": 500}}, ["plan_id", "reason"]),
         crm_hold_plan, write=True),
    Tool("crm_resume_plan", "Resume a held follow-up plan.",
         _schema({"plan_id": {"type": "integer", "minimum": 1}}, ["plan_id"]), crm_resume_plan, write=True),
    Tool("crm_record_call", "Record the outcome of a call task on an active plan.",
         _schema({"action_id": {"type": "integer", "minimum": 1}, "outcome": {"type": "string", "maxLength": 2000}},
                 ["action_id", "outcome"]), crm_record_call, write=True),
    Tool("crm_sync_property_predator", "Pull one page of Property Predator CRM leads from Relaunch72 into Group HQ.",
         _schema({"cursor": {"type": "string", "maxLength": 200, "description": "The next_cursor of the previous page."}}),
         crm_sync_property_predator, write=True),
)

# The CRM's write tools, then every admin screen's (app.services.operator_writes).
WRITE_TOOLS: tuple[Tool, ...] = CRM_WRITE_TOOLS + SCREEN_WRITE_TOOLS
assert len({t.name for t in READ_TOOLS + WRITE_TOOLS}) == len(READ_TOOLS + WRITE_TOOLS), "tool names are unique"

ALL_TOOLS: dict[str, Tool] = {tool.name: tool for tool in READ_TOOLS + WRITE_TOOLS}


def available(*, writes_enabled: bool | None = None) -> list[Tool]:
    writes = config.operator_writes_enabled() if writes_enabled is None else writes_enabled
    return [tool for tool in READ_TOOLS + WRITE_TOOLS if writes or not tool.write]


def get(name: str) -> Tool | None:
    return ALL_TOOLS.get(name) if isinstance(name, str) else None
