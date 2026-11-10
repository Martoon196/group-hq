"""The operator API's write tools for the admin screens (app.services.operator_writes), over POST /mcp with the static
bearer, writes enabled. For every tool: a happy path that checks the database changed as the screen's button changes
it (same audit row, the operator.tool row beside it), and a refusal that comes back as the screen's sentence with
isError and leaves the database as it was. Also: tools/list hides every one of them while writes are disabled, and the
routes whose logic moved into shared functions still answer as before. CONNECTORS_DRY_RUN is forced on by conftest,
so no product is called."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.models import (Acquisition, AcquisitionThread, Activity, AuditLog, CommissionEntry, CommissionRule, ConnectorCall,
                        EntitlementGrant, FranchiseOpportunity, FranchisePayment, LapsDeal, Mentee, Payment, Person, PostcodeDistrict,
                        Programme, Seller, Statement, Task, utcnow)
from app.routers import laps as laps_router
from app.routers import mentees as mentees_router
from app.routers import operator_mcp
from app.services import inbox_crm, operator_tools, operator_writes
from app.services import laps as laps_service
from app.services import territory_rules as tr
from tests.conftest import ADMIN_EMAIL, session
from tests import test_acquisition_threads as threads
from tests.test_commission import _ledger_for_statements, confirm_rules, entries_for
from tests.test_franchise import seed_districts
from tests.test_people import flash_of

TOKEN = "static-operator-token-synthetic-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TODAY = utcnow().date()
NEW_TOOLS = [t.name for t in operator_writes.WRITE_TOOLS]
connected = threads.connected          # the fixture with one Inbox account and a stand-in provider


@pytest.fixture
def mcp(monkeypatch):
    monkeypatch.setattr(config, "GHQ_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GHQ_OPERATOR_USER_EMAIL", ADMIN_EMAIL)
    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "enabled")
    app = FastAPI()
    app.include_router(operator_mcp.router)
    return TestClient(app)


def rpc(c: TestClient, method: str, params: dict | None = None) -> dict:
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})},
               headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def call(c: TestClient, tool: str, /, **arguments) -> tuple[bool, str]:
    result = rpc(c, "tools/call", {"name": tool, "arguments": arguments})
    return result["isError"], result["content"][0]["text"]


def ok(c: TestClient, tool: str, /, **arguments) -> dict:
    is_error, text = call(c, tool, **arguments)
    assert not is_error, text
    data = json.loads(text)
    assert data["ok"] is True and isinstance(data["message"], str)
    with session() as db:
        last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        assert (last.action, last.entity, last.actor) == ("operator.tool", f"tool:{tool}", ADMIN_EMAIL)
    return data


def audit_count() -> int:
    with session() as db:
        return db.query(AuditLog).count()


def refused(c: TestClient, tool: str, expected: str, /, **arguments) -> str:
    """The tool refuses with the screen's sentence and no audit row (so nothing was written) appears."""
    before = audit_count()
    is_error, text = call(c, tool, **arguments)
    assert is_error, text
    assert expected in text, text
    assert audit_count() == before, "a refusal writes nothing"
    return text


def count(model, *where) -> int:
    with session() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def get(model, key):
    with session() as db:
        return db.get(model, key)


def actions(entity: str) -> list[str]:
    with session() as db:
        return [r.action for r in db.query(AuditLog).filter(AuditLog.entity == entity).order_by(AuditLog.id)]


def make_person(email: str = "amy@example.test", name: str = "Amy Archer", **extra) -> int:
    with session() as db:
        person = Person(person_key=email, name=name, **extra)
        db.add(person)
        db.commit()
        return person.id


def programme_id(code: str = "mentorship") -> int:
    with session() as db:
        return db.query(Programme).filter(Programme.code == code).one().id


def make_mentee(person_id: int, mode: str = "full") -> int:
    with session() as db:
        mentee = Mentee(person_id=person_id, programme_id=programme_id(), payment_mode=mode, start_date=TODAY, status="active")
        db.add(mentee)
        db.commit()
        return mentee.id


def make_grant(person_id: int, **kw) -> int:
    with session() as db:
        fields = {"product": "pp", "kind": "comp_tier", "value": "pro_buyer", "starts_at": utcnow(),
                  "ends_at": utcnow() + timedelta(days=200), **kw}
        grant = EntitlementGrant(person_id=person_id, **fields)
        db.add(grant)
        db.commit()
        return grant.id


# ---- the catalogue -----------------------------------------------------------------------------------------------
def test_tools_list_hides_every_new_write_tool_until_writes_are_enabled(mcp, monkeypatch):
    names = {t["name"] for t in rpc(mcp, "tools/list")["tools"]}
    assert set(NEW_TOOLS) <= names
    for tool in rpc(mcp, "tools/list")["tools"]:
        if tool["name"] in NEW_TOOLS:
            assert tool["inputSchema"]["type"] == "object" and tool["inputSchema"]["additionalProperties"] is False
            assert tool["description"] and set(tool["inputSchema"].get("required", [])) <= set(tool["inputSchema"]["properties"])
    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "")
    hidden = {t["name"] for t in rpc(mcp, "tools/list")["tools"]}
    assert not set(NEW_TOOLS) & hidden, "every write tool is hidden while writes are disabled"
    assert {t.name for t in operator_tools.READ_TOOLS} <= hidden
    is_error, text = call(mcp, "people_create", email="amy@example.test")
    assert is_error and "writes are disabled" in text and count(Person) == 0


def test_every_screen_tool_is_a_write_tool_with_the_area_prefix_and_the_moves_mirror_the_screens():
    prefixes = ("people_", "mentee_", "grant_", "laps_", "acquisition_", "programme_", "territory_", "franchise_", "seller_")
    assert len(NEW_TOOLS) == 50
    for tool in operator_writes.WRITE_TOOLS:
        assert tool.write and tool.name.startswith(prefixes) and operator_tools.get(tool.name) is tool
    assert operator_writes.LAPS_MOVES == list(laps_router.MOVES)
    for name in ("grant_recompute_all", "grant_pause", "grant_resume", "mentee_recompute", "mentee_revoke_all", "mentee_pause_all"):
        assert "real access" in operator_tools.get(name).description
    for name in ("mentee_record_payment", "mentee_clear_payment"):
        assert "no money moves" in operator_tools.get(name).description


# ---- people -------------------------------------------------------------------------------------------------------
def test_people_create(mcp):
    data = ok(mcp, "people_create", email="  Amy@Example.test ", name="Amy Archer", phone="07700 900000")
    assert data["message"] == "amy@example.test added to the register."
    person = get(Person, data["person_id"])
    assert (person.person_key, person.name, person.phone) == ("amy@example.test", "Amy Archer", "07700 900000")
    assert actions(f"person:{person.id}") == ["person.create"]
    refused(mcp, "people_create", "That email address does not look right", email="not-an-email")
    refused(mcp, "people_create", f"is already on the register as person #{person.id}", email="AMY@example.test")
    assert count(Person) == 1


def test_people_update(mcp):
    pid = make_person()
    other = make_person("bob@example.test", "Bob")
    data = ok(mcp, "people_update", person_id=pid, phone="01234 567890")
    assert data["message"] == "Person saved."
    person = get(Person, pid)
    assert (person.person_key, person.name, person.phone) == ("amy@example.test", "Amy Archer", "01234 567890"), "left out is kept"
    assert ok(mcp, "people_update", person_id=pid)["message"] == "No changes to save."
    refused(mcp, "people_update", f"already belongs to person #{other}", person_id=pid, email="bob@example.test")
    refused(mcp, "people_update", "Person 999 was not found", person_id=999, name="X")
    assert get(Person, pid).person_key == "amy@example.test"


def test_people_set_links(mcp):
    pid = make_person(kp_user_id=5)
    data = ok(mcp, "people_set_links", person_id=pid, hnh_partner_id=42, pp_account_id="9b1d-77")
    assert data["message"] == "Linked ids saved."
    person = get(Person, pid)
    assert (person.hnh_partner_id, person.pp_account_id, person.kp_user_id) == (42, "9b1d-77", 5), "left out is kept"
    ok(mcp, "people_set_links", person_id=pid, kp_user_id="")
    assert get(Person, pid).kp_user_id is None, "an empty string clears"
    refused(mcp, "people_set_links", "Key Properties user id must be a whole number", person_id=pid, kp_user_id="abc")
    assert get(Person, pid).hnh_partner_id == 42


# ---- mentees ------------------------------------------------------------------------------------------------------
def test_mentee_create(mcp):
    pid = make_person()
    data = ok(mcp, "mentee_create", person_id=pid, programme_id=programme_id(), payment_mode="monthly", start_date=TODAY.isoformat(),
              cohort="Nov 26")
    mentee = get(Mentee, data["mentee_id"])
    assert (mentee.person_id, mentee.payment_mode, mentee.status, mentee.cohort) == (pid, "monthly", "active", "Nov 26")
    assert "enrolled on" in data["message"] and actions(f"mentee:{mentee.id}") == ["mentee.create"]
    refused(mcp, "mentee_create", "Choose how they pay", person_id=pid, programme_id=programme_id(), start_date=TODAY.isoformat())
    refused(mcp, "mentee_create", "is already active on", person_id=pid, programme_id=programme_id(), payment_mode="full",
            start_date=TODAY.isoformat())
    assert count(Mentee) == 1


def test_mentee_record_and_clear_payment(mcp):
    mid = make_mentee(make_person())
    data = ok(mcp, "mentee_record_payment", mentee_id=mid, amount="1,250.50", paid_at=TODAY.isoformat(), reference="BACS 1")
    payment = get(Payment, data["payment_id"])
    assert str(payment.amount) == "1250.50" and payment.cleared_at is None and payment.method == "manual"
    assert data["message"] == "Payment of £1,250.50 recorded. Mark it cleared once the money has arrived."
    refused(mcp, "mentee_record_payment", "Commas and spaces may only group thousands", mentee_id=mid, amount="12,5",
            paid_at=TODAY.isoformat())
    assert count(Payment) == 1

    refused(mcp, "mentee_clear_payment", "The cleared date does not look like a date", mentee_id=mid, payment_id=payment.id,
            cleared_at="soon")
    assert get(Payment, payment.id).cleared_at is None
    data = ok(mcp, "mentee_clear_payment", mentee_id=mid, payment_id=payment.id, cleared_at=TODAY.isoformat())
    assert "marked cleared" in data["message"] and get(Payment, payment.id).cleared_at is not None
    assert actions(f"payment:{payment.id}") == ["payment.create", "payment.clear"]


def test_mentee_recompute_revoke_pause_and_resume(mcp):
    pid = make_person()
    mid = make_mentee(pid)
    data = ok(mcp, "mentee_recompute", mentee_id=mid)
    assert data["message"].startswith("Recompute run for amy@example.test.") and "mentee.recompute" in actions(f"mentee:{mid}")
    refused(mcp, "mentee_recompute", "Mentee 999 was not found", mentee_id=999)

    with pytest.MonkeyPatch.context() as engine:
        engine.setattr(mentees_router, "pause_person", None)
        refused(mcp, "mentee_pause_all", "The entitlement engine is not available", mentee_id=mid, reason="x")
    assert get(Person, pid).access_held_at is None

    refused(mcp, "mentee_resume_all", "Access for amy@example.test is not paused.", mentee_id=mid)
    data = ok(mcp, "mentee_pause_all", mentee_id=mid, reason="Contract unsigned")
    person = get(Person, pid)
    assert person.access_held_at is not None and person.access_held_reason == "Contract unsigned"
    assert "All access paused for amy@example.test" in data["message"]
    data = ok(mcp, "mentee_resume_all", mentee_id=mid)
    assert get(Person, pid).access_held_at is None and "decisions" in data
    assert actions(f"mentee:{mid}")[-2:] == ["mentee.pause_all", "mentee.resume_all"]

    refused(mcp, "mentee_revoke_all", "Mentee 998 was not found", mentee_id=998)
    data = ok(mcp, "mentee_revoke_all", mentee_id=mid, reason="Left the programme")
    assert get(Mentee, mid).status == "ended" and "Revoke requested for every grant" in data["message"]
    assert actions(f"mentee:{mid}")[-1] == "mentee.revoke_all"


def test_mentee_update(mcp):
    mid = make_mentee(make_person())
    data = ok(mcp, "mentee_update", mentee_id=mid, cohort="Spring", status="ended")
    mentee = get(Mentee, mid)
    assert (mentee.cohort, mentee.status, mentee.payment_mode) == ("Spring", "ended", "full"), "left out is kept"
    assert data["message"] == "Mentee saved. Run Recompute now on the Grants tab so the products follow."
    refused(mcp, "mentee_update", "Lapsed is worked out from the payments", mentee_id=mid, status="lapsed")
    refused(mcp, "mentee_update", "The start date does not look like a date", mentee_id=mid, start_date="whenever")
    assert get(Mentee, mid).status == "ended"


# ---- grants -------------------------------------------------------------------------------------------------------
def test_grant_recompute_all_and_person(mcp):
    pid = make_person()
    mid = make_mentee(pid)
    with session() as db:
        db.add(Payment(mentee_id=mid, amount=1000, paid_at=utcnow(), cleared_at=utcnow(), method="manual"))
        db.commit()
    refused(mcp, "grant_recompute_all", "The date must look like 2026-11-07.", dry_run=True, today="not-a-date")
    assert count(EntitlementGrant) == 0
    data = ok(mcp, "grant_recompute_all", dry_run=True)
    assert data["dry_run"] is True and data["message"].startswith(f"recompute {TODAY.isoformat()} (DRY RUN")
    assert count(EntitlementGrant) >= 1 and data["decisions_total"] >= 1
    grant_id = data["decisions"][0]["grant_id"]
    data = ok(mcp, "grant_recompute_person", grant_id=grant_id, dry_run=True)
    assert data["person_id"] == pid and data["people"] == 1
    refused(mcp, "grant_recompute_person", "That grant does not exist.", grant_id=99999)


def test_grant_retry_pause_and_resume(mcp):
    pid = make_person()
    active = make_grant(pid, state="active")
    failed = make_grant(pid, state="failed", last_error="boom", product="ordris", kind="org_plan", value="starter")
    refused(mcp, "grant_retry", "Only a failed or blocked grant can be retried.", grant_id=active)
    data = ok(mcp, "grant_retry", grant_id=failed, dry_run=True)
    assert get(EntitlementGrant, failed).last_error == "" and "grant.retry" in actions(f"grant:{failed}")
    assert data["grant_id"] == failed and data["dry_run"] is True

    refused(mcp, "grant_resume", "That grant is not paused.", grant_id=active)
    refused(mcp, "grant_pause", "That grant does not exist.", grant_id=99999)
    data = ok(mcp, "grant_pause", grant_id=active, reason="Chasing the contract")
    grant = get(EntitlementGrant, active)
    assert grant.held_at is not None and grant.held_reason == "Chasing the contract" and data["message"].startswith("Paused")
    assert "already paused" in ok(mcp, "grant_pause", grant_id=active)["message"]
    data = ok(mcp, "grant_resume", grant_id=active)
    assert get(EntitlementGrant, active).held_at is None and data["message"].startswith("recompute")
    assert "grant.resume" in actions(f"grant:{active}")


# ---- LAPS ---------------------------------------------------------------------------------------------------------
def test_laps_refresh(mcp, monkeypatch):
    refused(mcp, "laps_refresh", "No CRM answered")
    monkeypatch.setattr(inbox_crm, "command", lambda asker, brand, verb, **kw: {"leads": [], "next_cursor": None})
    data = ok(mcp, "laps_refresh")
    assert data["message"].startswith("Leads refreshed: 0 CRM contacts checked")


def test_laps_create_add_business_edit_move_and_targets(mcp):
    refused(mcp, "laps_create_deals", "Choose which business this lead is for.", name="Sam Seller", brands=[])
    refused(mcp, "laps_create_deals", "Give the lead a name.", name=" ", brands=["mhp"])
    assert count(LapsDeal) == 0
    data = ok(mcp, "laps_create_deals", name="Sam  Seller", brands=["mhp", "kp"], value="1,500", stage="appointment")
    assert data["message"] == "Sam Seller added to appointments for 2 businesses."
    first, second = (get(LapsDeal, i) for i in data["deal_ids"])
    assert first.party and first.party == second.party and {first.brand, second.brand} == {"mhp", "kp"}
    assert first.stage == "appointment" and first.value_pence == 150_000

    refused(mcp, "laps_add_business", "Choose a business this person is not already under.", deal_id=first.id, brands=["kp"])
    data = ok(mcp, "laps_add_business", deal_id=first.id, brands=["pp", "kp"])
    [twin] = data["new_deal_ids"]
    assert get(LapsDeal, twin).brand == "pp" and get(LapsDeal, twin).party == first.party

    refused(mcp, "laps_edit_deal", "Enter the next step date as a date.", deal_id=first.id, next_step_on="soon")
    ok(mcp, "laps_edit_deal", deal_id=first.id, offer="Mentorship", next_step_on="2026-12-01")
    deal = get(LapsDeal, first.id)
    assert (deal.offer, deal.next_step_on, deal.name, deal.value_pence) == ("Mentorship", date(2026, 12, 1), "Sam Seller", 150_000)
    assert actions(f"laps_deal:{first.id}") == ["laps.deal.add", "laps.deal.edit"]

    refused(mcp, "laps_move_deal", "Choose where to move this lead.", deal_id=first.id, to="nowhere")
    data = ok(mcp, "laps_move_deal", deal_id=first.id, to="sale", value="2000")
    assert get(LapsDeal, first.id).stage == "sale" and data["stage"] == "sale" and get(LapsDeal, first.id).value_pence == 200_000

    refused(mcp, "laps_set_targets", "Enter the amount in pounds, for example 1500 or 1500.00.", monthly_goal="lots")
    data = ok(mcp, "laps_set_targets", monthly_goal="10000", typical_sale="2,500")
    assert (data["monthly_goal_pence"], data["typical_sale_pence"]) == (1_000_000, 250_000)
    ok(mcp, "laps_set_targets", typical_sale="3000")
    with session() as db:
        assert laps_service.targets(db) == {"monthly_goal_pence": 1_000_000, "typical_sale_pence": 300_000}


# ---- property deals ---------------------------------------------------------------------------------------------
def test_acquisition_create_edit_move_and_checklist(mcp):
    refused(mcp, "acquisition_create", "Give the deal an address or a working name.", title=" ")
    refused(mcp, "acquisition_create", "Refresh the inbox before continuing.", title="12 High St", thread_ticket="stale")
    assert count(Acquisition) == 0
    data = ok(mcp, "acquisition_create", title="12 High Street", kind="house", asking="250,000", postcode="sp10 1aa", units=1)
    deal = get(Acquisition, data["acquisition_id"])
    assert (deal.title, deal.kind, deal.asking_pence, deal.postcode, deal.stage) == ("12 High Street", "house", 25_000_000, "SP10 1AA", "lead")
    assert data["message"] == "12 High Street added to new leads."

    refused(mcp, "acquisition_edit", "Enter the number of properties or units as a whole number.", deal_id=deal.id, units="lots")
    ok(mcp, "acquisition_edit", deal_id=deal.id, seller_name="Pat Owner", works=15000)
    deal = get(Acquisition, deal.id)
    assert (deal.seller_name, deal.works_pence, deal.asking_pence, deal.postcode, deal.units) == ("Pat Owner", 1_500_000, 25_000_000, "SP10 1AA", 1)
    assert actions(f"acquisition:{deal.id}") == ["acquisition.add", "acquisition.edit"]

    refused(mcp, "acquisition_move", "Choose where to move this deal.", deal_id=deal.id, to="moon")
    data = ok(mcp, "acquisition_move", deal_id=deal.id, to="offer", price="230000")
    assert data["stage"] == "offer" and get(Acquisition, deal.id).offer_pence == 23_000_000
    assert data["message"] == "12 High Street moved to offer made."

    refused(mcp, "acquisition_toggle_check", "Unknown checklist item", deal_id=deal.id, key="nope")
    data = ok(mcp, "acquisition_toggle_check", deal_id=deal.id, key="replied")
    assert data["ticked"] is True and "replied" in get(Acquisition, deal.id).checks
    assert ok(mcp, "acquisition_toggle_check", deal_id=deal.id, key="replied")["ticked"] is False


@pytest.mark.usefixtures("connected")
def test_acquisition_link_and_unlink(mcp):
    with session() as db:
        _, first = threads.ticket(db)
    data = ok(mcp, "acquisition_create", title="Portfolio of six", thread_ticket=first)
    assert [c["name"] for c in data["conversations"]] == ["Sample Landlord"]
    other = ok(mcp, "acquisition_create", title="Second deal")["acquisition_id"]
    with session() as db:
        _, again = threads.ticket(db)
    refused(mcp, "acquisition_link", "This conversation is already on Portfolio of six.", deal_id=data["acquisition_id"], thread_ticket=again)
    linked = ok(mcp, "acquisition_link", deal_id=other, thread_ticket=again)
    assert linked["message"].startswith("Conversation added to Second deal.") and count(AcquisitionThread) == 2
    [conversation] = linked["conversations"]
    refused(mcp, "acquisition_unlink", "That conversation is not on this deal.", deal_id=other, account_key="kp-fb", thread_sha256="0" * 64)
    ok(mcp, "acquisition_unlink", deal_id=other, account_key=conversation["account_key"], thread_sha256=conversation["thread_sha256"])
    assert count(AcquisitionThread) == 1 and actions(f"acquisition:{other}")[-1] == "acquisition.detach"


# ---- programmes ---------------------------------------------------------------------------------------------------
def test_programme_update(mcp):
    pid = programme_id()
    before = get(Programme, pid)
    refused(mcp, "programme_update", "Item 1: product must be one of", programme_id=pid, grant_template=[{"product": "xx"}])
    refused(mcp, "programme_update", "The price must be pounds and pence", programme_id=pid, price="a lot")
    assert get(Programme, pid).grant_template == before.grant_template and get(Programme, pid).price == before.price
    template = [{"product": "pp", "kind": "comp_tier", "value": "pro_buyer", "months": 12}]
    data = ok(mcp, "programme_update", programme_id=pid, grant_template=template, price=4995)
    after = get(Programme, pid)
    assert after.grant_template == template and str(after.price) == "4995.00" and after.name == before.name and after.active == before.active
    assert "Existing mentees pick up the new template" in data["message"]
    assert actions(f"programme:{pid}") == ["programme.update"]


# ---- territories --------------------------------------------------------------------------------------------------
def test_territory_reserve_assign_release_and_hq(mcp):
    seed_districts()
    refused(mcp, "territory_reserve", "SA1 is kept for head office", district="SA1", partner_id=7)
    data = ok(mcp, "territory_reserve", district="sp10", partner_id=7)
    row = get(PostcodeDistrict, "SP10")
    assert (row.state, row.partner_id, data["state"]) == ("reserved", 7, "reserved") and row.reserved_until > TODAY
    refused(mcp, "territory_assign", "already a core district held by partner 5", district="SE1", partner_id=7)
    ok(mcp, "territory_assign", district="SP11", partner_id=7)
    assert (get(PostcodeDistrict, "SP11").state, get(PostcodeDistrict, "SP11").partner_id) == ("core", 7)
    refused(mcp, "territory_keep_for_hq", "SE1 is held by partner 5. Release it first.", district="SE1")
    refused(mcp, "territory_release", "ZZ99 is not in the territory model", district="ZZ99")
    data = ok(mcp, "territory_release", district="SE1")
    assert get(PostcodeDistrict, "SE1").state == "open" and "released" in data["message"]
    ok(mcp, "territory_keep_for_hq", district="DH7")
    assert get(PostcodeDistrict, "DH7").state == "hq"
    assert actions("district:SP10") == ["territory.reserve"] and actions("district:DH7") == ["territory.hq"]


# ---- franchise ----------------------------------------------------------------------------------------------------
def test_franchise_create_details_activity_requote_and_stages(mcp):
    seed_districts()
    refused(mcp, "franchise_create", "Not available: SA1", email="bea@example.test", districts=["SA1"])
    assert count(FranchiseOpportunity) == 0 and count(Person) == 0
    data = ok(mcp, "franchise_create", email="bea@example.test", name="Bea Buyer", districts=["SP10", "sp11"])
    oid = data["opportunity_id"]
    opportunity = get(FranchiseOpportunity, oid)
    assert opportunity.districts == ["SP10", "SP11"] and opportunity.stage == "enquiry" and data["message"].startswith(f"Opportunity #{oid} created")

    refused(mcp, "franchise_update_details", "No seller has the code NOPE.", opportunity_id=oid, seller_code="NOPE")
    ok(mcp, "franchise_update_details", opportunity_id=oid, notes="Met at the event")
    assert get(FranchiseOpportunity, oid).notes == "Met at the event"

    refused(mcp, "franchise_add_activity", "Write what happened.", opportunity_id=oid, kind="call", body=" ")
    data = ok(mcp, "franchise_add_activity", opportunity_id=oid, kind="call", body="Talked through the area")
    assert get(Activity, data["activity_id"]).kind == "call"

    data = ok(mcp, "franchise_requote", opportunity_id=oid, fee_waived=True)
    assert get(FranchiseOpportunity, oid).quoted_upfront_pence == 0 and data["message"].startswith("Requoted")

    refused(mcp, "franchise_move_stage", "Choose a stage.", opportunity_id=oid, stage="space")
    ok(mcp, "franchise_move_stage", opportunity_id=oid, stage="pack_sent")
    assert get(FranchiseOpportunity, oid).stage == "pack_sent"
    with session() as db:
        tasks = [t.id for t in db.query(Task).filter(Task.opportunity_id == oid).order_by(Task.id)]
    assert len(tasks) == 4
    refused(mcp, "franchise_task_done", "Task 99999 was not found on this opportunity.", opportunity_id=oid, task_id=99999)
    assert ok(mcp, "franchise_task_done", opportunity_id=oid, task_id=tasks[0])["message"].startswith("Done:")
    refused(mcp, "franchise_task_skip", "Opportunity 99999", opportunity_id=99999, task_id=tasks[1])
    assert ok(mcp, "franchise_task_skip", opportunity_id=oid, task_id=tasks[1])["message"].startswith("Skipped:")
    assert [get(Task, t).state for t in tasks[:2]] == ["done", "skipped"]

    ok(mcp, "franchise_move_stage", opportunity_id=oid, stage="agreement")
    refused(mcp, "franchise_requote", "keeps the quote it was sold on", opportunity_id=oid)
    refused(mcp, "franchise_create_hnh_partner", "Move the opportunity to Paid or Onboarding first", opportunity_id=oid)
    ok(mcp, "franchise_move_stage", opportunity_id=oid, stage="paid", note="Fees waived")
    data = ok(mcp, "franchise_create_hnh_partner", opportunity_id=oid)
    call_row = get(ConnectorCall, data["connector_call_id"])
    assert data["dry_run"] is True and call_row.status == "pending" and call_row.product == "hnh"
    assert "franchise.hnh_partner" in actions(f"opportunity:{oid}")


def test_franchise_payments(mcp):
    seed_districts()
    oid = ok(mcp, "franchise_create", email="cal@example.test", districts=["SP10"])["opportunity_id"]
    refused(mcp, "franchise_move_stage", "needs a cleared upfront payment", opportunity_id=oid, stage="onboarding")
    tomorrow = (TODAY + timedelta(days=1)).isoformat()
    refused(mcp, "franchise_record_payment", "The paid date cannot be in the future.", opportunity_id=oid, amount="13000",
            paid_at=tomorrow)
    assert count(FranchisePayment) == 0
    data = ok(mcp, "franchise_record_payment", opportunity_id=oid, amount=13000, paid_at=TODAY.isoformat())
    pid = data["payment_id"]
    assert get(FranchisePayment, pid).amount_pence == 1_300_000 and data["cleared"] is False
    refused(mcp, "franchise_clear_payment", "The cleared date cannot be in the future.", opportunity_id=oid, payment_id=pid, cleared_at=tomorrow)
    assert get(FranchisePayment, pid).cleared_at is None
    data = ok(mcp, "franchise_clear_payment", opportunity_id=oid, payment_id=pid)
    assert get(FranchisePayment, pid).cleared_at is not None and "No seller is attributed" in data["message"]
    refused(mcp, "franchise_refund_payment", "Say why the fee is being refunded", opportunity_id=oid, payment_id=pid)
    assert get(FranchisePayment, pid).refunded_at is None
    data = ok(mcp, "franchise_refund_payment", opportunity_id=oid, payment_id=pid, reason="Changed their mind")
    assert get(FranchisePayment, pid).refunded_at is not None and "marked refunded" in data["message"]


# ---- sellers ------------------------------------------------------------------------------------------------------
def test_seller_create_and_update(mcp):
    refused(mcp, "seller_create", "Say what kind of seller they are", email="sam@example.test", kind="boss")
    assert count(Seller) == 0 and count(Person) == 0
    data = ok(mcp, "seller_create", email="sam@example.test", name="Sam Seller", kind="mentee", code="SAM1")
    seller = get(Seller, data["seller_id"])
    assert (seller.code, seller.kind, seller.active, seller.self_billing_agreed_at) == ("SAM1", "mentee", True, None)
    other = ok(mcp, "seller_create", email="olly@example.test", kind="outsider")["seller_id"]
    refused(mcp, "seller_update", "The code SAM1 is already in use.", seller_id=other, code="SAM1")
    ok(mcp, "seller_update", seller_id=data["seller_id"], bank_reference="Sort 00-00-00", self_billing=True,
       self_billing_date="2026-09-01")
    seller = get(Seller, data["seller_id"])
    assert seller.bank_reference == "Sort 00-00-00" and seller.self_billing_agreed_at == datetime(2026, 9, 1) and seller.code == "SAM1"
    assert actions(f"seller:{seller.id}") == ["seller.create", "seller.update"]


def test_seller_statements_clawback_and_rules(mcp, admin_client):
    sam, _, sam_oid = _ledger_for_statements(admin_client)
    refused(mcp, "seller_generate_statements", "has not started", period="2099-01")
    assert count(Statement) == 0
    data = ok(mcp, "seller_generate_statements", period="2026-09")
    assert "1 statement generated" in data["message"] and count(Statement) == 1
    with session() as db:
        sid = db.query(Statement).one().id
    refused(mcp, "seller_mark_statement_paid", "a commission rule the founder has not confirmed", statement_id=sid, paid_at="2026-10-18")
    assert get(Statement, sid).paid_at is None

    refused(mcp, "seller_confirm_rule", "Commission rule 99999 was not found.", rule_id=99999)
    with session() as db:
        tr.ensure_commission_rules(db)
        db.commit()
        rule_ids = [r.id for r in db.query(CommissionRule).order_by(CommissionRule.id)]
    data = ok(mcp, "seller_confirm_rule", rule_id=rule_ids[0])
    assert "confirmed by founder@example.test" in data["message"]
    confirm_rules(admin_client)
    data = ok(mcp, "seller_mark_statement_paid", statement_id=sid, paid_at="2026-10-18")
    assert get(Statement, sid).paid_at == datetime(2026, 10, 18) and data["message"].startswith(f"Statement #{sid} marked paid")

    original = entries_for(sam_oid)[0]
    refused(mcp, "seller_clawback_entry", "Entry 99999 was not found on this seller.", seller_id=sam, entry_id=99999, reason="x")
    data = ok(mcp, "seller_clawback_entry", seller_id=sam, entry_id=original.id, reason="Refunded")
    assert count(CommissionEntry, CommissionEntry.clawback_of == original.id) == 1 and data["message"].startswith("Clawed back")


# ---- the routes whose logic moved into shared functions answer as before -------------------------------------------------
def test_routes_still_answer_as_before(admin_client):
    r = admin_client.post("/people/new", data={"email": "bad", "name": "", "phone": "", "notes": ""})
    assert r.status_code == 400 and "That email address does not look right" in r.text
    r = admin_client.post("/people/new", data={"email": "dee@example.test", "name": "Dee", "phone": "", "notes": ""})
    assert r.status_code == 303 and flash_of(r.headers["location"]) == {"ok": "dee@example.test added to the register."}
    pid = int(r.headers["location"].split("?")[0].rsplit("/", 1)[1])
    r = admin_client.post(f"/people/{pid}/links", data={"kp_user_id": "x"})
    assert r.status_code == 400 and "must be a whole number" in r.text
    r = admin_client.post("/mentees/new", data={"person_id": str(pid), "programme_id": str(programme_id()), "payment_mode": "",
                                                "start_date": TODAY.isoformat(), "cohort": "", "notes": ""})
    assert r.status_code == 400 and "Choose how they pay" in r.text
    r = admin_client.post("/laps/deals", data={"name": "Lee", "view": "kp"})
    assert r.status_code == 303 and r.headers["location"].startswith("/laps?brand=kp&err=") and r.headers["location"].endswith("#add")
    r = admin_client.post("/acquisitions/deals", data={"title": ""})
    assert r.status_code == 303 and flash_of(r.headers["location"].split("#")[0])["err"] == "Give the deal an address or a working name."
    mid = make_mentee(pid)
    r = admin_client.post(f"/mentees/{mid}/resume")
    assert r.status_code == 303 and flash_of(r.headers["location"])["err"] == "Access for dee@example.test is not paused."
    grant = make_grant(pid, state="active")
    r = admin_client.post(f"/grants/{grant}/retry")
    assert r.status_code == 303 and flash_of(r.headers["location"])["err"] == "Only a failed or blocked grant can be retried."
    r = admin_client.post(f"/grants/{grant}/resume")
    assert r.status_code == 303 and flash_of(r.headers["location"])["err"] == "That grant is not paused."
    r = admin_client.post("/grants/recompute", data={"dry_run": "1", "today": "nope"})
    assert r.status_code == 400
    r = admin_client.post(f"/programmes/{programme_id()}", data={"name": "", "grant_template": "[]"})
    assert r.status_code == 400 and "A name is required." in r.text
    seed_districts()
    r = admin_client.post("/territories/SA1/reserve", data={"partner_id": "7"})
    assert r.status_code == 400 and "kept for head office" in r.text
    r = admin_client.post("/franchise/new", data={"email": "eve@example.test", "districts": ["SA1"]})
    assert r.status_code == 400 and "Not available: SA1" in r.text
    r = admin_client.post("/sellers/new", data={"email": "fay@example.test", "kind": "boss"})
    assert r.status_code == 400 and "Say what kind of seller they are" in r.text
