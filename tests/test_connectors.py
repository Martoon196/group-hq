"""The four connectors: dry run returns a recorded plan and sends nothing; the PP contract is sent exactly;
bearer token and X-Partner-Client on every live call, 10 second timeout, no retries, structured errors;
the token never appears in a plan or an error; Ordris, KP and HNH follow the Phase 2 contracts."""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
import respx

from app import config
from app.services.connectors import (CONNECTORS, ConnectorError, HNHConnector, KPConnector, OrdrisConnector, PPConnector,
                                     get_connector, iso_utc, open_connectors)
from app.services.connectors.base import CLIENT_HEADER, REDACTED, TIMEOUT_SECONDS
from app.services.connectors.pp import SCOUT_NOOP, pp_tier

PP = "https://pp.example.test"
ORDRIS = "https://ordris.example.test"
KP = "https://kp.example.test"
HNH = "https://hnh.example.test"
ENDS = datetime(2027, 10, 1)


def fake_person(**kw):
    base = {"id": 1, "person_key": "amy@example.test", "name": "Amy Archer", "pp_account_id": None,
            "organisation_links": []}
    base.update(kw)
    return SimpleNamespace(**base)


def fake_grant(**kw):
    base = {"id": 12, "product": "pp", "kind": "comp_tier", "value": "pro_buyer", "ends_at": ENDS, "remote_ref": None,
            "reason": "Mentorship, paid in full", "source": "programme:mentorship"}
    base.update(kw)
    return SimpleNamespace(**base)


def body_of(call) -> dict:
    return json.loads(call.request.content)


def test_tokens_are_test_values_only():
    """conftest replaces every product token, so no test can leak a real one."""
    assert config.PP_SERVICE_TOKEN.startswith("test-")
    assert config.ORDRIS_PARTNER_TOKEN.startswith("test-")
    assert config.KP_PARTNER_TOKEN.startswith("test-")
    assert config.HNH_PARTNER_TOKEN.startswith("test-")


# ---- dry run: every connector returns a plan and sends nothing ----------------------------------
def test_every_connector_works_in_dry_run_and_redacts_the_token():
    person = fake_person(pp_account_id=77)
    expectations = {
        "pp": (fake_grant(), "POST", f"{PP}/api/partner/grouphq/comp", "POST", f"{PP}/api/partner/grouphq/revoke"),
        "ordris": (fake_grant(product="ordris", kind="org_plan", value="starter"), "POST",
                   f"{ORDRIS}/api/partner/grouphq/entitlements", "DELETE",
                   f"{ORDRIS}/api/partner/grouphq/entitlements/grouphq%3Agrant%3A12"),
        "kp": (fake_grant(product="kp", kind="sourcer_link", value="mentee"), "POST", f"{KP}/api/partner/sourcers",
               "PATCH", f"{KP}/api/partner/sourcers/{{sourcer_id}}"),
        "hnh": (fake_grant(product="hnh", kind="partner_seat", value="area"), "POST", f"{HNH}/api/partner/partners",
                "POST", f"{HNH}/api/partner/partners/{{partner_id}}/status"),
    }
    with respx.mock(assert_all_called=False) as mock, open_connectors(dry_run=True) as pool:
        for product, (grant, g_method, g_url, r_method, r_url) in expectations.items():
            connector = pool[product]
            granted = connector.grant(grant, person, reason="Group HQ test")
            assert granted.dry_run and granted.ok and granted.plan["method"] == g_method and granted.plan["url"] == g_url, product
            revoked = connector.revoke(grant, person, reason="Group HQ test")
            assert revoked.dry_run and revoked.plan["method"] == r_method and revoked.plan["url"] == r_url, product
            looked = connector.lookup("Amy@Example.test ")
            assert looked.dry_run and looked.plan["method"] == "GET" and looked.plan["params"] == {"email": "amy@example.test"}
            expected_headers = {"Authorization": REDACTED, "X-Partner-Client": CLIENT_HEADER}
            if product == "pp":
                expected_headers["X-Service-Token"] = "[redacted]"
            for result in (granted, revoked, looked):
                text = json.dumps(result.as_dict())
                assert "test-" not in text, f"{product}: a token leaked into the plan"
                assert result.plan["headers"] == expected_headers, product
    assert mock.calls.call_count == 0, "a dry run must not touch the network"


# ---- the shared base ---------------------------------------------------------------------------
def test_live_calls_carry_bearer_and_client_header_and_never_retry():
    with respx.mock(base_url=PP) as mock:
        route = mock.get("/api/partner/grouphq/account").mock(return_value=httpx.Response(500, json={"error": "boom"}))
        with PPConnector.from_config() as pp:
            assert pp.client().timeout.read == TIMEOUT_SECONDS
            with pytest.raises(ConnectorError) as raised:
                pp.lookup("amy@example.test")
    assert route.call_count == 1, "the connector itself never retries"
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {config.PP_SERVICE_TOKEN}"
    assert request.headers["x-service-token"] == config.PP_SERVICE_TOKEN, "the header PP's job_auth reads"
    assert request.headers["x-partner-client"] == "grouphq"
    assert request.headers["accept"] == "application/json"
    assert request.url.params["email"] == "amy@example.test"
    error = raised.value
    assert error.reason == "boom" and error.status == 500
    assert error.as_dict()["ok"] is False
    assert "test-" not in json.dumps(error.as_dict()) and "test-" not in str(error)


def test_transport_errors_are_structured():
    with respx.mock(base_url=PP) as mock:
        mock.get("/api/partner/grouphq/account").mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().lookup("amy@example.test")
        assert raised.value.reason == "timeout" and raised.value.status is None
        mock.get("/api/partner/grouphq/account").mock(side_effect=httpx.ConnectError("no route"))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().lookup("amy@example.test")
        assert raised.value.reason == "unreachable" and "ConnectError" in raised.value.detail
        mock.get("/api/partner/grouphq/account").mock(return_value=httpx.Response(502, text="<html>bad gateway</html>"))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().lookup("amy@example.test")
        assert raised.value.reason == "http_502" and raised.value.body == {"raw": "<html>bad gateway</html>"}
        mock.get("/api/partner/grouphq/account").mock(
            return_value=httpx.Response(403, json={"detail": {"error": "forbidden_origin", "message": "No browser origins"}}))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().lookup("amy@example.test")
        assert raised.value.reason == "forbidden_origin" and raised.value.detail == "No browser origins"


def test_not_configured_is_an_error_not_a_crash():
    with respx.mock(assert_all_called=False) as mock:
        with pytest.raises(ConnectorError) as raised:
            PPConnector("", "").lookup("amy@example.test")
        assert raised.value.reason == "not_configured"
        with pytest.raises(ConnectorError):
            PPConnector(PP, "").lookup("amy@example.test")
    assert mock.calls.call_count == 0
    assert "token" not in repr(PPConnector(PP, "test-secret"))


def test_iso_utc_formats_naive_datetimes_and_dates():
    assert iso_utc(datetime(2027, 10, 1)) == "2027-10-01T00:00:00Z"
    assert iso_utc(datetime(2027, 10, 1).date()) == "2027-10-01T00:00:00Z"
    assert iso_utc(None) is None


def test_registry_builds_each_connector_from_config():
    assert set(CONNECTORS) == {"pp", "ordris", "kp", "hnh"}
    assert isinstance(get_connector("pp", dry_run=True), PPConnector)
    assert isinstance(get_connector("ordris"), OrdrisConnector)
    assert isinstance(get_connector("kp"), KPConnector)
    assert isinstance(get_connector("hnh"), HNHConnector)
    with pytest.raises(ConnectorError):
        get_connector("stripe")
    with open_connectors(dry_run=True) as pool:
        assert all(c.dry_run for c in pool.values())
        assert pool["pp"].base_url == PP and pool["hnh"].base_url == HNH


def test_dry_run_can_be_set_per_product(monkeypatch):
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset({"ordris", "kp", "hnh"}))
    with open_connectors() as pool:
        assert {p: c.dry_run for p, c in pool.items()} == {"pp": False, "ordris": True, "kp": True, "hnh": True}
    with open_connectors(dry_run=True) as pool:
        assert all(c.dry_run for c in pool.values()), "a dry run asked for is a dry run everywhere"
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", True)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset())
    with open_connectors() as pool:
        assert all(c.dry_run for c in pool.values())


# ---- Property Predator: the Phase 1 contract, exactly -----------------------------------------------
def test_pp_comp_sends_the_contract_and_links_the_account():
    person = fake_person()
    grant = fake_grant()
    with respx.mock(base_url=PP) as mock:
        route = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(
            200, json={"ok": True, "account_id": 77, "tier": "pro_buyer", "comp_expires_at": "2027-10-01T00:00:00+00:00"}))
        result = PPConnector.from_config().grant(grant, person, reason="Group HQ: Mentorship (grant 12)")
    assert body_of(route.calls.last) == {"email": "amy@example.test", "tier": "pro_buyer", "until": "2027-10-01T00:00:00Z",
                                         "revert_to": "free", "reason": "Group HQ: Mentorship (grant 12)"}
    assert result.ok and result.remote_ref == "77" and result.links == {"pp_account_id": 77}
    assert result.data["tier"] == "pro_buyer"


def test_pp_tier_vocabulary_maps_to_pp_keys():
    assert pp_tier("hunter") == "pro_buyer" and pp_tier("Apex") == "pro_investor" and pp_tier("business") == "pro_business"
    assert pp_tier("scout") == "free" and pp_tier("pro_buyer") == "pro_buyer"
    with pytest.raises(ConnectorError) as raised:
        pp_tier("platinum")
    assert raised.value.reason == "unsupported"
    person, grant = fake_person(), fake_grant(value="hunter")
    with respx.mock(base_url=PP) as mock:
        route = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json={"ok": True, "account_id": 1}))
        PPConnector.from_config().grant(grant, person)
    assert body_of(route.calls.last)["tier"] == "pro_buyer"


def test_pp_scout_is_a_noop_without_any_call():
    with respx.mock(assert_all_called=False) as mock:
        pp = PPConnector.from_config()
        for value in ("scout", "free"):
            granted = pp.grant(fake_grant(value=value), fake_person())
            revoked = pp.revoke(fake_grant(value=value), fake_person())
            assert granted.ok and granted.noop == SCOUT_NOOP and not granted.plan
            assert revoked.ok and revoked.noop == SCOUT_NOOP
    assert mock.calls.call_count == 0


def test_pp_paid_subscription_is_raised_as_its_own_reason():
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(409, json={"ok": False, "reason": "paid_subscription"}))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().grant(fake_grant(), fake_person())
        assert raised.value.reason == "paid_subscription" and raised.value.status == 409
        # a 200 that still says no is refused the same way
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json={"ok": False, "reason": "paid_subscription"}))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().grant(fake_grant(), fake_person())
        assert raised.value.reason == "paid_subscription"


def test_pp_revoke_contract_and_not_on_comp_is_already_done():
    with respx.mock(base_url=PP) as mock:
        route = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True, "reverted_to": "free", "account_id": 77}))
        result = PPConnector.from_config().revoke(fake_grant(), fake_person())
        assert body_of(route.calls.last) == {"email": "amy@example.test", "reason": "grouphq:revoke:12"}
        assert result.ok and result.remote_ref == "77"
        PPConnector.from_config().revoke(fake_grant(), fake_person(), reason="Left the programme")
        assert body_of(route.calls.last)["reason"] == "Left the programme", "PP logs the reason Group HQ sends"
        mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(409, json={"ok": False, "reason": "not_on_comp"}))
        result = PPConnector.from_config().revoke(fake_grant(), fake_person())
        assert result.ok and result.noop == "already off the comp"
        mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(404, json={"ok": False, "reason": "no_comp"}))
        assert PPConnector.from_config().revoke(fake_grant(), fake_person()).noop == "already off the comp"
        mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(404, json={"ok": False, "reason": "no_account"}))
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().revoke(fake_grant(), fake_person())
        assert raised.value.reason == "no_account"


def test_pp_account_lookup_and_flags():
    with respx.mock(base_url=PP) as mock:
        route = mock.get("/api/partner/grouphq/account").mock(return_value=httpx.Response(
            200, json={"account_id": 77, "tier": "free", "subscription_status": None, "comp_expires_at": None}))
        result = PPConnector.from_config().lookup("Amy@Example.test")
        assert route.calls.last.request.url.params["email"] == "amy@example.test"
        assert result.links == {"pp_account_id": 77} and result.remote_ref == "77"
        flags = mock.post("/api/partner/grouphq/flags").mock(return_value=httpx.Response(200, json={"ok": True, "account_id": 77}))
        PPConnector.from_config().grant(fake_grant(kind="flag", value="kpuk.submit"), fake_person())
        assert body_of(flags.calls.last) == {"email": "amy@example.test", "key": "kpuk.submit", "until": "2027-10-01T00:00:00Z"}
        with pytest.raises(ConnectorError) as raised:
            PPConnector.from_config().grant(fake_grant(kind="org_plan", value="starter"), fake_person())
        assert raised.value.reason == "unsupported"


# ---- Ordris, KP, HNH: the Phase 2 contracts (pending on the product side) ------------------------------
def test_ordris_contract():
    person, grant = fake_person(), fake_grant(product="ordris", kind="org_plan", value="starter")
    with respx.mock(base_url=ORDRIS) as mock:
        post = mock.post("/api/partner/grouphq/entitlements").mock(return_value=httpx.Response(
            200, json={"ok": True, "org_id": "org_abc", "user_id": "user_xyz", "external_ref": "grouphq:grant:12"}))
        result = OrdrisConnector.from_config().grant(grant, person, reason="Group HQ: Mentorship")
        assert body_of(post.calls.last) == {"email": "amy@example.test", "plan": "starter", "until": "2027-10-01T00:00:00Z",
                                            "reason": "Group HQ: Mentorship", "external_ref": "grouphq:grant:12"}
        assert result.remote_ref == "grouphq:grant:12"
        assert result.links == {"ordris_user_id": "user_xyz", "ordris_org_id": "org_abc"}
        delete = mock.delete("/api/partner/grouphq/entitlements/grouphq%3Agrant%3A12").mock(return_value=httpx.Response(200, json={"ok": True, "revoked": True}))
        assert OrdrisConnector.from_config().revoke(grant, person).ok
        assert delete.call_count == 1
        # a reference already gone answers 200 {ok: true, revoked: false} (Ordris as built); a 404 is read the same way
        mock.delete("/api/partner/grouphq/entitlements/grouphq%3Agrant%3A12").mock(return_value=httpx.Response(200, json={"ok": True, "revoked": False}))
        assert OrdrisConnector.from_config().revoke(grant, person).ok
        mock.delete("/api/partner/grouphq/entitlements/grouphq%3Agrant%3A12").mock(return_value=httpx.Response(404, json={"error": "not_found"}))
        assert OrdrisConnector.from_config().revoke(grant, person).noop == "entitlement already gone"
        lookup = mock.get("/api/partner/grouphq/lookup").mock(return_value=httpx.Response(200, json={
            "ok": True, "user_id": "user_xyz", "org_id": "org_abc", "email": "amy@example.test", "trial_end": None,
            "subscription_status": None, "entitlement": {"plan": "starter", "until": "2027-10-01T00:00:00.000Z",
                                                         "external_ref": "grouphq:grant:12"},
            "plan_active": True, "plan_source": "entitlement"}))
        assert OrdrisConnector.from_config().lookup("amy@example.test").links["ordris_org_id"] == "org_abc"
        assert lookup.calls.last.request.url.params["email"] == "amy@example.test"
    with pytest.raises(ConnectorError):
        OrdrisConnector.from_config(dry_run=True).grant(fake_grant(product="ordris", kind="org_plan", value="gold"), person)


def test_kp_contract_creates_then_patches():
    person = fake_person(pp_account_id=77, organisation_links=[SimpleNamespace(organisation=SimpleNamespace(name="Archer Homes Ltd"))])
    grant = fake_grant(product="kp", kind="sourcer_link", value="mentee")
    # The portal answers a flat row with the id at the top (kpuk-deal-portal src/routes/partner.js, sourcerOut).
    created = {"ok": True, "id": 41, "email": "amy@example.test", "name": "Amy Archer", "company": "Archer Homes Ltd",
               "source": "mentee", "pp_account_id": "77", "fee_share_pct": 50, "active": True,
               "created_at": "2026-10-20T17:32:37.327Z", "last_login": None, "created": True, "linked": False}
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(201, json=created))
        result = KPConnector.from_config().grant(grant, person, extra={"fee_share_pct": 50})
        assert body_of(post.calls.last) == {"email": "amy@example.test", "name": "Amy Archer", "company": "Archer Homes Ltd",
                                            "pp_account_id": 77, "source": "mentee", "fee_share_pct": 50}
        assert result.remote_ref == "41" and result.links == {"kp_user_id": 41}
        # a second grant with the id known re-activates instead of creating
        patch = mock.patch("/api/partner/sourcers/41").mock(return_value=httpx.Response(200, json={**created, "created": False, "linked": True}))
        KPConnector.from_config().grant(fake_grant(product="kp", kind="sourcer_link", value="mentee", remote_ref="41"), person)
        assert body_of(patch.calls.last) == {"active": True}
        KPConnector.from_config().revoke(fake_grant(product="kp", kind="sourcer_link", value="mentee", remote_ref="41"), person)
        assert body_of(patch.calls.last) == {"active": False}
        # no remote ref: look the sourcer up by email first
        lookup = mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(200, json=created))
        KPConnector.from_config().revoke(grant, person)
        assert lookup.call_count == 1 and patch.call_count == 3
        # no sourcer with that email (or an admin's email): the portal answers 404 not_found, which is "done" here
        mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(404, json={"ok": False, "error": "not_found", "message": "No sourcer with that email."}))
        assert "nothing to deactivate" in KPConnector.from_config().revoke(grant, person).noop
        assert patch.call_count == 3, "nothing is patched when there is no sourcer"
        # any other failure of the lookup is still a failure
        mock.get("/api/partner/sourcers").mock(return_value=httpx.Response(503, json={"ok": False, "error": "not_configured"}))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().revoke(grant, person)
        assert raised.value.reason == "not_configured" and raised.value.status == 503
    # fee_share_pct is only sent when the template says so
    with respx.mock(base_url=KP) as mock:
        post = mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(201, json={"id": 42}))
        KPConnector.from_config().grant(grant, fake_person())
        assert "fee_share_pct" not in body_of(post.calls.last)
        # an id that is not a whole number is a bad answer, never a path segment
        mock.post("/api/partner/sourcers").mock(return_value=httpx.Response(201, json={"id": "41/../admin"}))
        with pytest.raises(ConnectorError) as raised:
            KPConnector.from_config().grant(grant, fake_person())
        assert raised.value.reason == "bad_response"
    # a remote ref is quoted into the path, whatever it holds
    odd = KPConnector.from_config(dry_run=True).revoke(fake_grant(product="kp", kind="sourcer_link", value="mentee", remote_ref="4/1?x"), person)
    assert odd.plan["url"] == f"{KP}/api/partner/sourcers/4%2F1%3Fx"


def hnh_partner(**kw) -> dict:
    """The partner record HNH's partner_record() answers with (hellonewhome/hq app/routers/partner_api.py)."""
    base = {"id": 9, "name": "Amy Archer", "first_name": None, "email": "amy@example.test", "phone": None, "whatsapp": None,
            "whatsapp_display": None, "tier": "area", "status": "active",
            "franchise": {"fee_waived": True, "reason": "Group HQ: Mentorship"},
            "areas": [{"id": 6, "slug": "swansea", "name": "Swansea", "region": "wales", "status": "active",
                       "postcodes": ["SA1", "SA2"], "public": True}],
            "user_id": 31, "login": {"user_id": 31, "email": "amy@example.test", "is_active": True,
                                     "must_change_password": True, "last_login": None},
            "created_at": "2026-10-20T17:40:00Z"}
    base.update(kw)
    return base


def test_hnh_contract_creates_pauses_and_reactivates():
    person, grant = fake_person(), fake_grant(product="hnh", kind="partner_seat", value="area")
    created = {"ok": True, "created": True, "partner_id": 9, "user_id": 31, "one_time_password": "Tq7xK2mN9pLw",
               "area_slug": "swansea", "area_created": False, "note": "", "partner": hnh_partner()}
    with respx.mock(base_url=HNH) as mock:
        post = mock.post("/api/partner/partners").mock(return_value=httpx.Response(201, json=created))
        result = HNHConnector.from_config().grant(grant, person, reason="Group HQ: Mentorship", extra={"area_slug": "swansea"})
        assert body_of(post.calls.last) == {"email": "amy@example.test", "name": "Amy Archer", "tier": "area", "status": "active",
                                            "create_login": True, "franchise": {"fee_waived": True, "reason": "Group HQ: Mentorship"},
                                            "area_slug": "swansea"}
        assert result.remote_ref == "9" and result.links == {"hnh_partner_id": 9, "hnh_user_id": 31}
        # the temporary password is taken out of the body and handed over once, never through the stored answer
        assert "one_time_password" not in result.data and "Tq7xK2mN9pLw" not in json.dumps(result.as_dict())
        assert "Tq7xK2mN9pLw" in result.once and "amy@example.test" in result.once and "shown once" in result.once
        paused = {"ok": True, "partner_id": 9, "status": "paused", "previous_status": "active", "changed": True,
                  "partner": hnh_partner(status="paused")}
        status = mock.post("/api/partner/partners/9/status").mock(return_value=httpx.Response(200, json=paused))
        known = fake_grant(product="hnh", kind="partner_seat", value="area", remote_ref="9")
        result = HNHConnector.from_config().revoke(known, person, reason="lapsed")
        assert body_of(status.calls.last) == {"status": "paused", "reason": "lapsed"}
        assert result.remote_ref == "9" and result.once == ""
        HNHConnector.from_config().grant(known, person, reason="back")
        assert body_of(status.calls.last) == {"status": "active", "reason": "back"}
        # a resend answers 200 with one_time_password null: nothing to hand over
        mock.post("/api/partner/partners").mock(return_value=httpx.Response(200, json={**created, "created": False, "one_time_password": None}))
        assert HNHConnector.from_config().grant(grant, person, extra={"area_slug": "swansea"}).once == ""
        # no remote ref: look the partner up by email first ({ok, found, partner})
        lookup = mock.get("/api/partner/partners").mock(return_value=httpx.Response(200, json={"ok": True, "found": True, "partner": hnh_partner()}))
        HNHConnector.from_config().revoke(grant, person)
        assert lookup.call_count == 1 and status.call_count == 3
        mock.get("/api/partner/partners").mock(return_value=httpx.Response(200, json={"ok": True, "found": False, "partner": None}))
        assert "nothing to pause" in HNHConnector.from_config().revoke(grant, person).noop
        mock.get("/api/partner/partners").mock(return_value=httpx.Response(404, json={"ok": False, "reason": "no_partner"}))
        assert "nothing to pause" in HNHConnector.from_config().revoke(grant, person).noop, "a 404 lookup reads as no partner"
        assert status.call_count == 3
        postcodes = HNHConnector.from_config(dry_run=True).grant(grant, person, extra={"postcodes": ["sa1", "SA2"]})
        assert postcodes.plan["json"]["postcodes"] == ["SA1", "SA2"]
        mock.post("/api/partner/partners").mock(return_value=httpx.Response(201, json={"ok": True, "partner": {"id": "nine"}}))
        with pytest.raises(ConnectorError) as raised:
            HNHConnector.from_config().grant(grant, person)
        assert raised.value.reason == "bad_response"
    # HNH prints the name on its public pages and refuses a blank one, so a nameless person is refused here, before any call
    with respx.mock(base_url=HNH, assert_all_called=False) as mock:
        for name in ("", "   ", None):
            with pytest.raises(ConnectorError) as raised:
                HNHConnector.from_config().grant(grant, fake_person(name=name))
            assert raised.value.reason == "missing_name" and "person page" in raised.value.detail
        assert mock.calls.call_count == 0
        # a known partner is re-activated by id, so no name is needed then
        mock.post("/api/partner/partners/9/status").mock(return_value=httpx.Response(200, json={"ok": True, "partner": hnh_partner()}))
        assert HNHConnector.from_config().grant(fake_grant(product="hnh", kind="partner_seat", value="area", remote_ref="9"),
                                                fake_person(name="")).remote_ref == "9"
    odd = HNHConnector.from_config(dry_run=True).revoke(fake_grant(product="hnh", kind="partner_seat", value="area", remote_ref="9/../x"), person)
    assert odd.plan["url"] == f"{HNH}/api/partner/partners/9%2F..%2Fx/status"
    with pytest.raises(ConnectorError):
        HNHConnector.from_config(dry_run=True).grant(fake_grant(product="hnh", kind="partner_seat", value="franchisee"), person)
