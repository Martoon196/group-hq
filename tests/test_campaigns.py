"""GHQ-OMNI-001: founder planning remains brand-scoped and effect-free."""
from __future__ import annotations

from app.models import AuditLog, CampaignAction, CampaignActivity, CampaignLead, CampaignPlan
from app.routers import crm
from app.services.relaunch72_crm import Lead, LeadPage
from tests.conftest import session

PP_ID = "60e3dbd7-c7e5-48cb-b37b-6909e2cbeace"


def page(stage: str = "New") -> LeadPage:
    return LeadPage((Lead(PP_ID, "Sample Investor", "site", "lead", stage, None, None,
                           f"https://hq.propertypredator.com/portal/crm/contacts/{PP_ID}"),), None)


def sync(admin_client, monkeypatch):
    monkeypatch.setattr(crm, "fetch_leads", lambda cursor=None: page())
    result = admin_client.post("/crm/sync")
    assert result.status_code == 303
    with session() as db:
        return db.query(CampaignLead).filter_by(brand="pp").one().id


def test_campaign_routes_require_admin_and_origin(client, admin_client, monkeypatch):
    monkeypatch.setattr(crm, "fetch_leads", lambda cursor=None: page())
    assert client.get("/crm").status_code == 303
    assert client.post("/crm/sync").status_code == 303
    blocked = admin_client.post("/crm/sync", headers={"Origin": "https://other.example"})
    assert blocked.status_code == 403
    with session() as db:
        assert db.query(CampaignLead).count() == 0


def test_projection_uses_stable_product_id_and_never_fabricates_contact_details(admin_client, monkeypatch):
    lead_id = sync(admin_client, monkeypatch)
    monkeypatch.setattr(crm, "fetch_leads", lambda cursor=None: page("Qualified"))
    assert admin_client.post("/crm/sync").status_code == 303
    with session() as db:
        leads = db.query(CampaignLead).all()
        assert len(leads) == 1 and leads[0].id == lead_id
        assert leads[0].external_id == PP_ID and leads[0].stage == "Qualified"
        assert not hasattr(leads[0], "email") and not hasattr(leads[0], "phone")
        assert db.query(AuditLog).filter(AuditLog.action == "campaign.lead.project").count() == 1
        assert db.query(AuditLog).filter(AuditLog.action == "campaign.lead.refresh").count() == 1


def test_plan_is_idempotent_and_all_non_call_steps_are_planning_only(admin_client, monkeypatch):
    lead_id = sync(admin_client, monkeypatch)
    for _ in range(2):
        result = admin_client.post(f"/crm/leads/{lead_id}/start")
        assert result.status_code == 303
    with session() as db:
        plan = db.query(CampaignPlan).one()
        actions = db.query(CampaignAction).filter_by(plan_id=plan.id).order_by(CampaignAction.due_at).all()
        assert len(actions) == 6
        assert [a.channel for a in actions] == ["call", "email", "sms", "call", "whatsapp", "call"]
        assert db.query(AuditLog).filter(AuditLog.action == "campaign.plan.start").count() == 1
        email_id = next(a.id for a in actions if a.channel == "email")
    assert admin_client.post(f"/crm/actions/{email_id}/call", data={"outcome": "Sent"}).status_code == 303
    with session() as db:
        assert db.get(CampaignAction, email_id).state == "open"
        assert db.query(CampaignActivity).count() == 0
    detail = admin_client.get(f"/crm/leads/{lead_id}")
    assert detail.status_code == 200 and "No messages were sent" not in detail.text
    assert "delivery connection pending" in detail.text


def test_source_archival_holds_the_plan_without_sending(admin_client, monkeypatch):
    lead_id = sync(admin_client, monkeypatch)
    admin_client.post(f"/crm/leads/{lead_id}/start")
    old = page().leads[0]
    archived = Lead(old.id, old.name, old.source, "archived", old.stage, old.next_task_at,
                    old.last_activity_at, old.detail_url)
    monkeypatch.setattr(crm, "fetch_leads", lambda cursor=None: LeadPage((archived,), None))
    assert admin_client.post("/crm/sync").status_code == 303
    with session() as db:
        plan = db.query(CampaignPlan).one()
        assert plan.state == "held" and "archived" in plan.hold_reason
        assert db.query(AuditLog).filter(AuditLog.action == "campaign.plan.hold").count() == 1


def test_call_log_hold_and_brand_boundary(admin_client, monkeypatch):
    lead_id = sync(admin_client, monkeypatch)
    admin_client.post(f"/crm/leads/{lead_id}/start")
    with session() as db:
        plan = db.query(CampaignPlan).one()
        plan_id = plan.id
        action_id = db.query(CampaignAction).filter_by(plan_id=plan_id, channel="call").order_by(CampaignAction.id).first().id
        db.add(CampaignLead(brand="mhp", source_system="relaunch72", external_id=PP_ID,
                            display_name="Other brand", lifecycle="lead"))
        db.commit()
        mhp_id = db.query(CampaignLead).filter_by(brand="mhp").one().id
    assert admin_client.get(f"/crm/leads/{mhp_id}").status_code == 404
    assert admin_client.post(f"/crm/leads/{mhp_id}/start").status_code == 404
    assert admin_client.post(f"/crm/plans/{plan_id}/hold", data={"reason": "Asked to pause"}).status_code == 303
    assert admin_client.post(f"/crm/actions/{action_id}/call", data={"outcome": "No answer"}).status_code == 303
    with session() as db:
        assert db.get(CampaignAction, action_id).state == "open"
    assert admin_client.post(f"/crm/plans/{plan_id}/resume").status_code == 303
    for _ in range(2):
        assert admin_client.post(f"/crm/actions/{action_id}/call", data={"outcome": "Discussed next step"}).status_code == 303
    with session() as db:
        assert db.get(CampaignAction, action_id).state == "done"
        assert db.query(CampaignActivity).filter_by(action_id=action_id, kind="call").count() == 1
        assert db.query(AuditLog).filter(AuditLog.action == "campaign.call.record").count() == 1
