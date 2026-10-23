"""Admin switches: a Live / Dry run switch per product on the Settings screen, and Pause / Resume on a grant or on all
of a person's access.

Covers: the switch precedence (CONNECTORS_DRY_RUN=1 is a hard floor; otherwise a switch once set beats the
environment list, which is only the default); not configured beats live; the nightly job reads the same switches,
fails its run when a live product has no token in its own environment, refuses a database its build does not match,
and stops sending to a product switched to Dry run while it runs; Revoke all, the import batch and the Hello New
Home hand-off follow the switches; every change is audited with who and what; a paused grant is revoked and the
recompute leaves it revoked; Resume grants again under a fresh key (and pause, resume, pause on one day asks the
product every time); a pause in dry run is only planned and Resume drops it; a failed pause revoke is retried under
the same key, and one that had given up is reported, not passed off as done; the reason typed on a pause never
leaves Group HQ; Pause all access for a mentee, new grants included; the screens (Settings, Grants, mentee, person,
import, run form) show it all, and the Live wording never promises a nightly send the job cannot make."""
from __future__ import annotations

import io
from datetime import datetime, timedelta

import httpx
import pytest
import respx

from app import config
from app.jobs import recompute as job
from app.models import AppSetting, AuditLog, ConnectorCall, EntitlementGrant, Mentee, Person
from app.services import entitlements as ent
from app.services import settings
from app.services.connectors import open_connectors, product_dry_run, product_mode
from app.services.entitlements import (at_midnight, pause_grant, pause_person, recompute_all, recompute_person, resume_grant,
                                       resume_person, revoke_person)
from tests.conftest import ADMIN_EMAIL, session
from tests.test_entitlements import COMP_OK, PP, TODAY, actions, calls_of, enrol, grants_of, make_person, make_programme, pay
from tests.test_people import flash_of

HNH = "https://hnh.example.test"

ORDRIS = "https://ordris.example.test"
KP = "https://kp.example.test"
REVOKE_OK = {"ok": True, "reverted_to": "free"}
FOUNDER = "founder@example.test"


@pytest.fixture
def live(monkeypatch):
    """The live path with respx standing in for the products (conftest forces CONNECTORS_DRY_RUN on), and the
    engine's hard floor clock pinned to TODAY, as in test_entitlements."""
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(ent, "real_today", lambda: TODAY)


def env(monkeypatch, *, floor: bool = False, dry: tuple[str, ...] = ()) -> None:
    """The server's CONNECTORS_DRY_RUN: floor is "1"; dry is the per-product list ("pp" in production today)."""
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", floor)
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset(dry))


def switch(product: str, mode: str) -> None:
    with session() as db:
        settings.set_product_switch(db, product, mode, actor=FOUNDER)
        db.commit()


def active_pp_grant(db, mock):
    """A paid mentee whose PP comp has been sent live: (person, mentee, grant, the comp route)."""
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "full")
    pay(db, mentee)
    comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
    recompute_person(db, person, TODAY, False)
    [grant] = grants_of(db, person)
    assert grant.state == "active"
    return person, mentee, grant, comp


# ---- the switch rule -------------------------------------------------------------------------------------
def test_switch_precedence_with_the_hard_floor(monkeypatch):
    env(monkeypatch, dry=("pp",))
    assert product_dry_run("pp") and not product_dry_run("ordris"), "no switch: the environment list is the default"
    assert not product_dry_run("pp", switches={"pp": "live"}), "a switch once set beats the environment list"
    assert product_dry_run("ordris", switches={"ordris": "dry_run"})
    assert product_dry_run("pp", switches={"pp": "nonsense"}), "an unreadable switch is ignored, not obeyed"
    assert product_dry_run("ordris", True, switches={"ordris": "live"}), "a dry run asked for is a dry run"
    assert product_mode("pp", switches={"pp": "live"})["source"] == "switch"
    assert product_mode("pp")["source"] == "environment"
    env(monkeypatch, floor=True)
    for product in ("pp", "ordris", "kp", "hnh"):
        assert product_dry_run(product, switches={product: "live"}), "CONNECTORS_DRY_RUN=1 is a floor no switch lifts"
        mode = product_mode(product, switches={product: "live"})
        assert mode["state"] == "dry_run" and mode["source"] == "floor" and mode["switch"] == "live"
    env(monkeypatch)
    with open_connectors(switches={"kp": "dry_run"}) as pool:
        assert [p for p, c in pool.items() if c.dry_run] == ["kp"]


def test_the_switch_is_stored_and_read_back(db, monkeypatch):
    env(monkeypatch, dry=("pp",))
    assert settings.product_switches(db) == {}
    assert settings.set_product_switch(db, "pp", "live", actor=FOUNDER) is True
    db.commit()
    assert settings.product_switches(db) == {"pp": "live"}
    assert settings.set_product_switch(db, "pp", "live", actor=FOUNDER) is False, "the same value changes nothing"
    assert settings.set_product_switch(db, "pp", "dry_run", actor=FOUNDER) is True
    db.commit()
    with session() as fresh:
        assert settings.product_switches(fresh) == {"pp": "dry_run"}
        assert fresh.get(AppSetting, "product_switch.pp").updated_by == FOUNDER
    assert settings.set_product_switch(db, "pp", "", actor=FOUNDER) is True, "cleared: the default applies again"
    db.commit()
    assert settings.product_switches(db) == {}
    [m] = [m for m in settings.product_modes(db) if m["product"] == "pp"]
    assert m["state"] == "dry_run" and m["source"] == "environment"


def test_not_configured_beats_live(admin_client, db, live, monkeypatch):
    env(monkeypatch, dry=("pp",))
    monkeypatch.setattr(config, "PP_SERVICE_TOKEN", "")
    switch("pp", "live")
    [m] = [m for m in settings.product_modes(db) if m["product"] == "pp"]
    assert m["state"] == "not_configured" and m["source"] == "switch" and not m["configured"]
    page = admin_client.get("/settings").text
    assert "status-not_configured" in page and "Not configured" in page and "Missing" in page
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(assert_all_called=False) as mock:
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "skipped")] and mock.calls.call_count == 0
        assert decisions[0].detail.startswith("not configured")
    r = admin_client.post("/settings/products/pp", data={"mode": "dry_run"})
    r = admin_client.post("/settings/products/pp", data={"mode": "live"})
    assert "token or base URL is missing" in flash_of(r.headers["location"])["ok"]
    monkeypatch.setattr(config, "PP_SERVICE_TOKEN", "test-pp")
    page = admin_client.get("/settings").text
    assert "status-live" in page


def test_the_nightly_job_reads_the_same_switch(db, live, monkeypatch):
    env(monkeypatch, dry=("pp",))       # production's default today: PP held back
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        out = io.StringIO()
        assert job.main([], out=out) == 0
        assert comp.call_count == 0 and "planned" in out.getvalue(), "no switch: PP follows CONNECTORS_DRY_RUN=pp"
        switch("pp", "live")
        out = io.StringIO()
        assert job.main([], out=out) == 0
        assert comp.call_count == 1 and "sent" in out.getvalue(), "the switch on the Settings screen sends PP live"
        assert "products: pp live" in out.getvalue()
    [grant] = grants_of(db, person)
    assert grant.state == "active"
    with session() as fresh:
        run = fresh.query(AuditLog).filter(AuditLog.action == "recompute.run").order_by(AuditLog.id.desc()).first()
        assert run.actor == "job:recompute" and run.after["products"]["pp"] == "live"
    # and the other way round: an environment that would send Ordris, held back by its switch
    env(monkeypatch)
    switch("ordris", "dry_run")
    other = make_person(db, "ola@example.test", "Ola")
    pay(db, enrol(db, other, make_programme(db, "test_ordris", [{"product": "ordris", "kind": "org_plan", "value": "starter"}]), "full"))
    with respx.mock(assert_all_called=False) as mock:
        summary = recompute_all(db, TODAY, False)
        assert mock.calls.call_count == 0
    assert summary.products["ordris"] == "dry_run" and summary.products["pp"] == "live"
    assert {(d.product, d.outcome) for d in summary.decisions if d.person_id == other.id} == {("ordris", "planned")}


def test_a_switch_does_not_lift_the_floor(db, monkeypatch):
    env(monkeypatch, floor=True)
    switch("pp", "live")
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(assert_all_called=False) as mock:
        summary = recompute_all(db, TODAY, False)
        assert mock.calls.call_count == 0
    assert summary.dry_run and summary.products["pp"] == "dry_run" and summary.outcomes == {"planned": 1}


# ---- the Settings screen ---------------------------------------------------------------------------------
def test_settings_screen_switches_and_audits(client, admin_client, monkeypatch):
    assert client.get("/settings").status_code == 303, "behind the login"
    env(monkeypatch, dry=("pp",))
    page = admin_client.get("/settings")
    assert page.status_code == 200
    for name in ("Property Predator", "Ordris", "Key Properties", "Hello New Home"):
        assert name in page.text
    assert 'href="/settings" class="is-active"' in page.text, "in the side navigation"
    assert "the server default is dry run" in page.text and "<code>pp</code>" in page.text
    assert "test-" not in page.text, "a token is never shown"
    assert page.text.count('value="live"') == 4 and page.text.count('value="dry_run"') == 4

    r = admin_client.post("/settings/products/pp", data={"mode": "live"})
    assert r.status_code == 303 and "Property Predator is live" in flash_of(r.headers["location"])["ok"]
    with session() as db:
        [row] = db.query(AuditLog).filter(AuditLog.action == "settings.product_switch").all()
        assert row.entity == "product:pp" and row.actor == ADMIN_EMAIL and row.user_id is not None
        assert row.before == {"switch": None, "state": "dry_run"}
        assert row.after["switch"] == "live" and row.after["state"] == "live"
    page = admin_client.get("/settings").text
    assert 'value="live"><button class="btn btn-ghost btn-sm" type="submit" aria-pressed="true"' in page
    assert "The switch: live" in page and ADMIN_EMAIL in page and "Use the default" in page

    r = admin_client.post("/settings/products/pp", data={"mode": "live"})
    assert "Nothing changed" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post("/settings/products/pp", data={"mode": "default"})
    assert "follows the environment default again" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post("/settings/products/pp", data={"mode": "maybe"})
    assert "err" in flash_of(r.headers["location"])
    assert admin_client.post("/settings/products/acme", data={"mode": "live"}).status_code == 404
    with session() as db:
        rows = db.query(AuditLog).filter(AuditLog.action == "settings.product_switch").order_by(AuditLog.id).all()
        assert [(r.before["switch"], r.after["switch"]) for r in rows] == [(None, "live"), ("live", None)]
        assert settings.product_switches(db) == {}

    env(monkeypatch, floor=True)
    page = admin_client.get("/settings").text
    assert "CONNECTORS_DRY_RUN=1 on this server" in page
    r = admin_client.post("/settings/products/kp", data={"mode": "live"})
    assert "still holds every product in dry run" in flash_of(r.headers["location"])["ok"]


def test_the_screens_show_the_switch_state(admin_client, monkeypatch):
    env(monkeypatch, dry=("pp",))
    switch("pp", "live")
    switch("kp", "dry_run")
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.get("/mentees/import")
        assert mock.calls.call_count == 0, "Key Properties is switched to dry run, so its list is not fetched"
    assert page.status_code == 200
    assert "Property Predator: live" in page.text and "Key Properties: dry run" in page.text and "Ordris: live" in page.text
    assert "Key Properties is in dry run here" in page.text and 'href="/settings"' in page.text
    grants = admin_client.get("/grants").text
    assert "chip chip-live" in grants and "Property Predator: live" in grants and "Key Properties: dry run" in grants
    env(monkeypatch, floor=True)
    grants = admin_client.get("/grants").text
    assert "CONNECTORS_DRY_RUN=1" in grants and "Property Predator: dry run" in grants


def test_the_settings_page_shows_what_the_nightly_job_saw(admin_client, db, live, monkeypatch):
    env(monkeypatch, dry=("pp",))
    assert "No nightly run has been recorded yet" in admin_client.get("/settings").text
    assert job.main([], out=io.StringIO()) == 0
    page = admin_client.get("/settings").text
    assert "The last nightly run" in page and "Property Predator: dry run" in page and "Ordris: live" in page


# ---- Pause and Resume on one grant -------------------------------------------------------------------------
def test_pause_revokes_now_and_the_recompute_leaves_it_revoked(db, live):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        decision = pause_grant(db, grant, actor=FOUNDER, reason="HNH area not ready")
        assert (decision.action, decision.outcome) == ("revoke", "sent") and revoke.call_count == 1
        body = revoke.calls.last.request.content
        assert b"HNH area not ready" not in body, "the reason typed on the pause stays in Group HQ"
        assert f"Group HQ: paused (grant {grant.id})".encode() in body
        [grant] = grants_of(db, person)
        assert grant.state == "revoked" and grant.held_by == FOUNDER and grant.held_reason == "HNH area not ready"
        assert grant.ends_at == at_midnight(grant.held_at.date()), "access stopped the day it was paused"
        assert actions(db, f"grant:{grant.id}")[-2:] == ["grant.pause", "grant.revoke"]
        # the rule would grant it again (the payment still covers a year); the hold keeps it off
        for _ in range(2):
            decisions = recompute_person(db, person, TODAY, False)
            assert [(d.action, d.outcome) for d in decisions] == [("held", "skipped")]
        pay(db, mentee, "500")
        summary = recompute_all(db, TODAY, False)
        assert summary.actions == {"held": 1} and "1 held" in summary.line()
        out = io.StringIO()
        assert job.main([], out=out) == 0 and "1 held" in out.getvalue()
        assert mock.calls.call_count == 2, "one grant, one revoke, nothing since"
    assert grants_of(db, person)[0].state == "revoked"


def test_resume_grants_again_under_a_fresh_key_every_time(db, live):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, comp = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        pause_grant(db, grant, actor=FOUNDER)
        first_revoke = [c for c in calls_of(db, grant) if c.action == "revoke"][-1]
        decisions = resume_grant(db, grant, TODAY, actor=FOUNDER)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")] and comp.call_count == 2
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.held_at is None and grant.held_by == ""
        regrant = calls_of(db, grant)[-1]
        assert regrant.idempotency_key == f"grant:{grant.id}:2027-10-01T00:00:00:after:{first_revoke.id}"
        assert "grant.resume" in actions(db, f"grant:{grant.id}")
        # pause and resume again the same day: the second revoke and re-grant are new calls, not matches of the first
        decision = pause_grant(db, grant, actor=FOUNDER)
        assert (decision.action, decision.outcome) == ("revoke", "sent") and revoke.call_count == 2
        second_revoke = calls_of(db, grant)[-1]
        assert second_revoke.idempotency_key == f"{first_revoke.idempotency_key}:after:{regrant.id}"
        resume_grant(db, grant, TODAY, actor=FOUNDER)
        assert comp.call_count == 3
        assert calls_of(db, grant)[-1].idempotency_key == f"grant:{grant.id}:2027-10-01T00:00:00:after:{second_revoke.id}"
        keys = [c.idempotency_key for c in calls_of(db, grant)]
        assert len(keys) == len(set(keys)) == 5 and all(c.status == "succeeded" for c in calls_of(db, grant))
        assert [(d.action, d.detail) for d in recompute_person(db, person, TODAY, False)] == [("noop", "nothing to change")]
        assert mock.calls.call_count == 5


def test_revoke_all_twice_in_one_day_asks_the_product_twice(db, live):
    """Before 0006 the second Revoke all of a day (after the founder undid the first) matched the first revoke's
    succeeded row and marked the grant revoked without asking the product: the comp stayed on in PP."""
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, comp = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        assert revoke_person(db, person, actor=FOUNDER) == {"mentees ended": 1, "revoked": 1}
        mentee.status = "active"        # undone by hand on the Notes tab
        db.commit()
        recompute_person(db, person, TODAY, False)
        assert comp.call_count == 2 and grants_of(db, person)[0].state == "active"
        assert revoke_person(db, person, actor=FOUNDER) == {"mentees ended": 1, "revoked": 1}
        assert revoke.call_count == 2, "the second revoke went to PP"
        first, second = [c.idempotency_key for c in calls_of(db, grant) if c.action == "revoke"]
        assert second.startswith(first + ":after:")


def test_a_pause_in_dry_run_is_planned_and_resume_drops_it(db, live, monkeypatch):
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        switch("pp", "dry_run")
        decision = pause_grant(db, grant, actor=FOUNDER)
        assert (decision.action, decision.outcome) == ("revoke", "planned") and revoke.call_count == 0
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.held_at is not None, "nothing was sent, so the product still has it"
        [planned] = [c for c in calls_of(db, grant) if c.action == "revoke"]
        assert planned.status == "pending" and planned.attempts == 0
        assert [(d.action, d.outcome) for d in recompute_person(db, person, TODAY, False)] == [("revoke", "planned")]
        assert len([c for c in calls_of(db, grant) if c.action == "revoke"]) == 1, "the same planned row"
        decisions = resume_grant(db, grant, TODAY, actor=FOUNDER)
        assert [(d.action, d.detail) for d in decisions] == [("noop", "nothing to change")]
        assert [c for c in calls_of(db, grant) if c.action == "revoke"] == [], "a planned revoke never goes out later"
        switch("pp", "live")
        assert [(d.action, d.outcome) for d in recompute_person(db, person, TODAY, False)] == [("noop", "skipped")]
        assert revoke.call_count == 0


def test_a_failed_pause_revoke_is_retried_under_the_same_key(db, live):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        decision = pause_grant(db, grant, actor=FOUNDER)
        assert (decision.action, decision.outcome) == ("revoke", "failed")
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.held_at is not None and grant.ends_at == datetime(2027, 10, 1), \
            "the product still holds the old window"
        revoke.mock(return_value=httpx.Response(200, json=REVOKE_OK))
        decisions = recompute_person(db, person, TODAY + timedelta(days=1), False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")]
        [row] = [c for c in calls_of(db, grant) if c.action == "revoke"]
        assert row.attempts == 2 and row.status == "succeeded", "one row, retried, not a new key every night"
        assert grants_of(db, person)[0].state == "revoked"


def test_a_pause_revokes_what_the_product_still_holds_and_nothing_else(db, live):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, comp = active_pp_grant(db, mock)
        grant.state, grant.last_error = "failed", "gave up after 5 attempts"     # extensions failed; the old window stands
        db.commit()
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        decision = pause_grant(db, grant, actor=FOUNDER)
        assert (decision.action, decision.outcome) == ("revoke", "sent") and revoke.call_count == 1
        assert grants_of(db, person)[0].state == "revoked"
    other = make_person(db, "ola@example.test", "Ola")
    pay(db, enrol(db, other, make_programme(db, "test_ordris", [{"product": "ordris", "kind": "org_plan", "value": "starter"}]), "full"))
    with respx.mock(base_url=ORDRIS) as mock:
        mock.post("/api/partner/grouphq/entitlements").mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        recompute_person(db, other, TODAY, False)
        [never] = grants_of(db, other)
        assert never.state == "failed" and mock.calls.call_count == 1
        decision = pause_grant(db, never, actor=FOUNDER)
        assert (decision.action, decision.outcome) == ("held", "skipped") and mock.calls.call_count == 1, \
            "Ordris never held it, so there is nothing to revoke"


def test_grant_page_pause_and_resume(admin_client, db, live):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        detail = admin_client.get(f"/grants/{grant.id}").text
        assert f'action="/grants/{grant.id}/pause"' in detail
        r = admin_client.post(f"/grants/{grant.id}/pause", data={"reason": "Chasing the contract", "next": "//elsewhere.example/x"})
        assert r.status_code == 303 and r.headers["location"].startswith(f"/grants/{grant.id}?")
        assert "Paused: revoked in Property Predator now" in flash_of(r.headers["location"])["ok"]
        detail = admin_client.get(f"/grants/{grant.id}").text
        assert "status-held" in detail and "Chasing the contract" in detail and f'action="/grants/{grant.id}/resume"' in detail
        r = admin_client.post(f"/grants/{grant.id}/pause", data={"next": f"/mentees/{mentee.id}?tab=grants"})
        assert r.headers["location"].startswith(f"/mentees/{mentee.id}?tab=grants&")
        assert "already paused" in flash_of(r.headers["location"])["ok"]
        listing = admin_client.get("/grants", params={"state": "held"}).text
        assert "1 grant matching" in listing and "status-held" in listing and "1 held" in listing
        page = admin_client.post(f"/grants/{grant.id}/resume", data={"next": f"/mentees/{mentee.id}?tab=grants"})
        assert page.status_code == 200 and "resume for amy@example.test" in page.text
        assert 'class="status status-sent"' in page.text
        assert f'href="/mentees/{mentee.id}?tab=grants"' in page.text
        r = admin_client.post(f"/grants/{grant.id}/resume")
        assert r.status_code == 303 and "not paused" in flash_of(r.headers["location"])["err"]
    with session() as fresh:
        row = fresh.query(AuditLog).filter(AuditLog.action == "grant.pause").one()
        assert row.actor == ADMIN_EMAIL and row.user_id is not None and row.after["held_reason"] == "Chasing the contract"
        assert fresh.get(EntitlementGrant, grant.id).state == "active"


# ---- Pause all access for a mentee ---------------------------------------------------------------------------
def test_mentee_pause_all_access_holds_everything_until_resumed(admin_client, db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db, "test_two", [
        {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
        {"product": "pp", "kind": "flag", "value": "kpuk.submit"},
    ]), "full")
    pay(db, mentee)
    with respx.mock(assert_all_called=False) as mock:
        comp = mock.post(f"{PP}/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        flag = mock.post(f"{PP}/api/partner/grouphq/flags").mock(return_value=httpx.Response(200, json={"ok": True, "account_id": 77}))
        revoke = mock.post(f"{PP}/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        flag_off = mock.post(f"{PP}/api/partner/grouphq/flags/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        ordris = mock.post(f"{ORDRIS}/api/partner/grouphq/entitlements").mock(
            return_value=httpx.Response(200, json={"ok": True, "org_id": "org_1"}))
        recompute_person(db, person, TODAY, False)
        assert {g.state for g in grants_of(db, person)} == {"active"}

        tab = admin_client.get(f"/mentees/{mentee.id}", params={"tab": "grants"}).text
        assert "Pause all access" in tab and tab.count(">Pause</button>") == 2
        r = admin_client.post(f"/mentees/{mentee.id}/pause", data={"reason": "Franchise agreement unsigned"})
        assert r.status_code == 303 and "All access paused" in flash_of(r.headers["location"])["ok"]
        assert revoke.call_count == 1 and flag_off.call_count == 1
        assert all(b"Franchise agreement unsigned" not in call.request.content for call in mock.calls), \
            "the reason stays in Group HQ"
        assert b"Group HQ: all access paused (grant " in revoke.calls.last.request.content
        db.expire_all()
        assert {g.state for g in grants_of(db, person)} == {"revoked"}
        assert db.get(Person, person.id).access_held_reason == "Franchise agreement unsigned"
        assert db.get(Mentee, mentee.id).status == "active", "unlike Revoke all, the mentee is not ended"
        assert "person.pause" in actions(db, f"person:{person.id}") and "mentee.pause_all" in actions(db, f"mentee:{mentee.id}")

        tab = admin_client.get(f"/mentees/{mentee.id}", params={"tab": "grants"}).text
        assert "All access is paused" in tab and "Franchise agreement unsigned" in tab and "Resume all access" in tab
        assert tab.count("status-held") == 2 and f'action="/mentees/{mentee.id}/pause"' not in tab
        person_page = admin_client.get(f"/people/{person.id}").text
        assert "All access is paused" in person_page and person_page.count("status-held") == 2
        assert "2 grants matching" in admin_client.get("/grants", params={"state": "held"}).text

        # the nightly rule grants nothing back, not even a right a programme adds while the hold is on
        pay(db, enrol(db, person, make_programme(db, "test_ordris", [{"product": "ordris", "kind": "org_plan", "value": "starter"}]), "full"))
        decisions = recompute_person(db, person, TODAY, False)
        assert sorted((d.product, d.action) for d in decisions) == [("ordris", "held"), ("pp", "held"), ("pp", "held")]
        assert ordris.call_count == 0 and comp.call_count == 1 and flag.call_count == 1
        r = admin_client.post(f"/grants/{grants_of(db, person)[0].id}/resume")
        assert "all access for the person is paused" in flash_of(r.headers["location"])["err"]

        page = admin_client.post(f"/mentees/{mentee.id}/resume")
        assert page.status_code == 200 and "resume for amy@example.test" in page.text
        assert page.text.count('class="status status-sent"') == 3
        assert comp.call_count == 2 and flag.call_count == 2 and ordris.call_count == 1
    db.expire_all()
    assert {g.state for g in grants_of(db, person)} == {"active"}
    assert db.get(Person, person.id).access_held_at is None
    assert "person.resume" in actions(db, f"person:{person.id}") and "mentee.resume_all" in actions(db, f"mentee:{mentee.id}")
    r = admin_client.post(f"/mentees/{mentee.id}/resume")
    assert r.status_code == 303 and "not paused" in flash_of(r.headers["location"])["err"]


def test_a_grant_paused_on_its_own_stays_paused_when_all_access_resumes(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db, "test_two", [
        {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
        {"product": "ordris", "kind": "org_plan", "value": "starter"},
    ]), "full"))
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{PP}/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        mock.post(f"{PP}/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        mock.post(f"{ORDRIS}/api/partner/grouphq/entitlements").mock(return_value=httpx.Response(200, json={"ok": True, "org_id": "org_1"}))
        mock.delete(url__regex=rf"^{ORDRIS}/api/partner/grouphq/entitlements/.+").mock(
            return_value=httpx.Response(200, json={"ok": True, "revoked": True}))
        recompute_person(db, person, TODAY, False)
        pp_grant, ordris_grant = grants_of(db, person)
        pause_grant(db, pp_grant, actor=FOUNDER)
        assert pause_person(db, person, actor=FOUNDER) == {"already off": 1, "revoked": 1}
        decisions = resume_person(db, person, TODAY, actor=FOUNDER)
        assert {(d.product, d.action) for d in decisions} == {("pp", "held"), ("ordris", "grant")}
    pp_grant, ordris_grant = grants_of(db, person)
    assert pp_grant.state == "revoked" and pp_grant.held_at is not None and ordris_grant.state == "active"
    with session() as fresh:
        assert fresh.query(ConnectorCall).filter(ConnectorCall.status != "succeeded").count() == 0


# ---- the nightly job's own environment ---------------------------------------------------------------------
def test_a_live_product_without_its_token_on_the_nightly_job_fails_the_run_and_says_so(admin_client, db, live, monkeypatch):
    """render.yaml copies the Ordris, Key Properties and Hello New Home tokens to the recompute cron, not PP's: a PP
    switched live before its token reaches the cron must not be promised nightly sends, and the cron must fail."""
    env(monkeypatch, dry=("pp",))
    token = config.PP_SERVICE_TOKEN
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    page = admin_client.get("/settings").text
    assert "No nightly run has shown yet whether the recompute cron holds PP_SERVICE_TOKEN" in page and "not seen yet" in page
    switch("pp", "live")
    monkeypatch.setattr(config, "PP_SERVICE_TOKEN", "")          # the cron's environment
    with respx.mock(assert_all_called=False) as mock:
        out = io.StringIO()
        assert job.main([], out=out) == 1, "the cron run shows as failed on Render"
        assert mock.calls.call_count == 0
    assert "products: pp not configured" in out.getvalue()
    assert "warning: pp is not in dry run, but this job has no token or base URL for it" in out.getvalue()
    with session() as fresh:
        run = fresh.query(AuditLog).filter(AuditLog.action == "recompute.run").one()
        assert run.after["configured"]["pp"] is False and run.after["configured"]["ordris"] is True

    monkeypatch.setattr(config, "PP_SERVICE_TOKEN", token)       # back on the web service, which holds it
    page = admin_client.get("/settings").text
    assert "The nightly job cannot send to it: add PP_SERVICE_TOKEN to the recompute cron" in page
    assert page.count("The nightly job cannot send to it") == 1, "only PP's row"
    admin_client.post("/settings/products/pp", data={"mode": "dry_run"})
    r = admin_client.post("/settings/products/pp", data={"mode": "live"})
    err = flash_of(r.headers["location"])["err"]
    assert "only Group HQ's buttons can send to it" in err and "PP_SERVICE_TOKEN" in err and "group-hq-recompute" in err

    with respx.mock(base_url=PP) as mock:                         # the token reaches the cron
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        assert job.main([], out=io.StringIO()) == 0 and comp.call_count == 1
    page = admin_client.get("/settings").text
    assert "The nightly job cannot send to it" not in page and "not seen yet" not in page
    admin_client.post("/settings/products/pp", data={"mode": "dry_run"})
    r = admin_client.post("/settings/products/pp", data={"mode": "live"})
    assert "the nightly one included" in flash_of(r.headers["location"])["ok"]


def test_the_nightly_job_refuses_a_database_its_build_does_not_match(db, live, monkeypatch):
    """The cron deploys apart from the web service: a build that does not match the schema would ignore what the
    schema holds (the holds and the switches), so it does nothing at all and fails."""
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    monkeypatch.setattr(job.migrate, "current_revision", lambda: "0005_district_cost_share")
    with respx.mock(assert_all_called=False) as mock:
        out = io.StringIO()
        assert job.main([], out=out) == 1
        assert mock.calls.call_count == 0
    assert "recompute refused" in out.getvalue() and "0005_district_cost_share" in out.getvalue()
    assert "0007_growth" in out.getvalue(), "the build's own head is named"
    assert grants_of(db, person) == []
    with session() as fresh:
        assert fresh.query(AuditLog).filter(AuditLog.action == "recompute.run").count() == 0


def test_a_dry_run_of_the_job_is_recorded_as_one_and_not_taken_for_the_nightly_view(admin_client, db, live, monkeypatch):
    env(monkeypatch, dry=("pp",))
    assert job.main([], out=io.StringIO()) == 0
    assert job.main(["--dry-run"], out=io.StringIO()) == 0
    with session() as fresh:
        last = fresh.query(AuditLog).filter(AuditLog.action == "recompute.run").order_by(AuditLog.id.desc()).first()
        assert last.after["dry_run"] is True and set(last.after["products"].values()) == {"dry_run"}
    page = admin_client.get("/settings").text
    assert "Ordris: live" in page, "the panel shows the last real run, not a dry run from a shell"


def test_a_dry_run_switch_stops_a_run_under_way_and_a_pause_counts_mid_run(db, live, monkeypatch):
    env(monkeypatch)                    # every product live by default
    programme = make_programme(db)
    people = [make_person(db, f"p{i}@example.test", f"P{i}") for i in range(3)]
    for person in people:
        pay(db, enrol(db, person, programme, "full"))
    first, second, third = people
    real = ent.recompute_person

    def pressed_during_the_run(db_, person, *args, **kwargs):
        decisions = real(db_, person, *args, **kwargs)
        if person.id == first.id:       # after the run has loaded everyone: Dry run for PP, Pause all access for P2
            switch("pp", "dry_run")
            with session() as other:
                other.get(Person, third.id).access_held_at = datetime(2026, 10, 20, 3, 15)
                other.commit()
        return decisions

    monkeypatch.setattr(ent, "recompute_person", pressed_during_the_run)
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        out = io.StringIO()
        assert job.main([], out=out) == 0
        assert comp.call_count == 1, "only the first person's comp went out before the switch"
    assert "stopped: pp switched to dry run during the run" in out.getvalue()
    with session() as fresh:
        run = fresh.query(AuditLog).filter(AuditLog.action == "recompute.run").one()
        assert run.after["stopped"] == ["pp"] and run.after["products"]["pp"] == "live"
    assert [g.state for g in grants_of(db, first)] == ["active"]
    [planned] = calls_of(db, grants_of(db, second)[0])
    assert planned.status == "pending" and planned.attempts == 0, "planned, not sent"
    [held] = grants_of(db, third)
    assert held.state == "pending" and calls_of(db, held) == [], "the pause pressed mid-run counted"


# ---- every path that sends follows the switches --------------------------------------------------------------
def test_revoke_all_follows_the_switch(db, live, monkeypatch):
    env(monkeypatch)                    # PP live by the environment
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json=REVOKE_OK))
        switch("pp", "dry_run")
        assert revoke_person(db, person, TODAY, actor=FOUNDER) == {"mentees ended": 1, "planned": 1}
        assert revoke.call_count == 0


def test_an_import_batch_follows_the_switches(admin_client, monkeypatch):
    from tests.test_mentee_import import form, mock_products
    env(monkeypatch, dry=("pp",))       # production's default; Key Properties is held back by its switch only
    switch("kp", "dry_run")
    created = {"ok": True, "created": True, "partner_id": 9, "user_id": 31,
               "partner": {"id": 9, "user_id": 31, "email": "dee@example.test", "name": "Dee New", "status": "active"}}
    with respx.mock(assert_all_called=False) as mock:
        mock_products(mock)
        kp_post = mock.post(f"{KP}/api/partner/sourcers").mock(return_value=httpx.Response(500))
        ordris_post = mock.post(f"{ORDRIS}/api/partner/grouphq/entitlements").mock(
            return_value=httpx.Response(200, json={"ok": True, "user_id": "user_1", "org_id": "org_1", "plan": "starter"}))
        hnh_post = mock.post(f"{HNH}/api/partner/partners").mock(return_value=httpx.Response(201, json=created))
        r = admin_client.post("/mentees/import", data=form("Dee New, dee@example.test", amount="2000"))
        kp_calls = [c for c in mock.calls if c.request.url.host == "kp.example.test"]
    assert r.status_code == 200, r.text[:600]
    assert kp_calls == [] and kp_post.call_count == 0, "Key Properties is neither looked up nor sent a grant"
    assert ordris_post.call_count == hnh_post.call_count == 1
    assert r.text.count('class="status status-sent"') == 2 and r.text.count('class="status status-planned"') == 3


def test_the_hnh_handoff_follows_the_switch(admin_client, monkeypatch):
    from tests.test_franchise import create_opportunity, move, pay_and_clear, seed_districts
    seed_districts()
    oid = create_opportunity(admin_client, ["SP10"], email="jo@example.test", name="Jo Andover")
    pay_and_clear(admin_client, oid, "3000.00")
    assert move(admin_client, oid, "paid").status_code == 303
    env(monkeypatch)                    # Hello New Home live by the environment
    switch("hnh", "dry_run")
    with respx.mock(assert_all_called=False) as mock:
        r = admin_client.post(f"/franchise/{oid}/hnh")
        assert mock.calls.call_count == 0
    assert r.status_code == 200 and "Dry run: nothing was sent" in r.text
    with session() as db:
        call = db.query(ConnectorCall).filter(ConnectorCall.idempotency_key == f"franchise:{oid}:hnh_partner").one()
        assert call.status == "pending"


# ---- a pause that could not stop access says so ---------------------------------------------------------------
def test_a_pause_after_the_revoke_gave_up_says_so_and_retry_sends_it(admin_client, db, live):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        revoke_person(db, person, TODAY, actor=FOUNDER)
        for _ in range(4):
            recompute_person(db, person, TODAY, False)
        [grant] = grants_of(db, person)
        [row] = [c for c in calls_of(db, grant) if c.action == "revoke"]
        assert row.attempts == 5 and grant.state == "failed" and revoke.call_count == 5

        r = admin_client.post(f"/mentees/{mentee.id}/pause")
        flash = flash_of(r.headers["location"])
        assert "ok" not in flash and "not every revoke went through" in flash["err"] and "gave up 1" in flash["err"]
        r = admin_client.post(f"/grants/{grant.id}/pause", data={"next": f"/grants/{grant.id}"})
        err = flash_of(r.headers["location"])["err"]
        assert "was not asked again" in err and "may still give access" in err and "Retry" in err
        assert revoke.call_count == 5, "nothing more was sent"

        revoke.mock(return_value=httpx.Response(200, json=REVOKE_OK))
        page = admin_client.post(f"/grants/{grant.id}/retry")
        assert page.status_code == 200 and revoke.call_count == 6
    [grant] = grants_of(db, person)
    assert grant.state == "revoked" and grant.held_at is not None


def test_use_the_default_and_dry_run_ask_first_when_they_change_what_is_sent(admin_client, monkeypatch):
    env(monkeypatch, dry=("pp",))
    switch("ordris", "dry_run")
    switch("pp", "live")

    def row(page: str, name: str) -> str:
        return page.split(f"<strong>{name}</strong>")[1].split("</tr>")[0]

    page = admin_client.get("/settings").text
    ordris = row(page, "Ordris")
    assert "Use the default (live)" in ordris and ordris.count("data-confirm=") == 2, \
        "back to a default of live asks as Live does; Dry run is already on, so it does not ask"
    pp = row(page, "Property Predator")
    assert "Use the default (dry run)" in pp and pp.count("data-confirm=") == 3 and "Switch Property Predator to dry run?" in pp
    kp = row(page, "Key Properties")
    assert "Use the default" not in kp and kp.count("data-confirm=") == 2
    r = admin_client.post("/settings/products/ordris", data={"mode": "default"})
    assert "follows the environment default again" in flash_of(r.headers["location"])["ok"]
    r = admin_client.post("/settings/products/kp", data={"mode": "dry_run"})
    assert "a run already under way included" in flash_of(r.headers["location"])["ok"]
    env(monkeypatch, floor=True)
    page = admin_client.get("/settings").text
    assert "Save Ordris&#39;s switch as live?" in row(page, "Ordris") and "Use the default (" not in page


def test_a_pause_with_no_token_here_says_the_product_may_still_give_access(admin_client, db, live, monkeypatch):
    with respx.mock(base_url=PP) as mock:
        person, mentee, grant, _ = active_pp_grant(db, mock)
        monkeypatch.setattr(config, "PP_SERVICE_TOKEN", "")
        r = admin_client.post(f"/grants/{grant.id}/pause")
        assert mock.calls.call_count == 1, "only the comp; the revoke could not be sent"
    flash = flash_of(r.headers["location"])
    assert "ok" not in flash and "may still give access" in flash["err"]
    [grant] = grants_of(db, person)
    assert grant.state == "active" and grant.held_at is not None
