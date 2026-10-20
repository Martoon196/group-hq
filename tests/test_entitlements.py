"""The entitlement rule and the recompute, with a fixed clock (TODAY) and respx in place of the products.

Covers: full paid then 12 months; monthly current; monthly lapsed revokes; the grace window; a failed call
is retried on the next run and given up after five; a 409 paid_subscription blocks and is never retried on
its own; the idempotency key stops a duplicate call on a re-run; a dry run makes no HTTP call; a Scout
tier is recorded as a no-op; plus the grants screens and the command-line job. Then the review guards: the
400 day ceiling, the hard floor on a back-dated run, a failed or dry-run extension is sent next time, a
re-grant after a revoke gets its own key, orphans are revoked before targets, one PP comp per account,
per-product dry run, unconfigured products, the run lock and the programme price."""
from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from app import config
from app.jobs import recompute as job
from app.models import AuditLog, ConnectorCall, EntitlementGrant, Mentee, Payment, Person, Programme
from app.services import entitlements as ent
from app.services.entitlements import (BLOCKED_ERROR, LOCK_RUN, MAX_ATTEMPTS, MAX_GRANT_DAYS, RUN_IN_PROGRESS, add_months,
                                       compute_access_end, display_state, full_term_months, grant_key, is_blocked,
                                       recompute_all, recompute_person, reset_for_retry, revoke_key, revoke_person)
from tests.conftest import session

TODAY = date(2026, 10, 20)
PP = "https://pp.example.test"
COMP_OK = {"ok": True, "account_id": 77, "tier": "pro_buyer", "comp_expires_at": "2027-10-01T00:00:00+00:00"}
PP_ONLY = [{"product": "pp", "kind": "comp_tier", "value": "pro_buyer"}]


@pytest.fixture
def live(monkeypatch):
    """conftest forces CONNECTORS_DRY_RUN on; these tests need the live path with respx standing in for PP. The
    engine's hard floor reads the real clock; it is pinned to TODAY so the fixed dates below stay valid."""
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN", False)
    monkeypatch.setattr(ent, "real_today", lambda: TODAY)


def clock(monkeypatch, day: date) -> None:
    """Move the real clock the hard floor reads (a run as of an earlier day needs it there too)."""
    monkeypatch.setattr(ent, "real_today", lambda: day)


# ---- builders -----------------------------------------------------------------------------------
def make_person(db, email="amy@example.test", name="Amy Archer") -> Person:
    person = Person(person_key=email, name=name)
    db.add(person)
    db.commit()
    return person


def make_programme(db, code="test_pp", template=None, name=None) -> Programme:
    programme = db.query(Programme).filter(Programme.code == code).first()
    if programme is None:
        programme = Programme(code=code, name=name or code.replace("_", " ").title(), grant_template=template or PP_ONLY)
        db.add(programme)
    else:
        programme.grant_template = template or PP_ONLY
    db.commit()
    return programme


def enrol(db, person, programme, mode="full", start=date(2026, 10, 1), status="active") -> Mentee:
    mentee = Mentee(person_id=person.id, programme_id=programme.id, payment_mode=mode, start_date=start, status=status)
    db.add(mentee)
    db.commit()
    return mentee


def pay(db, mentee, amount="1000", paid=None, cleared=True, period=None) -> Payment:
    paid_at = paid or datetime(2026, 10, 1, 9, 0)
    payment = Payment(mentee_id=mentee.id, amount=Decimal(amount), paid_at=paid_at,
                      cleared_at=paid_at + timedelta(hours=1) if cleared else None,
                      period_start=period[0] if period else None, period_end=period[1] if period else None)
    db.add(payment)
    db.commit()
    return payment


def grants_of(db, person) -> list[EntitlementGrant]:
    db.expire_all()
    return db.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id).order_by(EntitlementGrant.id).all()


def calls_of(db, grant) -> list[ConnectorCall]:
    db.expire_all()
    return db.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).order_by(ConnectorCall.id).all()


def actions(db, entity) -> list[str]:
    return [row.action for row in db.query(AuditLog).filter(AuditLog.entity == entity).order_by(AuditLog.id).all()]


def body_of(call) -> dict:
    return json.loads(call.request.content)


# ---- the rule -----------------------------------------------------------------------------------------
def test_full_paid_gives_twelve_months_once_a_payment_has_cleared(db):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "full", date(2026, 10, 1))
    assert compute_access_end(mentee, [], TODAY) is None
    pending = pay(db, mentee, cleared=False)
    assert compute_access_end(mentee, [pending], TODAY) is None, "an uncleared payment is not a payment"
    cleared = pay(db, mentee)
    assert compute_access_end(mentee, [pending, cleared], TODAY) == date(2027, 10, 1)
    late = enrol(db, person, make_programme(db), "full", date(2026, 1, 31))
    assert compute_access_end(late, [pay(db, late)], TODAY) == date(2027, 1, 31)


def test_monthly_follows_the_latest_cleared_period_plus_grace(db):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 10, 1))
    assert compute_access_end(mentee, [], TODAY) is None
    first = pay(db, mentee, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    assert compute_access_end(mentee, [first], TODAY) == date(2026, 11, 7)
    second = pay(db, mentee, "150", paid=datetime(2026, 11, 1), period=(date(2026, 11, 1), date(2026, 11, 30)))
    assert compute_access_end(mentee, [second, first], TODAY) == date(2026, 12, 7)
    unperioded = pay(db, mentee, "150", paid=datetime(2026, 12, 1))
    assert compute_access_end(mentee, [first, second, unperioded], TODAY) == date(2026, 12, 7), \
        "a cleared monthly payment without a period_end cannot extend anything"
    uncleared = pay(db, mentee, "150", paid=datetime(2026, 12, 1), cleared=False, period=(date(2026, 12, 1), date(2026, 12, 31)))
    assert compute_access_end(mentee, [first, second, uncleared], TODAY) == date(2026, 12, 7)


def test_add_months_clamps_to_the_month_end():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert add_months(date(2026, 10, 1), 12) == date(2027, 10, 1)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_a_fixed_term_item_never_outlives_the_mentee(db):
    person = make_person(db)
    programme = make_programme(db, "test_terms", [{"product": "pp", "kind": "comp_tier", "value": "pro_buyer", "months": 3},
                                                  {"product": "ordris", "kind": "org_plan", "value": "starter"}])
    mentee = enrol(db, person, programme, "full", date(2026, 10, 1))
    pay(db, mentee)
    targets = ent.targets_for(db, person, TODAY)
    assert targets[("pp", "comp_tier", "pro_buyer")].ends == date(2027, 1, 1)
    assert targets[("ordris", "org_plan", "starter")].ends == date(2027, 10, 1)
    monthly = enrol(db, make_person(db, "ben@example.test"), programme, "monthly", date(2026, 10, 1))
    pay(db, monthly, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    targets = ent.targets_for(db, monthly.person, TODAY)
    assert targets[("pp", "comp_tier", "pro_buyer")].ends == date(2026, 11, 7), "capped by what has been paid"


# ---- recompute through the PP connector ----------------------------------------------------------------
def test_full_paid_grants_hunter_for_twelve_months(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db, name="Mentorship"), "full", date(2026, 10, 1))
    pay(db, mentee)
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
    assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")]
    assert comp.call_count == 1
    assert body_of(comp.calls.last) == {"email": "amy@example.test", "tier": "pro_buyer", "until": "2027-10-01T00:00:00Z",
                                        "revert_to": "free", "reason": "Group HQ: Mentorship, paid in full (grant 1)"}
    [grant] = grants_of(db, person)
    assert grant.state == "active" and grant.ends_at == datetime(2027, 10, 1) and grant.starts_at == datetime(2026, 10, 1)
    assert grant.remote_ref == "77" and grant.last_error == "" and grant.last_sync_at is not None
    assert grant.source == "programme:test_pp" and grant.mentee_id == mentee.id
    db.refresh(person)
    assert person.pp_account_id == 77, "the account id PP returned fills the shared person id"
    [call] = calls_of(db, grant)
    assert call.status == "succeeded" and call.attempts == 1 and call.idempotency_key == grant_key(grant.id, grant.ends_at)
    assert call.idempotency_key == f"grant:{grant.id}:2027-10-01T00:00:00"
    assert call.payload["request"]["json"]["tier"] == "pro_buyer" and call.payload["request"]["headers"]["Authorization"] == "Bearer [redacted]"
    assert call.response["remote_ref"] == "77"
    assert actions(db, f"grant:{grant.id}") == ["grant.grant"]
    db.refresh(mentee)
    assert mentee.status == "active"


def test_monthly_current_grants_until_period_end_plus_grace(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 10, 1))
    pay(db, mentee, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        recompute_person(db, person, TODAY, False)
    assert body_of(comp.calls.last)["until"] == "2026-11-07T00:00:00Z"
    [grant] = grants_of(db, person)
    assert grant.state == "active" and grant.ends_at == datetime(2026, 11, 7)


def test_monthly_lapsed_revokes_and_a_late_payment_restores(db, live, monkeypatch):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 9, 1))
    pay(db, mentee, "150", paid=datetime(2026, 9, 1), period=(date(2026, 9, 1), date(2026, 9, 30)))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True, "reverted_to": "free"}))
        clock(monkeypatch, date(2026, 10, 1))
        recompute_person(db, person, date(2026, 10, 1), False)       # granted until 7 October
        clock(monkeypatch, TODAY)
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.ends_at == datetime(2026, 10, 7)
        decisions = recompute_person(db, person, TODAY, False)         # 20 October: lapsed
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")]
        revoked = body_of(revoke.calls.last)
        assert revoked["email"] == "amy@example.test" and revoked["reason"].startswith("Group HQ:"), "PP logs the reason"
        [grant] = grants_of(db, person)
        assert grant.state == "revoked" and grant.ends_at == datetime(2026, 10, 7)
        db.refresh(mentee)
        assert mentee.status == "lapsed"
        keys = [c.idempotency_key for c in calls_of(db, grant)]
        assert keys == [grant_key(grant.id, datetime(2026, 10, 7)), revoke_key(grant.id, datetime(2026, 10, 7))]
        assert actions(db, f"grant:{grant.id}") == ["grant.grant", "grant.revoke"]
        assert "mentee.lapse" in actions(db, f"mentee:{mentee.id}")

        # a late payment entered by hand restores access on the next run, under a new key
        pay(db, mentee, "150", paid=datetime(2026, 10, 19), period=(date(2026, 10, 1), date(2026, 10, 31)))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")]
        assert comp.call_count == 2 and body_of(comp.calls.last)["until"] == "2026-11-07T00:00:00Z"
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.ends_at == datetime(2026, 11, 7)
        db.refresh(mentee)
        assert mentee.status == "active"
        assert "mentee.reactivate" in actions(db, f"mentee:{mentee.id}")

        # and lapsing again later needs its own revoke row, not the one already used
        decisions = recompute_person(db, person, date(2026, 11, 20), False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")]
        assert revoke.call_count == 2
        assert len(calls_of(db, grant)) == 4


def test_grace_window_is_seven_days_after_period_end(db, live, monkeypatch):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 9, 1))
    payment = pay(db, mentee, "150", paid=datetime(2026, 9, 1), period=(date(2026, 9, 1), date(2026, 10, 13)))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        # period ended 13 October: access ends at the start of 20 October, so on the 19th it is still on
        clock(monkeypatch, date(2026, 10, 19))
        decisions = recompute_person(db, person, date(2026, 10, 19), False)
        assert decisions[0].action == "grant" and body_of(comp.calls.last)["until"] == "2026-10-20T00:00:00Z"
        clock(monkeypatch, TODAY)
        # on the 20th the grace has run out
        decisions = recompute_person(db, person, TODAY, False)
        assert decisions[0].action == "revoke" and revoke.call_count == 1
        # one more day on the period keeps them through the 20th
        payment.period_end = date(2026, 10, 14)
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert decisions[0].action == "grant" and body_of(comp.calls.last)["until"] == "2026-10-21T00:00:00Z"


def test_extension_when_the_product_holds_a_shorter_end(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 10, 1))
    pay(db, mentee, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        recompute_person(db, person, TODAY, False)
        pay(db, mentee, "150", paid=datetime(2026, 10, 20), period=(date(2026, 11, 1), date(2026, 11, 30)))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("extend", "sent")]
        assert comp.call_count == 2 and body_of(comp.calls.last)["until"] == "2026-12-07T00:00:00Z"
        [grant] = grants_of(db, person)
        assert grant.ends_at == datetime(2026, 12, 7) and len(calls_of(db, grant)) == 2
        assert actions(db, f"grant:{grant.id}") == ["grant.grant", "grant.extend"]
        # a shorter end (a payment taken back) is not sent: the revoke enforces it when the day comes, and the
        # row keeps the window the product confirmed
        db.delete(mentee.payments[-1])
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("noop", "skipped")]
        assert comp.call_count == 2
        assert grants_of(db, person)[0].ends_at == datetime(2026, 12, 7)
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        decisions = recompute_person(db, person, date(2026, 11, 7), False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")] and revoke.call_count == 1
        assert grants_of(db, person)[0].ends_at == datetime(2026, 11, 7)


def test_failed_call_is_retried_next_run_and_given_up_after_five(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        for attempt in range(1, MAX_ATTEMPTS + 1):
            decisions = recompute_person(db, person, TODAY, False)
            assert [(d.action, d.outcome) for d in decisions] == [("grant", "failed")], attempt
            [grant] = grants_of(db, person)
            [call] = calls_of(db, grant)
            assert grant.state == "failed" and "bad_gateway" in grant.last_error and not is_blocked(grant)
            assert call.status == "failed" and call.attempts == attempt and comp.call_count == attempt
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("gave_up", "skipped")]
        assert comp.call_count == MAX_ATTEMPTS, "after five attempts the job stops trying"
        assert actions(db, f"grant:{grant.id}").count("grant.fail") == MAX_ATTEMPTS
        # Retry clears the count and the next run tries again
        reset_for_retry(db, grant, actor="founder@example.test")
        db.commit()
        comp.mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")]
        assert grants_of(db, person)[0].state == "active"


def test_an_active_grant_keeps_its_access_when_a_revoke_fails(db, live, monkeypatch):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 9, 1))
    pay(db, mentee, "150", paid=datetime(2026, 9, 1), period=(date(2026, 9, 1), date(2026, 9, 30)))
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(side_effect=httpx.ConnectError("down"))
        clock(monkeypatch, date(2026, 10, 1))
        recompute_person(db, person, date(2026, 10, 1), False)
        clock(monkeypatch, TODAY)
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "failed")]
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.last_error.startswith("unreachable")
        revoke.mock(return_value=httpx.Response(200, json={"ok": True}))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")]
        assert grants_of(db, person)[0].state == "revoked"


def test_paid_subscription_blocks_and_is_never_retried_on_its_own(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(409, json={"ok": False, "reason": "paid_subscription"}))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("blocked", "blocked")]
        [grant] = grants_of(db, person)
        assert is_blocked(grant) and grant.state == "failed" and grant.last_error == BLOCKED_ERROR
        assert display_state(grant) == "blocked"
        [call] = calls_of(db, grant)
        assert call.status == "failed" and call.attempts == MAX_ATTEMPTS and call.response["error"] == "paid_subscription"
        assert actions(db, f"grant:{grant.id}") == ["grant.block"]
        for _ in range(3):
            decisions = recompute_person(db, person, TODAY, False)
            assert [(d.action, d.outcome) for d in decisions] == [("blocked", "skipped")]
        assert comp.call_count == 1
        # the subscription ends; the founder presses Retry
        reset_for_retry(db, grant, actor="founder@example.test")
        db.commit()
        comp.mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")]
        assert not is_blocked(grants_of(db, person)[0])


def test_idempotency_key_prevents_a_duplicate_call_on_rerun(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        recompute_person(db, person, TODAY, False)
        for _ in range(3):
            decisions = recompute_person(db, person, TODAY, False)
            assert [(d.action, d.outcome) for d in decisions] == [("noop", "skipped")]
        assert comp.call_count == 1
        [grant] = grants_of(db, person)
        assert len(calls_of(db, grant)) == 1
        # even if the grant row lost its state (a crash between the call and the commit), the sent row stands
        grant.state = "pending"
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.detail) for d in decisions] == [("noop", "already sent")]
        assert comp.call_count == 1 and grants_of(db, person)[0].state == "active"


def test_dry_run_makes_no_http_call_and_the_next_live_run_sends_it(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, dry_run=True)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "planned")]
        assert decisions[0].plan["url"] == f"{PP}/api/partner/grouphq/comp"
        assert mock.calls.call_count == 0
        [grant] = grants_of(db, person)
        assert grant.state == "pending" and grant.ends_at == datetime(2027, 10, 1)
        [call] = calls_of(db, grant)
        assert call.status == "pending" and call.attempts == 0 and call.response["dry_run"] is True
        assert call.response["plan"]["headers"]["Authorization"] == "Bearer [redacted]"
        assert "test-" not in json.dumps(call.response) and "test-" not in json.dumps(call.payload)
        assert actions(db, f"grant:{grant.id}") == [], "a dry run changes nothing worth auditing"
        # the environment flag makes any run a dry run, whatever the caller asked for
        config.CONNECTORS_DRY_RUN = True
        decisions = recompute_person(db, person, TODAY, dry_run=False)
        assert decisions[0].outcome == "planned" and mock.calls.call_count == 0
        config.CONNECTORS_DRY_RUN = False
        decisions = recompute_person(db, person, TODAY, dry_run=False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")]
        assert comp.call_count == 1
        [call] = calls_of(db, grant)
        assert call.status == "succeeded" and call.attempts == 1


def test_scout_tier_is_recorded_as_a_noop(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db, "test_scout", [{"product": "pp", "kind": "comp_tier", "value": "scout"}]), "full"))
    with respx.mock(assert_all_called=False) as mock:
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")]
        assert "free tier" in decisions[0].detail
        assert mock.calls.call_count == 0
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.remote_ref is None
        [call] = calls_of(db, grant)
        assert call.status == "succeeded" and "free tier" in call.response["noop"]
        decisions = recompute_person(db, person, date(2027, 11, 1), False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")] and mock.calls.call_count == 0
        assert grants_of(db, person)[0].state == "revoked"


def test_an_ended_mentee_loses_access_whatever_was_paid(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "full")
    pay(db, mentee)
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        recompute_person(db, person, TODAY, False)
        mentee.status = "ended"
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")] and revoke.call_count == 1
        [grant] = grants_of(db, person)
        assert grant.state == "revoked" and grant.ends_at == datetime(2026, 10, 20)
        db.refresh(mentee)
        assert mentee.status == "ended", "ended is a decision, never flipped by the rule"


def test_revoke_person_switches_everything_off_now_and_stays_off(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db, "test_two", [
        {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
        {"product": "pp", "kind": "flag", "value": "kpuk.submit"},
    ]), "full")
    pay(db, mentee)
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        mock.post("/api/partner/grouphq/flags").mock(return_value=httpx.Response(200, json={"ok": True, "account_id": 77}))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        flag_off = mock.post("/api/partner/grouphq/flags/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        recompute_person(db, person, TODAY, False)
        assert {g.state for g in grants_of(db, person)} == {"active"}
        # called the way the mentee screen calls it: (db, person, actor=email, reason=...)
        result = revoke_person(db, person, actor="founder@example.test", reason="Left the programme")
        assert result == {"mentees ended": 1, "revoked": 2}
        assert revoke.call_count == 1 and flag_off.call_count == 1
        assert body_of(revoke.calls.last) == {"email": "amy@example.test", "reason": "Left the programme"}
        assert {g.state for g in grants_of(db, person)} == {"revoked"}
        db.refresh(mentee)
        assert mentee.status == "ended"
        assert "mentee.end" in actions(db, f"mentee:{mentee.id}")
        # the nightly rule now agrees: nothing comes back
        decisions = recompute_person(db, person, TODAY, False)
        assert {d.action for d in decisions} == {"noop"} and mock.calls.call_count == 4, "two grants, two revokes, nothing more"
        assert revoke_person(db, person, actor="founder@example.test") == {}


def test_a_grant_no_programme_gives_any_more_is_revoked(db, live):
    person = make_person(db)
    programme = make_programme(db)
    pay(db, enrol(db, person, programme, "full"))
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        recompute_person(db, person, TODAY, False)
        programme.grant_template = [{"product": "pp", "kind": "comp_tier", "value": "scout"}]
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert sorted((d.value, d.action) for d in decisions) == [("pro_buyer", "revoke"), ("scout", "grant")]
        assert revoke.call_count == 1
        states = {g.value: g.state for g in grants_of(db, person)}
        assert states == {"pro_buyer": "revoked", "scout": "active"}


def test_two_programmes_giving_the_same_thing_keep_the_later_end(db, live):
    person = make_person(db)
    a = enrol(db, person, make_programme(db, "test_a"), "monthly", date(2026, 10, 1))
    pay(db, a, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    b = enrol(db, person, make_programme(db, "test_b"), "full", date(2026, 10, 1))
    pay(db, b)
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
    assert len(decisions) == 1 and comp.call_count == 1
    assert body_of(comp.calls.last)["until"] == "2027-10-01T00:00:00Z"
    [grant] = grants_of(db, person)
    assert grant.mentee_id == b.id and grant.source == "programme:test_b"


def test_a_future_start_date_waits(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full", start=date(2026, 11, 1)))
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("not_started", "skipped")] and comp.call_count == 0
        decisions = recompute_person(db, person, date(2026, 11, 1), False)
        assert decisions[0].action == "grant" and body_of(comp.calls.last)["until"] == "2027-11-01T00:00:00Z"


def test_recompute_all_covers_everyone_and_the_job_prints_a_summary(db, live, capsys):
    paid = make_person(db, "paid@example.test", "Paid Person")
    pay(db, enrol(db, paid, make_programme(db), "full"))
    unpaid = make_person(db, "unpaid@example.test", "Unpaid Person")
    enrol(db, unpaid, make_programme(db), "monthly")
    make_person(db, "nobody@example.test", "Not Enrolled")
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        summary = recompute_all(db, TODAY, dry_run=False)
        assert summary.people == 2 and not summary.dry_run
        # the unpaid monthly mentee gets a pending grant row and nothing is sent for it
        assert summary.actions == {"grant": 1, "noop": 1} and summary.outcomes == {"sent": 1, "skipped": 1}
        assert "1 granted" in summary.line() and "live" in summary.line()
        assert comp.call_count == 1
        assert "recompute.run" in actions(db, "job:recompute")

        out = io.StringIO()
        assert job.main(["--dry-run", "--today", "2027-10-05"], out=out) == 0
        text = out.getvalue()
        assert "DRY RUN" in text and "planned" in text and "revoke" in text, text
        assert comp.call_count == 1, "the dry-run job sent nothing"
        with session() as fresh:
            grant = fresh.query(EntitlementGrant).filter(EntitlementGrant.person_id == paid.id).one()
            assert grant.state == "active"
            pending = [c for c in fresh.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id) if c.status == "pending"]
            assert len(pending) == 1 and pending[0].action == "revoke"
    with pytest.raises(SystemExit):
        job.main(["--dry-run", "--today", "yesterday"])
    with pytest.raises(SystemExit):
        job.main(["--today", "2026-05-15"]), "a live run as of another day is refused"


# ---- the grants screens -----------------------------------------------------------------------------
def _seed_grants(db) -> tuple[Person, EntitlementGrant, EntitlementGrant]:
    """One active grant (sent in a live run) and one blocked grant, for the list and detail pages."""
    person = make_person(db, "page@example.test", "Page Person")
    pay(db, enrol(db, person, make_programme(db, "test_two", [
        {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
        {"product": "ordris", "kind": "org_plan", "value": "starter"},
    ]), "full"))
    with respx.mock() as mock:
        mock.post(f"{PP}/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        mock.post("https://ordris.example.test/api/partner/grouphq/entitlements").mock(
            return_value=httpx.Response(409, json={"ok": False, "reason": "paid_subscription"}))
        recompute_person(db, person, TODAY, False)
    active, blocked = grants_of(db, person)
    assert active.state == "active" and is_blocked(blocked)
    return person, active, blocked


def test_grants_page_needs_a_login_then_lists_and_filters(client, admin_client, db, live):
    assert client.get("/grants").status_code == 303
    person, active, blocked = _seed_grants(db)
    page = admin_client.get("/grants")
    assert page.status_code == 200
    assert "Run nightly now" in page.text and 'name="dry_run"' in page.text and "checked" in page.text
    assert "page@example.test" in page.text and "pro_buyer" in page.text and "starter" in page.text
    assert "status-blocked" in page.text and "paid subscription" in page.text
    assert "1 blocked" in page.text and "1 active" in page.text
    only_blocked = admin_client.get("/grants", params={"state": "blocked"})
    assert "1 grant matching" in only_blocked.text and "starter" in only_blocked.text and "pro_buyer" not in only_blocked.text
    only_failed = admin_client.get("/grants", params={"state": "failed"})
    assert "0 grants matching" in only_failed.text, "blocked is not shown under failed"
    by_product = admin_client.get("/grants", params={"product": "pp"})
    assert "pro_buyer" in by_product.text and "starter" not in by_product.text
    searched = admin_client.get("/grants", params={"q": "PAGE@"})
    assert "2 grants matching" in searched.text
    assert "0 grants matching" in admin_client.get("/grants", params={"q": "100%"}).text
    assert admin_client.get("/grants", params={"page": "oops", "state": "nope"}).status_code == 200


def test_run_now_dry_run_shows_the_plan_and_sends_nothing(admin_client, db):
    person = make_person(db, "run@example.test", "Run Person")
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.post("/grants/recompute", data={"dry_run": "1", "today": "2026-10-20"})
        assert page.status_code == 200
        assert mock.calls.call_count == 0
    assert "Dry run for everyone" in page.text and "nothing was sent" in page.text
    assert "status-planned" in page.text and f"{PP}/api/partner/grouphq/comp" in page.text
    assert "Bearer [redacted]" in page.text and "test-" not in page.text
    assert "run@example.test" in page.text
    assert admin_client.post("/grants/recompute", data={"today": "not-a-date"}).status_code == 400
    with session() as fresh:
        [call] = fresh.query(ConnectorCall).all()
        assert call.status == "pending" and call.attempts == 0


def test_run_now_live_sends_and_reports(admin_client, db, live):
    person = make_person(db, "live@example.test", "Live Person")
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        assert admin_client.post("/grants/recompute", data={"today": "2026-05-15"}).status_code == 400, "no live run as of another day"
        assert comp.call_count == 0
        page = admin_client.post("/grants/recompute", data={"today": "2026-10-20"})
    assert page.status_code == 200 and comp.call_count == 1
    assert "Recompute for everyone" in page.text and "1 call sent" in page.text and "status-sent" in page.text
    with session() as fresh:
        grant = fresh.query(EntitlementGrant).one()
        assert grant.state == "active"
        row = fresh.query(AuditLog).filter(AuditLog.entity == f"grant:{grant.id}").one()
        assert row.actor == "founder@example.test", "a run from the page is audited as the admin who pressed the button"


def test_grant_detail_calls_and_retry(admin_client, db, live):
    person, active, blocked = _seed_grants(db)
    page = admin_client.get(f"/grants/{blocked.id}")
    assert page.status_code == 200
    assert "status-blocked" in page.text and "paid_subscription" in page.text and "grant.block" in page.text
    assert f"grant:{blocked.id}:" in page.text and "Retry" in page.text
    assert admin_client.get("/grants/999999").status_code == 404
    assert admin_client.post(f"/grants/{active.id}/retry").status_code == 303, "an active grant cannot be retried"
    with respx.mock(assert_all_called=False) as mock:
        mock.post("https://ordris.example.test/api/partner/grouphq/entitlements").mock(
            return_value=httpx.Response(200, json={"ok": True, "org_id": "org_1"}))
        page = admin_client.post(f"/grants/{blocked.id}/retry")
        assert page.status_code == 200 and mock.calls.call_count == 1
    assert "status-sent" in page.text and "retry for page@example.test" in page.text
    with session() as fresh:
        grant = fresh.get(EntitlementGrant, blocked.id)
        assert grant.state == "active" and grant.last_error == "" and grant.remote_ref == f"grouphq:grant:{blocked.id}"
        assert fresh.get(Person, person.id).ordris_org_id == "org_1"
        assert "grant.retry" in actions(fresh, f"grant:{blocked.id}")
    detail = admin_client.get(f"/grants/{blocked.id}")
    assert "status-active" in detail.text and "org_1" in detail.text
    # recompute for one person from the detail page, as a dry run
    with respx.mock(assert_all_called=False) as mock:
        page = admin_client.post(f"/grants/{active.id}/recompute", data={"dry_run": "1", "today": "2027-11-01"})
        assert page.status_code == 200 and mock.calls.call_count == 0
    assert "Dry run for page@example.test" in page.text and "revoke" in page.text


def test_calls_page_lists_the_outbox(admin_client, db, live):
    person, active, blocked = _seed_grants(db)
    page = admin_client.get("/grants/calls")
    assert page.status_code == 200 and "2 calls" in page.text
    assert f"grant:{active.id}:2027-10-01T00:00:00" in page.text and "paid_subscription" in page.text
    failed = admin_client.get("/grants/calls", params={"status": "failed"})
    assert "1 call matching" in failed.text and "paid_subscription" in failed.text
    assert "1 call matching" in admin_client.get("/grants/calls", params={"product": "pp"}).text


# ---- the review guards -----------------------------------------------------------------------------------
def test_an_end_beyond_the_ceiling_is_never_sent(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 10, 1))
    pay(db, mentee, "150", period=(date(2026, 10, 1), date(2206, 10, 31)))      # a mis-typed year
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("noop", "skipped")] and comp.call_count == 0
        assert f"{MAX_GRANT_DAYS} day ceiling" in decisions[0].detail
        [grant] = grants_of(db, person)
        assert grant.state == "pending" and "ceiling" in grant.last_error and calls_of(db, grant) == []
    assert full_term_months() == 12


def test_a_live_run_as_of_the_past_sends_nothing(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 6, 1))
    pay(db, mentee, "150", paid=datetime(2026, 6, 1), period=(date(2026, 6, 1), date(2026, 6, 30)))     # lapsed since July
    with respx.mock(base_url=PP, assert_all_called=False) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, date(2026, 6, 15), False)       # back-dated to when they were paid up
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "failed")] and comp.call_count == 0
        assert decisions[0].detail.startswith("refused: end 2026-07-07 is not in the future")
        [grant] = grants_of(db, person)
        assert grant.state == "pending" and calls_of(db, grant) == [], "nothing was even written to the outbox"


def test_a_failed_extension_is_sent_on_the_next_run(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 10, 1))
    pay(db, mentee, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        recompute_person(db, person, TODAY, False)
        pay(db, mentee, "150", paid=datetime(2026, 10, 20), period=(date(2026, 11, 1), date(2026, 11, 30)))
        comp.mock(return_value=httpx.Response(502, json={"error": "bad_gateway"}))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("extend", "failed")]
        [grant] = grants_of(db, person)
        assert grant.state == "active" and grant.ends_at == datetime(2026, 11, 7), "the product still holds the old window"
        comp.mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("extend", "sent")]
        assert body_of(comp.calls.last)["until"] == "2026-12-07T00:00:00Z" and comp.call_count == 3
        [grant] = grants_of(db, person)
        assert grant.ends_at == datetime(2026, 12, 7) and grant.last_error == ""


def test_a_dry_run_extension_is_sent_by_the_next_live_run(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "monthly", date(2026, 10, 1))
    pay(db, mentee, "150", period=(date(2026, 10, 1), date(2026, 10, 31)))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        recompute_person(db, person, TODAY, False)
        pay(db, mentee, "150", paid=datetime(2026, 10, 20), period=(date(2026, 11, 1), date(2026, 11, 30)))
        decisions = recompute_person(db, person, TODAY, dry_run=True)
        assert [(d.action, d.outcome) for d in decisions] == [("extend", "planned")] and comp.call_count == 1
        [grant] = grants_of(db, person)
        assert grant.ends_at == datetime(2026, 11, 7)
        planned = [c for c in calls_of(db, grant) if c.status == "pending"]
        assert len(planned) == 1 and planned[0].attempts == 0
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("extend", "sent")] and comp.call_count == 2
        [row] = [c for c in calls_of(db, grant) if c.id == planned[0].id]
        assert row.status == "succeeded" and row.attempts == 1
        assert grants_of(db, person)[0].ends_at == datetime(2026, 12, 7)


def test_a_regrant_after_a_revoke_is_sent_under_its_own_key(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "full")
    pay(db, mentee)
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        recompute_person(db, person, TODAY, False)
        revoke_person(db, person, actor="founder@example.test", reason="Pressed by mistake")
        [grant] = grants_of(db, person)
        assert grant.state == "revoked"
        mentee.status = "active"        # the founder undoes the mistake
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("grant", "sent")] and comp.call_count == 2
        [grant] = grants_of(db, person)
        assert grant.state == "active"
        keys = [c.idempotency_key for c in calls_of(db, grant)]
        revoke_row = [c for c in calls_of(db, grant) if c.action == "revoke"][0]
        assert keys[-1] == f"grant:{grant.id}:2027-10-01T00:00:00:after:{revoke_row.id}"
        assert keys[0] == f"grant:{grant.id}:2027-10-01T00:00:00"
        # and a re-run is still a noop
        assert [(d.action, d.detail) for d in recompute_person(db, person, TODAY, False)] == [("noop", "nothing to change")]
        assert comp.call_count == 2


def test_a_tier_change_revokes_the_old_comp_before_granting_the_new_one(db, live):
    person = make_person(db)
    programme = make_programme(db)
    pay(db, enrol(db, person, programme, "full"))
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        recompute_person(db, person, TODAY, False)
        programme.grant_template = [{"product": "pp", "kind": "comp_tier", "value": "apex"}]
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.value, d.action, d.outcome) for d in decisions] == [("pro_buyer", "revoke", "sent"), ("apex", "grant", "sent")]
        wire = [(c.request.url.path.rsplit("/", 1)[-1], body_of(c).get("tier")) for c in mock.calls]
        assert wire == [("comp", "pro_buyer"), ("revoke", None), ("comp", "pro_investor")], "the revoke goes out before the new comp"
        states = {g.value: g.state for g in grants_of(db, person)}
        assert states == {"pro_buyer": "revoked", "apex": "active"}
        assert revoke.call_count == 1 and comp.call_count == 2


def test_two_programmes_giving_pp_tiers_keep_the_higher_one(db, live):
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db, "test_hunter", [{"product": "pp", "kind": "comp_tier", "value": "pro_buyer"}]), "full"))
    pay(db, enrol(db, person, make_programme(db, "test_apex", [{"product": "pp", "kind": "comp_tier", "value": "pro_investor", "months": 3}]), "full"))
    targets = ent.targets_for(db, person, TODAY)
    assert list(targets) == [("pp", "comp_tier", "pro_investor")], "one comp per account: the higher tier wins"
    assert targets[("pp", "comp_tier", "pro_investor")].ends == date(2027, 1, 1)
    with respx.mock(base_url=PP) as mock:
        comp = mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        decisions = recompute_person(db, person, TODAY, False)
    assert [(d.value, d.action) for d in decisions] == [("pro_investor", "grant")] and comp.call_count == 1
    assert [g.value for g in grants_of(db, person) if g.state == "active"] == ["pro_investor"]


def test_a_product_kept_in_dry_run_is_only_planned_while_pp_goes_live(db, live, monkeypatch):
    monkeypatch.setattr(config, "CONNECTORS_DRY_RUN_PRODUCTS", frozenset({"hnh"}))
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db, "test_mix", [
        {"product": "pp", "kind": "comp_tier", "value": "pro_buyer"},
        {"product": "hnh", "kind": "partner_seat", "value": "area"},
    ]), "full"))
    with respx.mock(assert_all_called=False) as mock:
        comp = mock.post(f"{PP}/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        hnh = mock.post("https://hnh.example.test/api/partner/partners").mock(return_value=httpx.Response(201, json={"id": 1}))
        decisions = recompute_person(db, person, TODAY, False)
    assert {(d.product, d.outcome) for d in decisions} == {("pp", "sent"), ("hnh", "planned")}
    assert comp.call_count == 1 and hnh.call_count == 0
    pp_grant, hnh_grant = sorted(grants_of(db, person), key=lambda g: g.product != "pp")
    assert pp_grant.state == "active" and hnh_grant.state == "pending"
    [row] = calls_of(db, hnh_grant)
    assert row.status == "pending" and row.attempts == 0 and row.response["dry_run"] is True


def test_an_unconfigured_product_does_not_burn_an_attempt(db, live, monkeypatch):
    monkeypatch.setattr(config, "ORDRIS_PARTNER_TOKEN", "")
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db, "test_ordris", [{"product": "ordris", "kind": "org_plan", "value": "starter"}]), "full"))
    with respx.mock(assert_all_called=False) as mock:
        for _ in range(MAX_ATTEMPTS + 1):
            decisions = recompute_person(db, person, TODAY, False)
            assert [(d.action, d.outcome) for d in decisions] == [("grant", "skipped")]
            assert decisions[0].detail.startswith("not configured")
        assert mock.calls.call_count == 0
    [grant] = grants_of(db, person)
    [row] = calls_of(db, grant)
    assert grant.state == "pending" and row.attempts == 0 and row.status == "pending"
    summary = recompute_all(db, TODAY, False)
    assert summary.unconfigured == 1 and "1 not configured" in summary.line() and summary.failed == 0


def test_recompute_all_steps_aside_while_another_run_holds_the_lock(db, live):
    from sqlalchemy import text
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    other = db.get_bind().connect()
    try:
        other.execute(text("SELECT pg_advisory_lock(:s, 0)"), {"s": LOCK_RUN})
        with respx.mock(assert_all_called=False) as mock:
            summary = recompute_all(db, TODAY, False)
        assert summary.note == RUN_IN_PROGRESS and summary.decisions == [] and mock.calls.call_count == 0
        assert RUN_IN_PROGRESS in summary.line()
        out = io.StringIO()
        assert job.main(["--dry-run"], out=out) == 1, "the cron shows the skipped run as a failure"
    finally:
        other.execute(text("SELECT pg_advisory_unlock(:s, 0)"), {"s": LOCK_RUN})
        other.close()
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        assert recompute_all(db, TODAY, False).actions == {"grant": 1}


def test_a_start_moved_into_the_future_switches_an_active_grant_off(db, live):
    person = make_person(db)
    mentee = enrol(db, person, make_programme(db), "full", date(2026, 10, 1))
    pay(db, mentee)
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(200, json=COMP_OK))
        revoke = mock.post("/api/partner/grouphq/revoke").mock(return_value=httpx.Response(200, json={"ok": True}))
        recompute_person(db, person, TODAY, False)
        mentee.start_date = date(2026, 12, 1)
        db.commit()
        decisions = recompute_person(db, person, TODAY, False)
        assert [(d.action, d.outcome) for d in decisions] == [("revoke", "sent")] and revoke.call_count == 1
        [grant] = grants_of(db, person)
        assert grant.state == "revoked" and grant.ends_at == datetime(2026, 10, 20)
        assert [(d.action, d.outcome) for d in recompute_person(db, person, TODAY, False)] == [("not_started", "skipped")]


def test_a_full_payer_needs_cleared_payments_that_reach_the_price(db):
    person = make_person(db)
    programme = make_programme(db)
    programme.price = Decimal("4995.00")
    db.commit()
    mentee = enrol(db, person, programme, "full", date(2026, 10, 1))
    deposit = pay(db, mentee, "0.01")
    assert compute_access_end(mentee, [deposit], TODAY) is None, "a penny does not cover the programme"
    part = pay(db, mentee, "4000")
    assert compute_access_end(mentee, [deposit, part], TODAY) is None
    rest = pay(db, mentee, "994.99")
    assert compute_access_end(mentee, [deposit, part, rest], TODAY) == date(2027, 10, 1)
    programme.price = None
    db.commit()
    db.refresh(mentee)
    assert compute_access_end(mentee, [deposit], TODAY) == date(2027, 10, 1), "with no price any cleared payment counts"


# ---- a product's credential never reaches the database -------------------------------------------------
HNH = "https://hnh.example.test"
HNH_CREATED = {"ok": True, "created": True, "partner_id": 9, "user_id": 31, "one_time_password": "Tq7xK2mN9pLw",
               "area_slug": None, "area_created": False, "note": "",
               "partner": {"id": 9, "name": "Amy Archer", "email": "amy@example.test", "tier": "area", "status": "active",
                           "franchise": {"fee_waived": True, "reason": "Group HQ: test"}, "areas": [], "user_id": 31,
                           "login": {"user_id": 31, "email": "amy@example.test", "is_active": True,
                                     "must_change_password": True, "last_login": None},
                           "created_at": "2026-10-20T17:40:00Z"}}


def test_hnh_one_time_password_is_shown_once_and_never_stored(admin_client, db, live):
    """HNH answers a grant with the login's temporary password. It is shown on the run page to the person who
    pressed the button and nowhere else: not in connector_calls.response, not in an audit row, not on the grant
    page and not in the job's output."""
    person = make_person(db, "amy@example.test", "Amy Archer")
    pay(db, enrol(db, person, make_programme(db, "test_hnh", [{"product": "hnh", "kind": "partner_seat", "value": "area"}]), "full"))
    with respx.mock(base_url=HNH) as mock:
        post = mock.post("/api/partner/partners").mock(return_value=httpx.Response(201, json=HNH_CREATED))
        page = admin_client.post("/grants/recompute", data={"today": "2026-10-20"})
    assert page.status_code == 200 and post.call_count == 1
    assert "Tq7xK2mN9pLw" in page.text and "shown once" in page.text and "not stored" in page.text
    with session() as fresh:
        [grant] = fresh.query(EntitlementGrant).filter(EntitlementGrant.person_id == person.id).all()
        assert grant.state == "active" and grant.remote_ref == "9"
        [call] = fresh.query(ConnectorCall).filter(ConnectorCall.grant_id == grant.id).all()
        stored = json.dumps(call.response) + json.dumps(call.payload)
        assert call.status == "succeeded" and "Tq7xK2mN9pLw" not in stored and "one_time_password" not in stored
        assert call.response["data"]["partner"]["id"] == 9, "the rest of the answer is kept"
        trail = " ".join(json.dumps([row.before, row.after]) for row in fresh.query(AuditLog).all())
        assert "Tq7xK2mN9pLw" not in trail
        fresh_person = fresh.get(Person, person.id)
        assert fresh_person.hnh_partner_id == 9 and fresh_person.hnh_user_id == 31
    detail = admin_client.get(f"/grants/{grant.id}")
    assert detail.status_code == 200 and "Tq7xK2mN9pLw" not in detail.text
    # a second person seated by the command-line job: the password is not printed either
    other = make_person(db, "ben@example.test", "Ben Brook")
    pay(db, enrol(db, other, make_programme(db, "test_hnh", [{"product": "hnh", "kind": "partner_seat", "value": "area"}]), "full"))
    out = io.StringIO()
    with respx.mock(base_url=HNH) as mock:
        mock.post("/api/partner/partners").mock(return_value=httpx.Response(201, json={
            **HNH_CREATED, "partner_id": 10, "user_id": 32, "one_time_password": "Zz8pQ4rT1vBn",
            "partner": {**HNH_CREATED["partner"], "id": 10, "user_id": 32, "email": "ben@example.test"}}))
        assert job.main([], out=out) == 0
    assert "Zz8pQ4rT1vBn" not in out.getvalue() and "ben@example.test" in out.getvalue()
    with session() as fresh:
        rows = fresh.query(ConnectorCall).all()
        assert all("Zz8pQ4rT1vBn" not in json.dumps(row.response) for row in rows)


def test_a_credential_in_any_product_body_is_redacted_before_it_is_stored(db, live):
    """The second line: even a product that put a password or token straight into its body (or an error body)
    cannot get it into connector_calls.response."""
    person = make_person(db)
    pay(db, enrol(db, person, make_programme(db), "full"))
    with respx.mock(base_url=PP) as mock:
        mock.post("/api/partner/grouphq/comp").mock(return_value=httpx.Response(
            200, json={**COMP_OK, "temporary_password": "hunter2", "api_token": "abc", "nested": {"secret_key": "xyz"}}))
        recompute_person(db, person, TODAY, False)
    [grant] = grants_of(db, person)
    [call] = calls_of(db, grant)
    data = call.response["data"]
    assert data["temporary_password"] == "[redacted]" and data["api_token"] == "[redacted]"
    assert data["nested"]["secret_key"] == "[redacted]" and data["account_id"] == 77
    assert "hunter2" not in json.dumps(call.response) and "xyz" not in json.dumps(call.response)
