"""Mentee earnings: the affiliate grant kind and the four contracts, the Mentorship template's data step, the order of a
person's recompute, the Key Properties link and its mismatch, the seller every active mentee gets, the earning links
panel (with each product failing in turn), the WhatsApp welcome and the "Needs a region" queue.

Every product answer is a recorded fixture in tests/fixtures/mentee_earnings, served by respx; nothing leaves the
machine. The clock the engine's hard floor reads is pinned to TODAY, as in tests/test_entitlements.py."""
from __future__ import annotations

import importlib.util
import json
import re
import time
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from markupsafe import escape

from app import config, models, seed
from app.db import engine
from app.models import AuditLog, ConnectorCall, EntitlementGrant, Mentee, Payment, Person, Programme, Seller
from app.routers.programmes import parse_grant_template
from app.services import earnings
from app.services import entitlements as ent
from app.services.connectors import ConnectorError, HNHConnector, KPConnector, OrdrisConnector, PPConnector
from app.services.connectors.hnh import partner_info
from app.services.connectors.kp import link_refusal
from app.services.connectors.ordris import partner_info as ordris_partner_info
from app.services.connectors.ordris import partner_terms as ordris_partner_terms
from app.services.connectors.pp import AFFILIATE_KEPT
from app.services.entitlements import (IN_USE_CREATE_ERROR, IN_USE_ERROR, LINK_UNCONFIRMED, MAX_ATTEMPTS, MISMATCH_ERROR,
                                       RETRY_PRESSED, display_state, link_key, link_refusal_heading, link_refusal_note,
                                       recompute_person, reset_for_retry, revoke_person)

TODAY = date(2026, 10, 20)
PP = "https://pp.example.test"
ORDRIS = "https://ordris.example.test"
KP = "https://kp.example.test"
HNH = "https://hnh.example.test"
FIXTURES = Path(__file__).parent / "fixtures" / "mentee_earnings"
EMAIL = "amy@example.test"
# Property Predator's users.id is str(uuid.uuid4()), and every PP route answers it as that string.
ACCOUNT = "3f6c1a2e-8d4b-4c1e-9a7f-2b5d6e8f9a01"
OTHER_ACCOUNT = "9b2e7d41-5c3a-4f8e-b1d6-0a4c8e2f7b63"
THIRD_ACCOUNT = "c4d8a2f0-1e6b-4a97-8c35-7f0b2d9e6a14"
COMP_OK = {"ok": True, "account_id": ACCOUNT, "tier": "pro_buyer", "comp_expires_at": "2027-10-01T00:00:00+00:00", "email_verified": True}
NOT_FOUND = {"detail": "Not Found"}         # FastAPI's answer for a route that is not there
ORDRIS_PLAN_OK = {"ok": True, "user_id": "user_1", "org_id": "org_1", "plan": "starter"}


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def body_of(call) -> dict:
    return json.loads(call.request.content)


@pytest.fixture
def live(monkeypatch):
    """Every product live by the environment (conftest forces dry run), with the hard floor's clock at TODAY."""
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset())
    monkeypatch.setattr(ent, "real_today", lambda: TODAY)


def fake_person(**kw):
    base = {"id": 1, "person_key": EMAIL, "name": "Amy Archer", "pp_account_id": None, "organisation_links": []}
    base.update(kw)
    return SimpleNamespace(**base)


def fake_grant(**kw):
    base = {"id": 12, "product": "pp", "kind": "affiliate", "value": "affiliate", "ends_at": datetime(2027, 10, 1),
            "remote_ref": None, "reason": "Mentorship, paid in full", "source": "programme:mentorship"}
    base.update(kw)
    return SimpleNamespace(**base)


# ---- builders ---------------------------------------------------------------------------------------------------
def make_person(db, email=EMAIL, name="Amy Archer", **kw) -> Person:
    person = Person(person_key=email, name=name, **kw)
    db.add(person)
    db.commit()
    return person


def programme(db, code="mentorship", template=None) -> Programme:
    row = db.query(Programme).filter(Programme.code == code).first()
    if row is None:
        row = Programme(code=code, name=code.replace("_", " ").title(), grant_template=template or [])
        db.add(row)
    elif template is not None:
        row.grant_template = template
    db.commit()
    return row


def enrol_paid(db, person, prog, start=date(2026, 10, 1), status="active") -> Mentee:
    mentee = Mentee(person_id=person.id, programme_id=prog.id, payment_mode="full", start_date=start, status=status)
    db.add(mentee)
    db.commit()
    db.add(Payment(mentee_id=mentee.id, amount=Decimal("1000"), paid_at=datetime(2026, 10, 1, 9), cleared_at=datetime(2026, 10, 1, 10)))
    db.commit()
    return mentee


def grants_of(db, person) -> dict[tuple[str, str], EntitlementGrant]:
    db.expire_all()
    return {(g.product, g.kind): g for g in db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id).all()}


def mock_mentorship(mock, *, hnh_lookup="hnh_partner_no_area", pp_lookup=None, kp=None):
    """Every call the seeded Mentorship template makes when all four products are live."""
    routes = SimpleNamespace()
    routes.pp_account = mock.get(f"{PP}/api/partner/grouphq/account").mock(
        return_value=pp_lookup or httpx.Response(200, json={"ok": True, "account_id": ACCOUNT, "tier": "free",
                                                            "subscription_status": None, "comp_expires_at": None}))
    routes.pp_comp = mock.post(f"{PP}/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
    routes.pp_flag = mock.post(f"{PP}/api/partner/grouphq/flags").mock(return_value=httpx.Response(200, json={"ok": True, "account_id": ACCOUNT}))
    routes.pp_affiliate = mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(
        return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
    routes.kp_post = mock.post(f"{KP}/api/partner/sourcers").mock(
        return_value=kp or httpx.Response(200, json=fixture("kp_sourcer_linked")))
    routes.ordris_plan = mock.post(f"{ORDRIS}/api/partner/grouphq/entitlements").mock(return_value=httpx.Response(200, json=ORDRIS_PLAN_OK))
    routes.ordris_partner = mock.post(f"{ORDRIS}/api/partner/grouphq/partners").mock(
        return_value=httpx.Response(200, json=fixture("ordris_partner_join")))
    routes.hnh_post = mock.post(f"{HNH}/api/partner/partners").mock(return_value=httpx.Response(201, json=fixture("hnh_partner_created")))
    routes.hnh_get = mock.get(f"{HNH}/api/partner/partners").mock(return_value=httpx.Response(200, json=fixture(hnh_lookup)))
    return routes


def call_order(mock) -> list[str]:
    return [f"{c.request.method} {c.request.url.host.split('.')[0]} {c.request.url.path}" for c in mock.calls]


# ---- contract 1: Property Predator --------------------------------------------------------------------------------
def test_pp_affiliate_join_sends_the_contract_and_keeps_the_code_and_link():
    with respx.mock(base_url=PP) as mock:
        route = mock.post("/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
        result = PPConnector.from_config().grant(fake_grant(), fake_person(), reason="Group HQ: Mentorship (grant 12)")
    assert body_of(route.calls.last) == {"email": EMAIL, "reason": "Group HQ: Mentorship (grant 12)"}
    request = route.calls.last.request
    assert request.headers["x-partner-client"] == "grouphq" and request.headers["x-service-token"] == config.PP_SERVICE_TOKEN
    assert "origin" not in request.headers, "server to server: no Origin is ever sent"
    assert result.remote_ref == ACCOUNT and result.links == {"pp_account_id": ACCOUNT}, "the UUID is kept as text"
    assert result.info == {"account_id": ACCOUNT, "code": "AMY77", "link": "https://pp.example.test/?ref=AMY77", "created": True}
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(404, json=fixture("pp_no_account")))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().grant(fake_grant(), fake_person())
    assert raised.value.reason == "no_account" and raised.value.status == 404


def test_pp_and_ordris_affiliate_revokes_are_recorded_noops_and_never_a_call():
    with respx.mock(assert_all_called=False) as mock:
        for connector, product in ((PPConnector.from_config(), "pp"), (OrdrisConnector.from_config(), "ordris"),
                                   (PPConnector.from_config(dry_run=True), "pp"), (OrdrisConnector.from_config(dry_run=True), "ordris")):
            result = connector.revoke(fake_grant(product=product, remote_ref="77"), fake_person(), reason="lapsed")
            assert result.ok and result.noop == AFFILIATE_KEPT == "kept: public programme" and not result.plan
    assert mock.calls.call_count == 0


def test_pp_affiliate_status_reads_both_answers_and_no_account():
    with respx.mock(base_url=PP) as mock:
        route = mock.get("/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_status")))
        info = PPConnector.from_config().affiliate_status("Amy@Example.test ").info
        assert route.calls.last.request.url.params["email"] == EMAIL
        assert info == {"account_id": ACCOUNT, "affiliate": True, "code": "AMY77", "link": "https://pp.example.test/?ref=AMY77",
                        "referred_accounts": 3, "commission_due_pence": 2970, "commission_paid_pence": 1170}
        route.mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_none")))
        assert PPConnector.from_config().affiliate_status(EMAIL).info == {"account_id": ACCOUNT, "affiliate": False}
        route.mock(return_value=httpx.Response(404, json=fixture("pp_no_account")))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().affiliate_status(EMAIL)
        assert raised.value.reason == "no_account"
        # a hostile link is never kept
        route.mock(return_value=httpx.Response(200, json={**fixture("pp_affiliate_status"), "link": "javascript:alert(1)"}))
        assert "link" not in PPConnector.from_config().affiliate_status(EMAIL).info
    planned = PPConnector.from_config(dry_run=True).affiliate_status(EMAIL)
    assert planned.dry_run and planned.plan["url"] == f"{PP}/api/partner/grouphq/affiliate" and planned.info == {}


# ---- contract 2: Ordris ----------------------------------------------------------------------------------------------
def test_ordris_partner_join_and_status_follow_contract_two():
    grant = fake_grant(product="ordris", value="partner")
    with respx.mock(base_url=ORDRIS) as mock:
        post = mock.post("/api/partner/grouphq/partners").mock(return_value=httpx.Response(200, json=fixture("ordris_partner_join")))
        result = OrdrisConnector.from_config().grant(grant, fake_person())
        assert body_of(post.calls.last) == {"email": EMAIL, "name": "Amy Archer", "external_ref": "grouphq:grant:12"}
        assert post.calls.last.request.headers["authorization"] == f"Bearer {config.ORDRIS_PARTNER_TOKEN}"
        assert result.remote_ref == "41" and result.links == {}, "no Ordris login is needed or linked"
        assert result.info == {"partner_id": "41", "referral_code": "AMYA41", "link": "https://ordris.example.test/?ref=AMYA41",
                               "created": True}
        get = mock.get("/api/partner/grouphq/partners").mock(return_value=httpx.Response(200, json=fixture("ordris_partner_status")))
        info = OrdrisConnector.from_config().partner_status(EMAIL).info
        assert get.calls.last.request.url.params["email"] == EMAIL
        assert info["partner"] is True and info["referrals"] == 2 and info["pending_pence"] == 5000
        assert info["payable_pence"] == 1500 and info["paid_pence"] == 0
        get.mock(return_value=httpx.Response(200, json=fixture("ordris_partner_none")))
        assert OrdrisConnector.from_config().partner_status(EMAIL).info == {"partner": False}
        mock.post("/api/partner/grouphq/partners").mock(return_value=httpx.Response(200, json={"ok": False, "reason": "programme_missing"}))
        with pytest.raises(ConnectorError) as raised:
            OrdrisConnector.from_config().grant(grant, fake_person())
        assert raised.value.reason == "programme_missing"
    with pytest.raises(ConnectorError):
        KPConnector.from_config(dry_run=True).grant(fake_grant(product="kp"), fake_person())
    with pytest.raises(ConnectorError):
        HNHConnector.from_config(dry_run=True).grant(fake_grant(product="hnh"), fake_person())


# ---- contract 4: Key Properties ------------------------------------------------------------------------------------
def test_kp_link_carries_the_pp_id_on_post_and_patch_and_reads_contract_four():
    grant = fake_grant(product="kp", kind="sourcer_link", value="mentee")
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        result = KPConnector.from_config().grant(grant, fake_person(pp_account_id=ACCOUNT))
        assert body_of(post.calls.last)["pp_account_id"] == ACCOUNT, "the UUID goes to the portal as text"
        assert result.remote_ref == "41" and result.info == {"pp_account_id": ACCOUNT}
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        held = fake_grant(product="kp", kind="sourcer_link", value="mentee", remote_ref="41")
        KPConnector.from_config().grant(held, fake_person(pp_account_id=ACCOUNT), extra={"fee_share_pct": 40})
        assert body_of(patch.calls.last) == {"active": True, "fee_share_pct": 40, "pp_account_id": ACCOUNT}
        KPConnector.from_config().grant(held, fake_person())
        assert body_of(patch.calls.last) == {"active": True}, "no id known, none sent"
        linked = KPConnector.from_config().link(held, fake_person(pp_account_id=ACCOUNT))
        assert body_of(patch.calls.last) == {"pp_account_id": ACCOUNT}, "the link sends the id alone: never active or a share"
        assert linked.info == {"pp_account_id": ACCOUNT}
        with pytest.raises(ConnectorError):
            KPConnector.from_config().link(held, fake_person())          # nothing to link without an id
        patch.mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_mismatch")))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().grant(held, fake_person(pp_account_id=OTHER_ACCOUNT))
        assert raised.value.reason == "pp_account_mismatch" and raised.value.status == 409
        get = mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_status")))
        info = KPConnector.from_config().sourcer_status(EMAIL).info
        assert get.calls.last.request.url.params["email"] == EMAIL
        assert info == {"sourcer": True, "id": 41, "fee_share_pct": "50", "active": True, "pp_linked": True,
                        "login_url": "https://kp.example.test/login", "deals_submitted": 4, "earned_pence": 250000,
                        "pipeline_pence": 125000}
        get.mock(return_value=httpx.Response(404, json={"ok": False, "reason": "not_found", "error": "not_found",
                                                        "message": "No sourcer with that email."}))
        assert KPConnector.from_config().sourcer_status(EMAIL).info == {"sourcer": False}
        get.mock(return_value=httpx.Response(404, text="Cannot GET /api/partner/sourcers"))
        with pytest.raises(ConnectorError):
            KPConnector.from_config().sourcer_status(EMAIL)         # a 404 that is not the portal's refusal is a failure
        get.mock(return_value=httpx.Response(503, json={"ok": False, "error": "not_configured"}))
        with pytest.raises(ConnectorError):
            KPConnector.from_config().sourcer_status(EMAIL)


# ---- contract 3: Hello New Home -------------------------------------------------------------------------------------
def test_hnh_contract_three_is_read_with_its_area_or_its_need_for_one():
    with respx.mock(base_url=HNH) as mock:
        get = mock.get("/api/partner/partners").mock(return_value=httpx.Response(200, json=fixture("hnh_partner_status")))
        info = HNHConnector.from_config().partner_status(EMAIL).info
        assert get.calls.last.request.url.params["email"] == EMAIL
    assert info == {"found": True, "partner_id": 9, "referral_code": "AMY9", "referral_link": "https://hnh.example.test/?ref=AMY9",
                    "portal_url": "https://hnh.example.test/partners/login", "admin_url": "https://hnh.example.test/admin/partners/9",
                    "tier": "area", "status": "active", "area": {"slug": "swansea", "name": "Swansea"}, "area_known": True,
                    "needs_region": False}
    no_area = partner_info(fixture("hnh_partner_no_area"))
    assert no_area["area"] is None and no_area["needs_region"] is True
    introducer = partner_info({**fixture("hnh_partner_no_area"), "partner": {**fixture("hnh_partner_no_area")["partner"], "tier": "introducer"}})
    assert introducer["needs_region"] is False, "an introducer has no territory by design"
    paused = partner_info({**fixture("hnh_partner_no_area"), "partner": {**fixture("hnh_partner_no_area")["partner"], "status": "paused"}})
    assert paused["needs_region"] is False
    older = fixture("hnh_partner_created")["partner"]       # an HNH build without contract 3: neither area nor admin_url
    older_info = partner_info({"ok": True, "found": True, "partner": {k: v for k, v in older.items() if k != "areas"}})
    assert older_info["area_known"] is False and older_info["needs_region"] is False
    with_areas = partner_info({"ok": True, "found": True, "partner": {**older, "areas": [{"slug": "swansea", "name": "Swansea"}]}})
    assert with_areas["area"] == {"slug": "swansea", "name": "Swansea"} and with_areas["needs_region"] is False
    assert partner_info({"ok": True, "found": False, "partner": None}) == {"found": False}
    hostile = partner_info({"ok": True, "found": True, "partner": {**fixture("hnh_partner_no_area")["partner"],
                                                                   "admin_url": "javascript:alert(1)", "portal_url": "/relative"}})
    assert "admin_url" not in hostile and "portal_url" not in hostile


# ---- the programme template ------------------------------------------------------------------------------------------
def test_a_template_takes_one_affiliate_item_for_pp_and_ordris_only():
    items, problem = parse_grant_template(json.dumps([{"product": "pp", "kind": "affiliate", "value": "affiliate"},
                                                      {"product": "ordris", "kind": "affiliate", "value": "partner"}]))
    assert problem == "" and len(items) == 2
    _, problem = parse_grant_template(json.dumps([{"product": "kp", "kind": "affiliate", "value": "affiliate"}]))
    assert "Property Predator or Ordris only" in problem
    _, problem = parse_grant_template(json.dumps([{"product": "hnh", "kind": "affiliate", "value": "x"}]))
    assert "Property Predator or Ordris only" in problem
    _, problem = parse_grant_template(json.dumps([{"product": "pp", "kind": "affiliate", "value": "affiliate"},
                                                  {"product": "pp", "kind": "affiliate", "value": "again"}]))
    assert "once only" in problem


def test_the_seed_and_the_migration_add_the_same_two_items():
    migration = _migration()
    seeded = next(p for p in seed.PROGRAMMES if p["code"] == "mentorship")["grant_template"]
    assert seeded[-2:] == list(migration.AFFILIATE_ITEMS)
    assert migration.GRANT_KINDS == models.GRANT_KINDS and "affiliate" not in migration.GRANT_KINDS_BEFORE


# ---- the data step ----------------------------------------------------------------------------------------------------
def _migration():
    path = Path(config.ROOT) / "alembic" / "versions" / "0008_mentee_earnings.py"
    spec = importlib.util.spec_from_file_location("migration_0008_mentee_earnings", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


OLD_MENTORSHIP = [
    {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
    {"product": "ordris", "kind": "org_plan", "value": "starter"},
    {"product": "hnh", "kind": "partner_seat", "value": "area"},
    {"product": "kp", "kind": "sourcer_link", "value": "mentee", "fee_share_pct": 40},
    {"product": "pp", "kind": "flag", "value": "kpuk.submit"},
]


def test_the_data_step_appends_once_never_twice_and_keeps_edits(db):
    migration = _migration()
    mentorship = programme(db, template=list(OLD_MENTORSHIP))           # as production holds it, with an edit (fee_share_pct)
    with engine.begin() as conn:
        assert migration.append_affiliate_items(conn) == 1
    db.expire_all()
    template = db.get(Programme, mentorship.id).grant_template
    assert template == OLD_MENTORSHIP + list(migration.AFFILIATE_ITEMS), "appended at the end, everything else as it was"
    row = db.query(AuditLog).filter(AuditLog.action == "programme.update").one()
    assert row.actor == "migration:0008_mentee_earnings" and row.entity == f"programme:{mentorship.id}"
    assert row.after["appended"] == list(migration.AFFILIATE_ITEMS) and row.before["grant_template"] == OLD_MENTORSHIP

    with engine.begin() as conn:
        assert migration.append_affiliate_items(conn) == 0, "never twice"
    db.expire_all()
    assert db.get(Programme, mentorship.id).grant_template == template
    assert db.query(AuditLog).filter(AuditLog.action == "programme.update").count() == 1

    # the founder has edited the items: one renamed and given a term, the other removed. A rerun adds back only an item
    # of a product and kind the template has none of, and never touches the edited one.
    edited = OLD_MENTORSHIP + [{"product": "pp", "kind": "affiliate", "value": "pp-affiliates", "months": 6}]
    programme(db, template=edited)
    with engine.begin() as conn:
        assert migration.append_affiliate_items(conn) == 1
    db.expire_all()
    assert db.get(Programme, mentorship.id).grant_template == edited + [{"product": "ordris", "kind": "affiliate", "value": "partner"}]

    # other programmes are never touched, and a template that is not a list is left alone
    kp_only = db.query(Programme).filter(Programme.code == "kp_mentee").one()
    before = list(kp_only.grant_template)
    assert migration.with_affiliate_items({"not": "a list"}) == ({"not": "a list"}, [])
    with engine.begin() as conn:
        migration.append_affiliate_items(conn)
        assert migration.remove_affiliate_items(conn) == 1
    db.expire_all()
    assert db.query(Programme).filter(Programme.code == "kp_mentee").one().grant_template == before
    assert all(item["kind"] != "affiliate" for item in db.get(Programme, mentorship.id).grant_template)


# ---- the recompute: order, affiliate grants, the seller, the area -------------------------------------------------------
def test_a_mentorship_recompute_runs_pp_first_then_kp_with_the_id_then_the_rest(db, live):
    person = make_person(db)
    enrol_paid(db, person, programme(db))
    with respx.mock() as mock:
        routes = mock_mentorship(mock)
        decisions = recompute_person(db, person, TODAY, False)
        order = call_order(mock)
    assert order == [
        "GET pp /api/partner/grouphq/account",          # the lookup, because Key Properties will need the id
        "POST pp /api/partner/grouphq/comp",
        "POST pp /api/partner/grouphq/flags",
        "POST pp /api/partner/grouphq/affiliate",
        "POST kp /api/partner/sourcers",
        "POST ordris /api/partner/grouphq/entitlements",
        "POST hnh /api/partner/partners",
        "POST ordris /api/partner/grouphq/partners",
        "GET hnh /api/partner/partners",                # the area, kept for the queue
    ]
    assert body_of(routes.kp_post.calls.last)["pp_account_id"] == ACCOUNT, "the sourcer link carries the PP account, a UUID"
    assert body_of(routes.pp_affiliate.calls.last) == {"email": EMAIL, "reason": "Group HQ: Mentorship, paid in full (grant 3)"}
    assert body_of(routes.ordris_partner.calls.last) == {"email": EMAIL, "name": "Amy Archer", "external_ref": "grouphq:grant:7"}
    assert {(d.product, d.kind, d.outcome) for d in decisions} >= {("pp", "affiliate", "sent"), ("ordris", "affiliate", "sent")}
    grants = grants_of(db, person)
    pp_aff, ordris_aff = grants[("pp", "affiliate")], grants[("ordris", "affiliate")]
    assert pp_aff.state == "active" and pp_aff.remote_ref == ACCOUNT
    assert pp_aff.remote_info == {"account_id": ACCOUNT, "code": "AMY77", "link": "https://pp.example.test/?ref=AMY77", "created": True}
    assert ordris_aff.state == "active" and ordris_aff.remote_ref == "41" and ordris_aff.remote_info["referral_code"] == "AMYA41"
    assert grants[("kp", "sourcer_link")].remote_info == {"pp_account_id": ACCOUNT}
    hnh = grants[("hnh", "partner_seat")].remote_info
    assert hnh["needs_region"] is True and hnh["admin_url"] == "https://hnh.example.test/admin/partners/9" and hnh["checked_at"]
    db.refresh(person)
    assert person.pp_account_id == ACCOUNT
    assert db.query(AuditLog).filter(AuditLog.action == "person.links", AuditLog.entity == f"person:{person.id}").one().after["via"] == "recompute lookup"
    # a second run sends nothing new to a product but still reads the area
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_mentorship(mock, hnh_lookup="hnh_partner_status")
        recompute_person(db, person, TODAY, False)
        order = call_order(mock)
    assert order == ["GET hnh /api/partner/partners"]
    assert grants_of(db, person)[("hnh", "partner_seat")].remote_info["needs_region"] is False


def test_without_a_pp_account_the_comp_still_gives_kp_the_id(db, live):
    """The lookup finds nothing (404), the PP comp answers the account id, and the sourcer link sent after it carries it."""
    person = make_person(db)
    enrol_paid(db, person, programme(db))
    with respx.mock() as mock:
        routes = mock_mentorship(mock, pp_lookup=httpx.Response(404, json=fixture("pp_no_account")))
        decisions = recompute_person(db, person, TODAY, False)
    assert routes.pp_account.call_count == 1 and body_of(routes.kp_post.calls.last)["pp_account_id"] == ACCOUNT
    assert not any(d.kind == "lookup" for d in decisions), "no account is not a failure"
    # a lookup that fails is reported on the run, and the link is sent without the id
    other = make_person(db, "ben@example.test", "Ben Brook")
    enrol_paid(db, other, programme(db))
    with respx.mock() as mock:
        routes = mock_mentorship(mock, pp_lookup=httpx.Response(502, json={"error": "bad_gateway"}))
        routes.pp_comp.mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        routes.pp_flag.mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        routes.pp_affiliate.mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        decisions = recompute_person(db, other, TODAY, False)
    [lookup] = [d for d in decisions if d.kind == "lookup"]
    assert lookup.outcome == "skipped" and "account lookup failed" in lookup.detail and lookup.grant_id is None
    assert body_of(routes.kp_post.calls.last)["pp_account_id"] is None


def test_no_lookup_when_pp_is_in_dry_run_or_the_id_is_known(db, live, monkeypatch):
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db))
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_mentorship(mock)
        recompute_person(db, person, TODAY, False)
    assert routes.pp_account.call_count == 0 and body_of(routes.kp_post.calls.last)["pp_account_id"] == ACCOUNT
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset({"pp"}))
    other = make_person(db, "ben@example.test", "Ben Brook")
    enrol_paid(db, other, programme(db))
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_mentorship(mock)
        recompute_person(db, other, TODAY, False)
    assert routes.pp_account.call_count == 0 and routes.pp_affiliate.call_count == 0


def test_an_active_sourcer_is_sent_the_pp_id_alone_until_key_properties_confirms_it(db, live):
    """A sourcer linked before Group HQ knew the PP account: the next recompute sends one link call, PATCH {pp_account_id}
    and nothing else, then nothing once Key Properties has confirmed the id."""
    person = make_person(db)
    enrol_paid(db, person, programme(db, "kp_only", [{"product": "kp", "kind": "sourcer_link", "value": "mentee"}]))
    with respx.mock() as mock:
        lookup = mock.get(f"{PP}/api/partner/grouphq/account").mock(return_value=httpx.Response(404, json=fixture("pp_no_account")))
        post = mock.post(f"{KP}/api/partner/sourcers").mock(return_value=httpx.Response(
            201, json={**fixture("kp_sourcer_linked"), "pp_account_id": None}))
        recompute_person(db, person, TODAY, False)
    assert lookup.call_count == 1, "Key Properties will need the id, so PP is asked first"
    [grant] = grants_of(db, person).values()
    assert grant.state == "active" and grant.remote_info == {} and body_of(post.calls.last)["pp_account_id"] is None
    person.pp_account_id = ACCOUNT
    db.commit()
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.action == "link" and decision.outcome == "sent" and decision.key == link_key(grant.id, ACCOUNT)
    assert body_of(patch.calls.last) == {"pp_account_id": ACCOUNT}, "never re-opens the sourcer or resets a share"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.remote_info == {"pp_account_id": ACCOUNT} and grant.ends_at == datetime(2027, 10, 1)
    with respx.mock(assert_all_called=False) as mock:
        [decision] = recompute_person(db, person, TODAY, False)
    assert mock.calls.call_count == 0 and decision.action == "noop"


def test_a_link_key_properties_does_not_confirm_is_tried_again_up_to_the_limit(db, live):
    """The portal build without contract 4 drops pp_account_id from a PATCH and answers 200: that is not a link, so it is
    a failed attempt, tried on the next runs until MAX_ATTEMPTS, and Retry starts again."""
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db, "kp_only", [{"product": "kp", "kind": "sourcer_link", "value": "mentee"}]))
    old_build = {**fixture("kp_sourcer_linked"), "pp_account_id": None}
    old_build.pop("pp_linked")
    with respx.mock(base_url=KP) as mock:
        mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(200, json=old_build))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.action == "grant" and decision.outcome == "sent", "the access itself went through"
    sends = []
    for _ in range(MAX_ATTEMPTS + 2):
        with respx.mock(base_url=KP, assert_all_called=False) as mock:
            patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=old_build))
            [decision] = recompute_person(db, person, TODAY, False)
        sends.append(patch.call_count)
        if patch.call_count:
            assert decision.action == "link" and decision.outcome == "failed" and decision.detail == LINK_UNCONFIRMED
            assert body_of(patch.calls.last) == {"pp_account_id": ACCOUNT}
    assert sends == [1] * MAX_ATTEMPTS + [0, 0], "tried on each run until the limit, then left alone"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == LINK_UNCONFIRMED and grant.remote_info == {}
    row = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == link_key(grant.id, ACCOUNT)).one()
    assert row.status == "failed" and row.attempts == MAX_ATTEMPTS and row.response["unconfirmed"] is True
    # Key Properties is updated; Retry sends the link at once and it takes
    reset_for_retry(db, grant, actor="founder@example.test")
    db.commit()
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert patch.call_count == 1 and decision.action == "link" and decision.outcome == "sent"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == "" and grant.remote_info == {"pp_account_id": ACCOUNT}


def test_a_pp_account_mismatch_is_a_failed_grant_with_a_plain_message_until_retry(db, live, monkeypatch):
    person = make_person(db, pp_account_id=OTHER_ACCOUNT)
    mentee = enrol_paid(db, person, programme(db, "kp_only", [{"product": "kp", "kind": "sourcer_link", "value": "mentee"}]))
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_mismatch")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.outcome == "failed" and decision.detail == MISMATCH_ERROR and post.call_count == 1
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "failed" and grant.last_error == MISMATCH_ERROR and display_state(grant) == "failed"
    assert "different Property Predator account" in MISMATCH_ERROR and "Retry" in MISMATCH_ERROR
    with respx.mock(assert_all_called=False) as mock:
        for _ in range(3):
            [decision] = recompute_person(db, person, TODAY, False)
            assert decision.action == "blocked" and "not retried automatically" in decision.detail
    assert mock.calls.call_count == 0, "never retried on its own"
    reset_for_retry(db, grants_of(db, person)[("kp", "sourcer_link")], actor="founder@example.test")
    db.commit()
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(
            200, json={**fixture("kp_sourcer_linked"), "pp_account_id": OTHER_ACCOUNT}))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.outcome == "sent" and post.call_count == 1
    assert grants_of(db, person)[("kp", "sourcer_link")].remote_info == {"pp_account_id": OTHER_ACCOUNT}

    # an active sourcer whose link call is refused is failed too
    person.pp_account_id = THIRD_ACCOUNT
    db.commit()
    with respx.mock(base_url=KP) as mock:
        mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_mismatch")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.action == "link" and decision.outcome == "failed"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "failed" and grant.last_error == MISMATCH_ERROR
    # Retry after a link mismatch sends the link in the same run (the grant goes back to active, its sourcer never left)
    reset_for_retry(db, grant, actor="founder@example.test")
    db.commit()
    assert grants_of(db, person)[("kp", "sourcer_link")].state == "active"
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(
            200, json={**fixture("kp_sourcer_linked"), "pp_account_id": THIRD_ACCOUNT}))
        [decision] = recompute_person(db, person, TODAY, False)
    assert patch.call_count == 1 and body_of(patch.calls.last) == {"pp_account_id": THIRD_ACCOUNT}
    assert decision.action == "link" and decision.outcome == "sent"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == "" and grant.remote_info == {"pp_account_id": THIRD_ACCOUNT}

    # and a mismatched sourcer is still switched off when access ends
    person.pp_account_id = ACCOUNT
    db.commit()
    with respx.mock(base_url=KP) as mock:
        mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_mismatch")))
        recompute_person(db, person, TODAY, False)
    assert grants_of(db, person)[("kp", "sourcer_link")].last_error == MISMATCH_ERROR
    mentee = db.get(Mentee, mentee.id)
    mentee.status = "ended"
    db.commit()
    with respx.mock(base_url=KP) as mock:
        off = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json={**fixture("kp_sourcer_linked"), "active": False}))
        [decision] = recompute_person(db, person, TODAY + timedelta(days=1), False)
    assert decision.action == "revoke" and decision.outcome == "sent" and body_of(off.calls.last) == {"active": False}
    assert grants_of(db, person)[("kp", "sourcer_link")].state == "revoked"


def test_affiliate_grants_are_kept_by_the_products_when_access_ends(db, live):
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db, "affiliates", [{"product": "pp", "kind": "affiliate", "value": "affiliate"},
                                                        {"product": "ordris", "kind": "affiliate", "value": "partner"}]))
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
        mock.post(f"{ORDRIS}/api/partner/grouphq/partners").mock(return_value=httpx.Response(200, json=fixture("ordris_partner_join")))
        recompute_person(db, person, TODAY, False)
    with respx.mock(assert_all_called=False) as mock:
        counts = revoke_person(db, person, TODAY, False, actor="founder@example.test", reason="left")
    assert mock.calls.call_count == 0 and counts == {"mentees ended": 1, "kept by a public programme": 2}
    for grant in grants_of(db, person).values():
        assert grant.state == "revoked" and grant.remote_info.get("link"), "the link is still shown: the product keeps the member"
        [_, revoke] = db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).order_by(ConnectorCall.id).all()
        assert revoke.status == "succeeded" and revoke.response["noop"] == "kept: public programme"


def test_every_active_mentee_gets_one_seller_and_its_statements_stay_held(db):
    person = make_person(db)
    enrol_paid(db, person, programme(db, "nothing", []))
    recompute_person(db, person, TODAY, True)
    recompute_person(db, person, TODAY, True)
    [seller] = db.query(Seller).all()
    assert seller.person_id == person.id and seller.kind == "mentee" and seller.active and seller.code.startswith("AA-")
    assert seller.self_billing_agreed_at is None, "statements stay held until self-billing is agreed"
    rows = db.query(AuditLog).filter(AuditLog.action == "seller.create").all()
    assert len(rows) == 1 and rows[0].after["via"] == "recompute: active mentee"
    # someone who is already a seller, of any kind, is left exactly as they are
    other = make_person(db, "ben@example.test", "Ben Brook")
    db.add(Seller(person_id=other.id, kind="outsider", code="BB-ZZZZ", active=False))
    db.commit()
    enrol_paid(db, other, programme(db, "nothing", []))
    recompute_person(db, other, TODAY, True)
    assert [(s.kind, s.code, s.active) for s in db.query(Seller).filter(Seller.person_id == other.id)] == [("outsider", "BB-ZZZZ", False)]
    # nobody without an active mentee row gets one
    ended = make_person(db, "cy@example.test", "Cy Three")
    enrol_paid(db, ended, programme(db, "nothing", []), status="ended")
    recompute_person(db, ended, TODAY, True)
    assert db.query(Seller).filter(Seller.person_id == ended.id).count() == 0


# ---- the Needs a region queue ------------------------------------------------------------------------------------------
def _seat(db, email, name, lookup, **person_kw):
    person = make_person(db, email, name, **person_kw)
    enrol_paid(db, person, programme(db, "hnh_only", [{"product": "hnh", "kind": "partner_seat", "value": "area"}]))
    with respx.mock(base_url=HNH) as mock:
        mock.post("/api/partner/partners").mock(return_value=httpx.Response(201, json=fixture("hnh_partner_created")))
        mock.get("/api/partner/partners").mock(return_value=httpx.Response(200, json=fixture(lookup)))
        recompute_person(db, person, TODAY, False)
    return person


def test_the_needs_a_region_queue_reads_the_cache_and_links_to_hnh(admin_client, db, live):
    amy = _seat(db, EMAIL, "Amy Archer", "hnh_partner_no_area")
    _seat(db, "ben@example.test", "Ben Brook", "hnh_partner_status")         # has an area
    held = _seat(db, "cy@example.test", "Cy Three", "hnh_partner_no_area")
    held.access_held_at = datetime(2026, 10, 19)
    db.commit()
    assert [r["person"].person_key for r in earnings.needs_region(db)] == [EMAIL]
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.get("/")
        listed = admin_client.get("/mentees?region=needed")
        unfiltered = admin_client.get("/mentees")
    assert mock.calls.call_count == 0, "the queue needs no live call"
    assert page.status_code == 200 and "Needs a region" in page.text and "Amy Archer" in page.text
    assert 'href="https://hnh.example.test/admin/partners/9"' in page.text and "Assign a region in Hello New Home" in page.text
    assert "Ben Brook" not in page.text.split("Needs a region")[1].split("Latest activity")[0]
    assert listed.status_code == 200 and "Amy Archer" in listed.text and "Ben Brook" not in listed.text
    assert "Cy Three" not in listed.text and 'href="https://hnh.example.test/admin/partners/9"' in listed.text
    assert "Ben Brook" in unfiltered.text and "Assign a region" not in unfiltered.text
    # once the area is assigned in Hello New Home, the next recompute takes them off
    with respx.mock(base_url=HNH) as mock:
        mock.get("/api/partner/partners").mock(return_value=httpx.Response(200, json=fixture("hnh_partner_status")))
        recompute_person(db, amy, TODAY, False)
    assert earnings.needs_region(db) == []
    assert "No active mentee needs a region" in admin_client.get("/mentees?region=needed").text
    # a lookup that fails keeps the last answer and says so on the grant
    with respx.mock(base_url=HNH) as mock:
        mock.get("/api/partner/partners").mock(side_effect=httpx.ConnectError("refused"))
        recompute_person(db, amy, TODAY, False)
    info = grants_of(db, amy)[("hnh", "partner_seat")].remote_info
    assert info["needs_region"] is False and info["area"]["name"] == "Swansea" and "unreachable" in info["check_error"]


# ---- the earning links panel -----------------------------------------------------------------------------------------
def mock_panel(mock):
    routes = SimpleNamespace()
    routes.pp = mock.get(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_status")))
    routes.ordris = mock.get(f"{ORDRIS}/api/partner/grouphq/partners").mock(return_value=httpx.Response(200, json=fixture("ordris_partner_status")))
    routes.kp = mock.get(f"{KP}/api/partner/sourcers").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_status")))
    routes.hnh = mock.get(f"{HNH}/api/partner/partners").mock(return_value=httpx.Response(200, json=fixture("hnh_partner_no_area")))
    return routes


def _mentee(db):
    person = make_person(db, pp_account_id=ACCOUNT)
    return person, enrol_paid(db, person, programme(db))


def test_the_pages_load_the_panel_after_they_render_and_ask_no_product(admin_client, db, live):
    person, mentee = _mentee(db)
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.get(f"/mentees/{mentee.id}")
        person_page = admin_client.get(f"/people/{person.id}")
    assert mock.calls.call_count == 0, "the page itself never waits on a product"
    assert page.status_code == 200 and f'data-fragment-src="/people/{person.id}/earnings?fragment=1&amp;mentee={mentee.id}"' in page.text
    assert person_page.status_code == 200 and f'data-fragment-src="/people/{person.id}/earnings?fragment=1"' in person_page.text
    assert "Earning links" in page.text and "Earning links" in person_page.text


def test_the_panel_shows_every_product_and_the_welcome_copies_every_link(admin_client, db, live):
    person, mentee = _mentee(db)
    db.add(Seller(person_id=person.id, kind="mentee", code="AA-7K3M"))
    db.commit()
    with respx.mock() as mock:
        mock_panel(mock)
        r = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    html = r.text
    assert "<html" not in html, "the fragment is the panel only"
    for link in ("https://pp.example.test/?ref=AMY77", "https://ordris.example.test/?ref=AMYA41", "https://kp.example.test/login",
                 "https://hnh.example.test/partners/login", "https://hnh.example.test/?ref=AMY9",
                 "https://hellonewhome.co.uk/partners-programme?seller=AA-7K3M"):
        assert f'value="{link}"' in html, link
    for figure in ("£29.70", "£11.70", "£50.00", "£15.00", "£2,500.00", "£1,250.00", "50%"):
        assert figure in html, figure
    assert "Needs a region" in html and 'href="https://hnh.example.test/admin/partners/9"' in html
    assert "Held until self-billing is agreed" in html
    assert "Copy WhatsApp welcome" in html and 'data-copy-target="#earn-welcome"' in html
    welcome = html.split('<textarea id="earn-welcome"')[1].split("</textarea>")[0]
    assert "Hi Amy," in welcome and "Welcome to the Mentorship." in welcome
    for line in ("Property Predator affiliate link: https://pp.example.test/?ref=AMY77",
                 "Ordris partner link: https://ordris.example.test/?ref=AMYA41",
                 "Key Properties: sign in to send your deals at https://kp.example.test/login",
                 "Hello New Home partner portal: https://hnh.example.test/partners/login",
                 "Hello New Home referral link: https://hnh.example.test/?ref=AMY9",
                 "(ask them to quote your code AA-7K3M)"):
        assert line in welcome, line
    assert "%" not in welcome and "£" not in welcome, "no figures and no promises in the message"
    # the person page's panel has no welcome; the page of its own works without JavaScript
    with respx.mock() as mock:
        mock_panel(mock)
        plain = admin_client.get(f"/people/{person.id}/earnings")
    assert plain.status_code == 200 and "<html" in plain.text and "Copy WhatsApp welcome" not in plain.text
    assert "https://pp.example.test/?ref=AMY77" in plain.text
    # a mentee row of someone else is ignored
    other = make_person(db, "ben@example.test", "Ben Brook")
    other_mentee = enrol_paid(db, other, programme(db))
    with respx.mock() as mock:
        mock_panel(mock)
        r = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={other_mentee.id}")
    assert "Copy WhatsApp welcome" not in r.text


@pytest.mark.parametrize("failing,name", [("pp", "Property Predator"), ("ordris", "Ordris"), ("kp", "Key Properties"),
                                          ("hnh", "Hello New Home")])
def test_a_product_that_fails_is_shown_as_unreachable_and_the_rest_still_render(admin_client, db, live, failing, name):
    person, mentee = _mentee(db)
    db.add(EntitlementGrant(person_id=person.id, product="pp", kind="affiliate", value="affiliate", state="active",
                            remote_info={"code": "AMY77", "link": "https://pp.example.test/?ref=AMY77"}))
    db.commit()
    with respx.mock() as mock:
        routes = mock_panel(mock)
        getattr(routes, failing).mock(side_effect=httpx.ConnectError("refused") if failing in ("pp", "kp")
                                      else httpx.ReadTimeout("slow"))
        r = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}")
    assert r.status_code == 200
    html = r.text
    assert f"Couldn't reach {name}" in html and "couldn&#39;t reach" in html
    others = {"pp": "https://pp.example.test/?ref=AMY77", "ordris": "https://ordris.example.test/?ref=AMYA41",
              "kp": "https://kp.example.test/login", "hnh": "https://hnh.example.test/partners/login"}
    for product, link in others.items():
        if product != failing:
            assert link in html, product
    if failing == "pp":
        assert "Last known, as the recompute kept it" in html and "https://pp.example.test/?ref=AMY77" in html, \
            "the link the recompute kept is still shown, and still goes in the welcome"
        assert "Not in the message:" not in html
    else:
        assert f"Not in the message: {name}, which could not be asked just now." in html


def test_a_slow_product_is_given_up_on_without_holding_the_panel(admin_client, db, live, monkeypatch):
    monkeypatch.setattr(earnings, "EARNINGS_TIMEOUT", 0.3)
    monkeypatch.setattr(earnings, "GRACE_SECONDS", 0.2)
    person, mentee = _mentee(db)

    def slow(request):
        time.sleep(1.5)
        return httpx.Response(200, json=fixture("hnh_partner_no_area"))

    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        routes.hnh.mock(side_effect=slow)
        started = time.monotonic()
        r = admin_client.get(f"/people/{person.id}/earnings?fragment=1")
        took = time.monotonic() - started
    assert r.status_code == 200 and took < 1.4, f"the panel waited {took:.2f}s for a slow product"
    assert "Couldn't reach Hello New Home: no answer within 0.3 seconds" in r.text
    assert "https://pp.example.test/?ref=AMY77" in r.text


def test_dry_run_and_unconfigured_products_are_not_asked(admin_client, db, live, monkeypatch):
    person, mentee = _mentee(db)
    db.add(EntitlementGrant(person_id=person.id, product="hnh", kind="partner_seat", value="area", state="active",
                            remote_info=partner_info(fixture("hnh_partner_status")) | {"checked_at": "2026-10-20T03:10:00"}))
    db.commit()
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset({"hnh", "ordris"}))
    monkeypatch.setattr(config, "KP_PARTNER_TOKEN", "")
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        r = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}")
    assert routes.hnh.call_count == routes.ordris.call_count == routes.kp.call_count == 0 and routes.pp.call_count == 1
    html = r.text
    assert html.count(">dry run</span>") == 2 and ">not configured</span>" in html
    assert "Last known, as the recompute kept it: Hello New Home is in dry run" in html and "Swansea" in html
    welcome = html.split('<textarea id="earn-welcome"')[1].split("</textarea>")[0]
    assert "Hello New Home partner portal: https://hnh.example.test/partners/login" in welcome
    assert "Key Properties" not in welcome, "no link is invented for a product that was not asked"


def test_the_panel_says_plainly_who_is_not_enrolled_yet(admin_client, db, live):
    person, mentee = _mentee(db)
    with respx.mock() as mock:
        routes = mock_panel(mock)
        routes.pp.mock(return_value=httpx.Response(404, json=fixture("pp_no_account")))
        routes.ordris.mock(return_value=httpx.Response(200, json=fixture("ordris_partner_none")))
        routes.kp.mock(return_value=httpx.Response(404, json={"ok": False, "reason": "not_found", "error": "not_found"}))
        routes.hnh.mock(return_value=httpx.Response(200, json={"ok": True, "found": False, "partner": None}))
        r = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}")
    html = r.text
    assert "No Property Predator account with this email." in html
    assert "Not a partner yet: their programme enrols them when the recompute grants their access." in html
    assert "No sourcer with this email yet: their programme links them" in html
    assert "No partner with this email yet: their programme seats them" in html
    assert html.count(">not enrolled</span>") == 4
    assert "No seller record yet" in html
    welcome = html.split('<textarea id="earn-welcome"')[1].split("</textarea>")[0]
    assert "Your links will be ready as soon as your access is set up" in welcome
    # someone on no programme that gives these rights, whose ids Group HQ holds in each product, is told so
    other = make_person(db, "ben@example.test", "Ben Brook", pp_account_id=OTHER_ACCOUNT, ordris_user_id="user_2abc",
                        kp_user_id=5, hnh_partner_id=12)
    enrol_paid(db, other, programme(db, "nothing", []))
    with respx.mock() as mock:
        routes = mock_panel(mock)
        routes.pp.mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_none")))
        routes.ordris.mock(return_value=httpx.Response(200, json=fixture("ordris_partner_none")))
        routes.kp.mock(return_value=httpx.Response(404, json={"ok": False, "reason": "not_found", "error": "not_found"}))
        routes.hnh.mock(return_value=httpx.Response(200, json={"ok": True, "found": False, "partner": None}))
        html = admin_client.get(f"/people/{other.id}/earnings?fragment=1").text
    assert "Not an affiliate, and none of their programmes enrols them." in html
    assert "Not a partner, and none of their programmes enrols them." in html
    assert "No sourcer with this email." in html and "No partner with this email." in html


def test_the_panel_never_sends_a_strangers_email_to_a_product(admin_client, db, live):
    """A franchise prospect or an outside seller: no mentee row, no grant, no product id. Their person page asks nobody,
    Hello New Home included (a separate controller), and says why."""
    for kw in ({}, {"pp_account_id": OTHER_ACCOUNT}):
        stranger = make_person(db, f"prospect{len(kw)}@example.test", "Pat Prospect", **kw)
        with respx.mock(assert_all_called=False) as mock:
            mock_panel(mock)
            html = admin_client.get(f"/people/{stranger.id}/earnings?fragment=1").text
            asked = sorted(c.request.url.host.split(".")[0] for c in mock.calls)
        assert asked == (["pp"] if kw else []), "only the product that already knows them by id is asked"
        assert html.count("Not asked: none of their programmes includes this") == (3 if kw else 4)
        assert "Not asked: none of their programmes includes this and no Hello New Home id is linked here." in html
    # an ended mentee keeps the affiliate memberships, so those two are still asked; the rest are not
    ended = make_person(db, "cy@example.test", "Cy Three")
    db.add_all([EntitlementGrant(person_id=ended.id, product=p, kind="affiliate", value=v, state="revoked")
                for p, v in (("pp", "affiliate"), ("ordris", "partner"))])
    db.commit()
    with respx.mock(assert_all_called=False) as mock:
        mock_panel(mock)
        admin_client.get(f"/people/{ended.id}/earnings?fragment=1")
        asked = sorted(c.request.url.host.split(".")[0] for c in mock.calls)
    assert asked == ["ordris", "pp"]


def test_a_route_not_deployed_is_never_shown_as_no_account(admin_client, db, live):
    person, mentee = _mentee(db)
    with respx.mock() as mock:
        routes = mock_panel(mock)
        routes.pp.mock(return_value=httpx.Response(404, json=NOT_FOUND))
        routes.hnh.mock(return_value=httpx.Response(404, json=NOT_FOUND))
        html = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}").text
    assert "No Property Predator account with this email." not in html
    assert "Couldn't reach Property Predator: Not Found (HTTP 404)" in html
    assert "Couldn't reach Hello New Home: Not Found (HTTP 404)" in html and "No partner with this email" not in html


def test_the_panel_needs_a_login(client, db):
    person = make_person(db)
    r = client.get(f"/people/{person.id}/earnings?fragment=1")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_pausing_an_affiliate_grant_sends_nothing_and_says_so(admin_client, db, live):
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db, "affiliates", [{"product": "pp", "kind": "affiliate", "value": "affiliate"}]))
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
        recompute_person(db, person, TODAY, False)
    grant = grants_of(db, person)[("pp", "affiliate")]
    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.post(f"/grants/{grant.id}/pause", data={"next": f"/grants/{grant.id}"})
    assert mock.calls.call_count == 0 and r.status_code == 303
    assert "membership+is+permanent" in r.headers["location"] and "err=" not in r.headers["location"]
    detail = admin_client.get(f"/grants/{grant.id}")
    assert detail.status_code == 200 and "https://pp.example.test/?ref=AMY77" in detail.text, "the product's code and link are shown"
    # Pause all access says the same: the affiliate membership is kept, not revoked
    other = make_person(db, "ben@example.test", "Ben Brook", pp_account_id=OTHER_ACCOUNT)
    enrol_paid(db, other, programme(db, "affiliates"))
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
        recompute_person(db, other, TODAY, False)
    with respx.mock(assert_all_called=False) as mock:
        assert ent.pause_person(db, other, TODAY, False, actor="founder@example.test") == {"kept by a public programme": 1}
    assert mock.calls.call_count == 0


# ---- review fixes: the PP account id is text, a route not deployed, a PP id held by someone else ------------------------
def test_account_ids_are_text_and_a_404_without_a_refusal_is_a_missing_route():
    from app.services.connectors.base import account_ref, route_missing
    assert account_ref(f" {ACCOUNT} ") == ACCOUNT and account_ref(77) == "77" and account_ref("77") == "77"
    for bad in (None, True, "", "  ", "forty two", "a" * 81, 7.5, {"id": 1}, "<script>"):
        assert account_ref(bad) is None, bad
    assert route_missing(ConnectorError("Not Found", status=404, body=NOT_FOUND))
    assert route_missing(ConnectorError("http_404", status=404, body={"raw": "<html>Cannot GET</html>"}))
    assert not route_missing(ConnectorError("no_account", status=404, body=fixture("pp_no_account")))
    assert not route_missing(ConnectorError("no_account", status=404, body={"detail": fixture("pp_no_account")}))
    assert not route_missing(ConnectorError("Not Found", status=502, body=NOT_FOUND))


def test_an_affiliate_route_not_deployed_yet_never_uses_up_the_attempts(db, live):
    """Group HQ (and its 0008) can go live before PP's contract 1 or Ordris's contract 2: every night's 404 is a failed
    call, but not an attempt, so the grant never gives up and needs no Retry once the product deploys."""
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db, "affiliates", [{"product": "pp", "kind": "affiliate", "value": "affiliate"},
                                                        {"product": "ordris", "kind": "affiliate", "value": "partner"}]))
    for _ in range(MAX_ATTEMPTS + 2):
        with respx.mock() as mock:
            pp = mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(404, json=NOT_FOUND))
            ordris = mock.post(f"{ORDRIS}/api/partner/grouphq/partners").mock(return_value=httpx.Response(404, text="Not found"))
            decisions = recompute_person(db, person, TODAY, False)
        assert pp.call_count == ordris.call_count == 1 and {d.outcome for d in decisions} == {"failed"}
    for grant in grants_of(db, person).values():
        assert grant.state == "failed" and "not deployed yet" in grant.last_error and "not counted as an attempt" in grant.last_error
        row = db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).one()
        assert row.attempts == 0 and row.status == "failed"
    assert "Property Predator answered 404" in grants_of(db, person)[("pp", "affiliate")].last_error
    # once the routes are there, the next run enrols them with no Retry
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
        mock.post(f"{ORDRIS}/api/partner/grouphq/partners").mock(return_value=httpx.Response(200, json=fixture("ordris_partner_join")))
        recompute_person(db, person, TODAY, False)
    assert {g.state for g in grants_of(db, person).values()} == {"active"}
    # PP's own no_account refusal still counts: that one needs the mentee to sign up
    other = make_person(db, "ben@example.test", "Ben Brook")
    enrol_paid(db, other, programme(db, "pp_affiliate", [{"product": "pp", "kind": "affiliate", "value": "affiliate"}]))
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(404, json=fixture("pp_no_account")))
        recompute_person(db, other, TODAY, False)
    [grant] = grants_of(db, other).values()
    assert grant.last_error.startswith("no_account") and db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).one().attempts == 1


def test_a_pp_account_another_person_holds_is_never_filled_twice(db, live):
    """PP answers an account id already on someone else's row (a hand link to the wrong person, say): the recompute goes
    on, the unique constraint is never hit, and the id stays with the person who holds it."""
    holder = make_person(db, "ben@example.test", "Ben Brook", pp_account_id=ACCOUNT)
    person = make_person(db)
    enrol_paid(db, person, programme(db, "affiliates", [{"product": "pp", "kind": "affiliate", "value": "affiliate"}]))
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/affiliate").mock(return_value=httpx.Response(200, json=fixture("pp_affiliate_join")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.outcome == "sent"
    db.expire_all()
    assert db.get(Person, person.id).pp_account_id is None and db.get(Person, holder.id).pp_account_id == ACCOUNT


def test_the_person_page_takes_a_pp_uuid_and_refuses_anything_else(admin_client, db):
    person = make_person(db)
    r = admin_client.post(f"/people/{person.id}/links", data={"pp_account_id": f" {ACCOUNT} "})
    assert r.status_code == 303, r.text[:400]
    db.expire_all()
    assert db.get(Person, person.id).pp_account_id == ACCOUNT
    r = admin_client.post(f"/people/{person.id}/links", data={"pp_account_id": "not an id"})
    assert r.status_code == 400 and "letters, digits and hyphens" in r.text
    db.expire_all()
    assert db.get(Person, person.id).pp_account_id == ACCOUNT


def test_the_downgrade_clears_only_ids_an_integer_column_cannot_hold(db):
    migration = _migration()
    people = [make_person(db, f"p{i}@example.test", f"P {i}", pp_account_id=value)
              for i, value in enumerate(("77", "007", ACCOUNT, "2147483648"))]
    keep = make_person(db, "q@example.test", "Q")
    with engine.begin() as conn:
        assert migration.clear_non_integer_ids(conn) == 3
    db.expire_all()
    assert [db.get(Person, p.id).pp_account_id for p in people] == ["77", None, None, None]
    assert db.get(Person, keep.id).pp_account_id is None
    rows = db.query(AuditLog).filter(AuditLog.action == "person.links", AuditLog.actor == migration.ACTOR).all()
    assert sorted(r.before["pp_account_id"] for r in rows) == sorted(["007", ACCOUNT, "2147483648"])


# ---- contract 4: a Property Predator link Key Properties refuses (pp_account_in_use) -----------------------------------
IN_USE_MESSAGE = "That PropertyPredator account is already linked to another Key Properties UK login."
KP_ONLY = [{"product": "kp", "kind": "sourcer_link", "value": "mentee"}]


def kp_card(html: str) -> str:
    return html.split("<strong>Key Properties</strong>")[1].split('<div class="earn-card">')[0]


def test_kp_link_refused_is_read_from_a_patch_answer_and_a_refused_link_call_is_a_failure():
    held = fake_grant(product="kp", kind="sourcer_link", value="mentee", remote_ref="41")
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_link_refused")))
        result = KPConnector.from_config().grant(held, fake_person(pp_account_id=ACCOUNT))
        assert body_of(patch.calls.last) == {"active": True, "pp_account_id": ACCOUNT}
        assert result.remote_ref == "41" and result.info == {"link_refused": {"reason": "pp_account_in_use", "message": IN_USE_MESSAGE}}, \
            "the sourcer is open (a success), and the link the portal left off is read"
        with pytest.raises(ConnectorError) as raised:          # the same answer to a call that asked only for the link
            KPConnector.from_config().link(held, fake_person(pp_account_id=ACCOUNT))
        assert raised.value.reason == "pp_account_in_use" and raised.value.detail == IN_USE_MESSAGE
        patch.mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_in_use")))
        with pytest.raises(ConnectorError) as raised:          # what the portal answers today to a link-only PATCH
            KPConnector.from_config().link(held, fake_person(pp_account_id=ACCOUNT))
        assert raised.value.reason == "pp_account_in_use" and raised.value.status == 409
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_in_use")))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().grant(fake_grant(product="kp", kind="sourcer_link", value="mentee"), fake_person(pp_account_id=ACCOUNT))
        assert raised.value.reason == "pp_account_in_use" and post.call_count == 1
    assert link_refusal(fixture("kp_sourcer_linked")) is None
    assert link_refusal({"link_refused": "yes"}) is None and link_refusal({"link_refused": {}}) is None
    assert link_refusal({"sourcer": {"id": 41, "link_refused": {"error": "pp_account_in_use"}}}) == {"reason": "pp_account_in_use"}
    assert link_refusal({"link_refused": {"reason": "pp_account_in_use", "message": "x" * 400}})["message"] == "x" * 300


def test_a_pp_account_another_kp_login_holds_fails_the_first_grant_plainly_until_retry(admin_client, db, live):
    """POST 409 pp_account_in_use: Key Properties created nothing, so the grant is failed with a message saying what to put
    right in Key Properties, never sent again on its own, and shown on the grant page and the panel until Retry."""
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db, "kp_only", KP_ONLY))
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_in_use")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert post.call_count == 1 and body_of(post.calls.last)["pp_account_id"] == ACCOUNT
    assert decision.action == "grant" and decision.outcome == "failed" and decision.detail == IN_USE_CREATE_ERROR
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "failed" and grant.last_error == IN_USE_CREATE_ERROR and display_state(grant) == "failed"
    refused = grant.remote_info["link_refused"]
    assert refused["reason"] == "pp_account_in_use" and refused["message"] == IN_USE_MESSAGE
    assert refused["pp_account_id"] == ACCOUNT and refused["at"] and refused["sourcer_open"] is False
    for words in ("another Key Properties login", "changed nothing", "no sourcer was created or opened",
                  "any Key Properties login they already had is left as it was", "In Key Properties", "merge it",
                  "take the account off it", "press Retry on the grant"):
        assert words in IN_USE_CREATE_ERROR, words
    assert "no Key Properties access" not in IN_USE_CREATE_ERROR, "the POST also refuses a login that already has the email"
    [row] = db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).all()
    assert row.status == "failed" and row.attempts == MAX_ATTEMPTS
    fail = db.query(AuditLog).filter(AuditLog.action == "grant.fail", AuditLog.entity == f"grant:{grant.id}").one()
    assert fail.after["reason"] == "pp_account_in_use" and fail.after["link_refused"]["pp_account_id"] == ACCOUNT
    with respx.mock(assert_all_called=False) as mock:
        for _ in range(3):
            [decision] = recompute_person(db, person, TODAY, False)
            assert decision.action == "blocked" and "not retried automatically" in decision.detail
    assert mock.calls.call_count == 0, "never sent again on its own"
    # the grant page and the panel say what to do; Key Properties itself has no sourcer for the email
    page = admin_client.get(f"/grants/{grant.id}").text
    assert str(escape(IN_USE_CREATE_ERROR)) in page and f'action="/grants/{grant.id}/retry"' in page
    assert "Refused by Key Properties (pp_account_in_use)" in page and ACCOUNT in page
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        routes.kp.mock(return_value=httpx.Response(404, json={"ok": False, "reason": "not_found", "error": "not_found"}))
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Not linked to Property Predator." in card and str(escape(IN_USE_CREATE_ERROR)) in card
    assert f'href="/grants/{grant.id}"' in card
    # put right in Key Properties: Retry sends the POST again and the refusal is gone
    reset_for_retry(db, grant, actor="founder@example.test")
    db.commit()
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(201, json=fixture("kp_sourcer_linked")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert post.call_count == 1 and decision.outcome == "sent" and decision.detail == "sent"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == "" and grant.remote_info == {"pp_account_id": ACCOUNT}
    with respx.mock(assert_all_called=False) as mock:
        mock_panel(mock)
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Not linked to Property Predator." not in card


def test_a_sourcer_opened_without_its_pp_link_says_so_and_waits_for_retry(admin_client, db, live):
    """A re-grant Key Properties applies without the link (200 with link_refused): the sourcer is open, so the grant is
    active; the refusal is kept and shown, the same id is not sent again on its own, and Retry asks again."""
    person = make_person(db, pp_account_id=ACCOUNT)
    mentee = enrol_paid(db, person, programme(db, "kp_only", KP_ONLY))
    # a sourcer Group HQ switched off before (an earlier lapse), never linked to Property Predator
    db.add(EntitlementGrant(person_id=person.id, mentee_id=mentee.id, product="kp", kind="sourcer_link", value="mentee",
                            source="programme:kp_only", state="revoked", remote_ref="41"))
    db.commit()
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_link_refused")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert body_of(patch.calls.last) == {"active": True, "pp_account_id": ACCOUNT}, "the re-grant carries the id"
    assert decision.action == "grant" and decision.outcome == "sent"
    assert decision.detail == f"sent without the Property Predator link: {IN_USE_ERROR}"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.ends_at == datetime(2027, 10, 1) and grant.last_error == IN_USE_ERROR
    assert display_state(grant) == "active", "the access itself went through"
    refused = grant.remote_info["link_refused"]
    assert refused["reason"] == "pp_account_in_use" and refused["pp_account_id"] == ACCOUNT and refused["message"] == IN_USE_MESSAGE
    assert "pp_account_id" not in grant.remote_info, "Key Properties holds no account on the row"
    assert "the sourcer is open but not linked" in IN_USE_ERROR and "press Retry on the grant" in IN_USE_ERROR
    granted = db.query(AuditLog).filter(AuditLog.action == "grant.grant", AuditLog.entity == f"grant:{grant.id}").one()
    assert granted.after["link_refused"]["reason"] == "pp_account_in_use"
    with respx.mock(assert_all_called=False) as mock:
        [decision] = recompute_person(db, person, TODAY, False)
    assert mock.calls.call_count == 0 and decision.action == "noop", "no link call for an id Key Properties has refused"

    # the panel shows it beside Key Properties' own answer, and from the grant when Key Properties cannot be asked
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        routes.kp.mock(return_value=httpx.Response(200, json={**fixture("kp_sourcer_status"), "pp_linked": False, "pp_account_id": None}))
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Not linked to Property Predator." in card and str(escape(IN_USE_ERROR)) in card and f'href="/grants/{grant.id}"' in card
    assert "Sourcer login" in card, "the sourcer's own rows still show"
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        routes.kp.mock(side_effect=httpx.ConnectError("refused"))
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Couldn't reach Key Properties" in card and str(escape(IN_USE_ERROR)) in card
    page = admin_client.get(f"/grants/{grant.id}").text
    assert "Refused by Key Properties (pp_account_in_use)" in page and str(escape(IN_USE_ERROR)) in page
    assert f'action="/grants/{grant.id}/retry"' in page, "an active grant with a refused link can be retried"

    # Retry asks again in the same run, with the link call; Key Properties still refuses (409 on a link-only PATCH)
    reset_for_retry(db, grant, actor="founder@example.test")
    db.commit()
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_in_use")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert patch.call_count == 1 and body_of(patch.calls.last) == {"pp_account_id": ACCOUNT}
    assert decision.action == "link" and decision.outcome == "failed" and decision.detail == IN_USE_ERROR
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == IN_USE_ERROR, "the sourcer is still open"
    assert grant.remote_info["link_refused"]["reason"] == "pp_account_in_use"
    assert "retried_at" not in grant.remote_info["link_refused"], "refused again: recorded afresh, so it waits for Retry"
    row = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == link_key(grant.id, ACCOUNT)).one()
    assert row.status == "failed" and row.attempts == MAX_ATTEMPTS
    with respx.mock(assert_all_called=False) as mock:
        recompute_person(db, person, TODAY, False)
    assert mock.calls.call_count == 0, "a refused link is not sent again on its own"

    # once it is put right in Key Properties, Retry links it and the message goes
    reset_for_retry(db, grant, actor="founder@example.test")
    db.commit()
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert patch.call_count == 1 and decision.action == "link" and decision.outcome == "sent"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == "" and grant.remote_info == {"pp_account_id": ACCOUNT}
    assert link_refusal_note(grant, person) == ""


def test_the_refusal_note_follows_the_grant_and_the_persons_current_account():
    def grant(**kw):
        base = {"product": "kp", "kind": "sourcer_link", "state": "active", "last_error": "",
                "remote_info": {"link_refused": {"reason": "pp_account_in_use", "pp_account_id": ACCOUNT}}}
        base.update(kw)
        return SimpleNamespace(**base)

    assert link_refusal_note(grant(), fake_person(pp_account_id=ACCOUNT)) == IN_USE_ERROR
    assert link_refusal_note(grant(), fake_person(pp_account_id=OTHER_ACCOUNT)) == "", "an id since corrected on the person page"
    mismatch = grant(remote_info={"link_refused": {"reason": "pp_account_mismatch", "pp_account_id": ACCOUNT}})
    assert link_refusal_note(mismatch, fake_person(pp_account_id=ACCOUNT)) == MISMATCH_ERROR
    assert link_refusal_note(grant(state="failed", last_error=MISMATCH_ERROR, remote_info={}), fake_person()) == MISMATCH_ERROR
    other = link_refusal_note(grant(remote_info={"link_refused": {"reason": "something_new"}}), fake_person(pp_account_id=ACCOUNT))
    assert "(something_new)" in other and "press Retry on the grant" in other
    assert link_refusal_note(grant(state="revoked"), fake_person(pp_account_id=ACCOUNT)) == ""
    assert link_refusal_note(grant(product="pp", kind="affiliate"), fake_person(pp_account_id=ACCOUNT)) == ""
    assert link_refusal_note(grant(remote_info=None), fake_person(pp_account_id=ACCOUNT)) == ""
    assert link_refusal_note(None) == ""
    # a failed grant waits for Retry whatever the person page now says; an open sourcer is sent a corrected id on the next run
    assert link_refusal_note(grant(state="failed", last_error=IN_USE_CREATE_ERROR), fake_person(pp_account_id=OTHER_ACCOUNT)) \
        == IN_USE_CREATE_ERROR
    assert link_refusal_note(grant(last_error=IN_USE_ERROR), fake_person(pp_account_id=ACCOUNT)) == IN_USE_ERROR
    assert link_refusal_note(grant(last_error=IN_USE_ERROR), fake_person(pp_account_id=OTHER_ACCOUNT)) == "", \
        "an open sourcer's refusal for an id since corrected is not shown, even while its last error still says it"
    # which message: a call that changed nothing, or an open sourcer; and once Retry has been pressed
    closed = {"reason": "pp_account_in_use", "pp_account_id": ACCOUNT, "sourcer_open": False}
    assert link_refusal_note(grant(state="pending", remote_info={"link_refused": closed}), fake_person(pp_account_id=ACCOUNT)) \
        == IN_USE_CREATE_ERROR
    retried = {"link_refused": {**closed, "retried_at": "2026-10-20T09:00:00"}}
    assert link_refusal_note(grant(state="pending", remote_info=retried), fake_person(pp_account_id=ACCOUNT)) \
        == IN_USE_CREATE_ERROR + RETRY_PRESSED
    assert link_refusal_heading(IN_USE_ERROR) == link_refusal_heading(IN_USE_CREATE_ERROR) == "Not linked to Property Predator."
    assert link_refusal_heading(MISMATCH_ERROR + RETRY_PRESSED) == "Linked to a different Property Predator account."
    assert link_refusal_heading("") == ""


def _reopened_without_link(db, person) -> EntitlementGrant:
    """A sourcer Group HQ switched off before, whose re-grant Key Properties applies without the PP link (200 with
    link_refused): the grant is active with IN_USE_ERROR and the refusal kept."""
    mentee = enrol_paid(db, person, programme(db, "kp_only", KP_ONLY))
    db.add(EntitlementGrant(person_id=person.id, mentee_id=mentee.id, product="kp", kind="sourcer_link", value="mentee",
                            source="programme:kp_only", state="revoked", remote_ref="41"))
    db.commit()
    with respx.mock(base_url=KP) as mock:
        mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_link_refused")))
        recompute_person(db, person, TODAY, False)
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == IN_USE_ERROR and grant.remote_info["link_refused"]["sourcer_open"] is True
    return grant


def test_an_extension_key_properties_links_clears_the_refusal_without_retry(admin_client, db, live, monkeypatch):
    """The founder puts it right in Key Properties and presses nothing: the next call that carries the id (here an
    extension) is answered with it, and the refusal and the panel warning go."""
    person = make_person(db, pp_account_id=ACCOUNT)
    _reopened_without_link(db, person)
    monkeypatch.setenv("GROUPHQ_SETTING_FULL_TERM_MONTHS", "13")     # a longer term, so the next run extends the window
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert decision.action == "extend" and decision.outcome == "sent" and decision.detail == "sent"
    assert patch.call_count == 1 and body_of(patch.calls.last) == {"active": True, "pp_account_id": ACCOUNT}
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == "" and grant.ends_at == datetime(2027, 11, 1)
    assert grant.remote_info == {"pp_account_id": ACCOUNT}, "Key Properties now holds the account: the refusal is over"
    assert link_refusal_note(grant, person) == ""
    with respx.mock(assert_all_called=False) as mock:
        mock_panel(mock)
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Sourcer login" in card and "earn-warning" not in card and "Not linked to Property Predator." not in card


def test_a_dry_run_retry_keeps_the_refusal_shown_and_the_next_live_run_asks_again(admin_client, db, live):
    """Retry with dry run ticked sends nothing, so the refusal stays on record and on the panel (saying Retry has been
    pressed); the next live run sends the call, and a fresh refusal waits for Retry again."""
    person = make_person(db, pp_account_id=ACCOUNT)
    enrol_paid(db, person, programme(db, "kp_only", KP_ONLY))
    with respx.mock(base_url=KP) as mock:
        mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_in_use")))
        recompute_person(db, person, TODAY, False)
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "failed" and grant.last_error == IN_USE_CREATE_ERROR
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.post(f"/grants/{grant.id}/retry", data={"dry_run": "1"})
    assert page.status_code == 200 and mock.calls.call_count == 0, "a dry-run Retry sends nothing"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    refused = grant.remote_info["link_refused"]
    assert refused["reason"] == "pp_account_in_use" and refused["pp_account_id"] == ACCOUNT and refused["message"] == IN_USE_MESSAGE
    assert refused["retried_at"] and grant.state == "pending" and grant.last_error == ""
    assert link_refusal_note(grant, person) == IN_USE_CREATE_ERROR + RETRY_PRESSED
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        routes.kp.mock(return_value=httpx.Response(404, json={"ok": False, "reason": "not_found", "error": "not_found"}))
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Not linked to Property Predator." in card and str(escape(IN_USE_CREATE_ERROR + RETRY_PRESSED)) in card
    detail = admin_client.get(f"/grants/{grant.id}").text
    assert "Refused by Key Properties (pp_account_in_use)" in detail and "Retry pressed" in detail
    # the next live run sends what the Retry planned; still refused, it is recorded afresh and waits for Retry again
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_in_use")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert post.call_count == 1 and decision.outcome == "failed" and decision.detail == IN_USE_CREATE_ERROR
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "failed" and "retried_at" not in grant.remote_info["link_refused"]
    assert link_refusal_note(grant, person) == IN_USE_CREATE_ERROR
    with respx.mock(assert_all_called=False) as mock:
        recompute_person(db, person, TODAY, False)
    assert mock.calls.call_count == 0, "not sent again on its own"


def test_a_dry_run_retry_of_an_open_sourcer_plans_the_link_and_the_next_live_run_sends_it(admin_client, db, live):
    person = make_person(db, pp_account_id=ACCOUNT)
    grant = _reopened_without_link(db, person)
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.post(f"/grants/{grant.id}/retry", data={"dry_run": "1"})
    assert page.status_code == 200 and mock.calls.call_count == 0
    row = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == link_key(grant.id, ACCOUNT)).one()
    assert row.action == "link" and row.status == "pending" and row.attempts == 0, "the link call is planned, not sent"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.state == "active" and grant.last_error == "" and grant.remote_info["link_refused"]["retried_at"]
    assert link_refusal_note(grant, person) == IN_USE_ERROR + RETRY_PRESSED
    with respx.mock(base_url=KP) as mock:
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json=fixture("kp_sourcer_linked")))
        [decision] = recompute_person(db, person, TODAY, False)
    assert patch.call_count == 1 and body_of(patch.calls.last) == {"pp_account_id": ACCOUNT}
    assert decision.action == "link" and decision.outcome == "sent"
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert grant.remote_info == {"pp_account_id": ACCOUNT} and link_refusal_note(grant, person) == ""


def test_a_mismatch_card_says_the_sourcer_is_linked_to_a_different_account(admin_client, db, live):
    person = make_person(db, pp_account_id=ACCOUNT)
    mentee = enrol_paid(db, person, programme(db, "kp_only", KP_ONLY))
    db.add(EntitlementGrant(person_id=person.id, mentee_id=mentee.id, product="kp", kind="sourcer_link", value="mentee",
                            source="programme:kp_only", state="revoked", remote_ref="41"))
    db.commit()
    with respx.mock(base_url=KP) as mock:
        mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(409, json=fixture("kp_pp_account_mismatch")))
        [decision] = recompute_person(db, person, TODAY, False)
    grant = grants_of(db, person)[("kp", "sourcer_link")]
    assert decision.outcome == "failed" and grant.state == "failed" and grant.last_error == MISMATCH_ERROR
    with respx.mock(assert_all_called=False) as mock:
        routes = mock_panel(mock)
        routes.kp.mock(return_value=httpx.Response(200, json={**fixture("kp_sourcer_status"), "pp_account_id": OTHER_ACCOUNT}))
        card = kp_card(admin_client.get(f"/people/{person.id}/earnings?fragment=1").text)
    assert "Linked to a different Property Predator account." in card and str(escape(MISMATCH_ERROR)) in card
    assert "Not linked to Property Predator." not in card, "the sourcer is linked, only to another account"
    assert f'href="/grants/{grant.id}"' in card


# ---- contract 2 in the panel: the Ordris card -----------------------------------------------------------------------------
def test_the_ordris_card_shows_contract_two_and_the_terms_only_when_ordris_says(admin_client, db, live):
    person, mentee = _mentee(db)
    with respx.mock() as mock:
        mock_panel(mock)
        html = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}").text
    card = html.split("<strong>Ordris</strong>")[1].split('<div class="earn-card">')[0]
    rows = re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", card, re.S)
    assert [label for label, _ in rows] == ["Referral link", "Code", "Referrals", "Pending", "Payable", "Paid"]
    values = dict(rows)
    assert 'value="https://ordris.example.test/?ref=AMYA41"' in values["Referral link"]
    assert (values["Code"], values["Referrals"], values["Pending"], values["Payable"], values["Paid"]) == \
        ("AMYA41", "2", "£50.00", "£15.00", "£0.00")
    assert ">live</span>" in card and "Partner programme" in card and "terms" not in card, \
        "contract 2 says nothing about terms, so neither does the card"
    # when Ordris's answer also says whether the partner terms are accepted, the card shows it
    for extra, row, headline in (
            ({"terms_accepted_at": None, "terms_version": "2026-10"}, "Not accepted", "Partner programme: terms not accepted"),
            ({"terms_accepted": False}, "Not accepted", "Partner programme: terms not accepted"),
            ({"terms_accepted_at": "2026-10-24T08:30:00Z", "terms_version": "2026-10"}, "Accepted 24 Oct 2026 (version 2026-10)",
             "Partner programme</p>"),
            ({"terms_accepted": True}, "Accepted", "Partner programme</p>")):
        with respx.mock() as mock:
            routes = mock_panel(mock)
            routes.ordris.mock(return_value=httpx.Response(200, json={**fixture("ordris_partner_status"), **extra}))
            html = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}").text
        card = html.split("<strong>Ordris</strong>")[1].split('<div class="earn-card">')[0]
        values = dict(re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", card, re.S))
        assert values["Partner terms"] == row and headline in card, extra
    assert ordris_partner_terms({"terms_version": "2026-10"}) == {}, "a version alone says nothing about acceptance"
    assert ordris_partner_terms({"terms_accepted": "yes", "terms_accepted_at": 7}) == {"terms_accepted": False}


def test_a_suspended_or_terminated_ordris_partner_shows_its_status_and_its_link_stays_out_of_the_welcome(
        admin_client, db, live):
    person, mentee = _mentee(db)
    for status, earning in (("active", True), ("suspended", False), ("terminated", False), ("pending_approval", False),
                            (None, True)):
        answer = {**fixture("ordris_partner_status")}
        answer.pop("status", None)
        if status is not None:
            answer["status"] = status
        with respx.mock() as mock:
            routes = mock_panel(mock)
            routes.ordris.mock(return_value=httpx.Response(200, json=answer))
            html = admin_client.get(f"/people/{person.id}/earnings?fragment=1&mentee={mentee.id}").text
        card = html.split("<strong>Ordris</strong>")[1].split('<div class="earn-card">')[0]
        values = dict(re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", card, re.S))
        welcome = html.split('<textarea id="earn-welcome"')[1].split("</textarea>")[0] if "earn-welcome" in html else ""
        if earning:
            assert "Status" not in values and "earns nothing" not in card, status
            assert "ordris.example.test/?ref=AMYA41" in welcome, status
        else:
            label = status.replace("_", " ")
            assert values["Status"] == label and f"Partner programme: {label}, so the link earns nothing" in card, status
            assert "ordris.example.test" not in welcome, f"a {status} partner's link never goes in the welcome"
    assert ordris_partner_info({"status": " Suspended "})["status"] == "suspended"
    assert "status" not in ordris_partner_info({"status": 7})
