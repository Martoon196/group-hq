"""Today: the home screen lists what is waiting for the founder, ordered and dated in UK time, and the health of
what feeds it in plain English. Read only."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.models import (Acquisition, AuditLog, CampaignAction, CampaignLead, CampaignPlan, EntitlementGrant, FranchiseOpportunity,
                        LapsDeal, Mentee, MetricSource, Payment, Person, Programme, Task, utcnow)
from app.services import today as today_service
from app.services.inbox_providers import Account

NOW = utcnow().replace(hour=13, minute=0, second=0, microsecond=0)    # naive UTC; 13:00 UTC is the same UK date
TODAY = NOW.date()           # the home route reads the real clock, so the seed is relative to it


def _person(db, key, name):
    person = Person(person_key=key, name=name)
    db.add(person)
    db.flush()
    return person


def _programme(db):
    row = db.query(Programme).first()
    if row is None:
        row = Programme(code="mentorship", name="Mentorship", grant_template=[])
        db.add(row)
        db.flush()
    return row


def seed(db):
    db.add_all([
        LapsDeal(name="Overdue Olive", brand="mhp", stage="appointment", lead_at=NOW, next_step="Send the proposal",
                 next_step_on=TODAY - timedelta(days=3)),
        LapsDeal(name="Today Tom", brand="pp", stage="lead", lead_at=NOW, next_step="Call back", next_step_on=TODAY),
        LapsDeal(name="Later Lily", brand="kp", stage="lead", lead_at=NOW, next_step="Call", next_step_on=TODAY + timedelta(days=2)),
        LapsDeal(name="Won Walter", brand="mhp", stage="sale", lead_at=NOW, next_step="Thank", next_step_on=TODAY - timedelta(days=1)),
        LapsDeal(name="Fresh Fiona", brand="piuk", stage="lead", lead_at=NOW, source="Facebook"),
        Acquisition(title="Six-unit HMO, SA1", stage="offer", next_step="Chase the agent", next_step_on=TODAY - timedelta(days=1)),
    ])
    lead = CampaignLead(brand="pp", source_system="relaunch72", external_id="c-1", display_name="Callum Crm", lifecycle="lead")
    db.add(lead)
    db.flush()
    plan = CampaignPlan(lead_id=lead.id, blueprint_key="pp", blueprint_version=1)
    db.add(plan)
    db.flush()
    db.add_all([CampaignAction(plan_id=plan.id, step_key="d1", channel="call", title="First call", guidance="g", due_at=NOW - timedelta(hours=2)),
                CampaignAction(plan_id=plan.id, step_key="d9", channel="email", title="Later email", guidance="g", due_at=NOW + timedelta(days=5))])
    prog = _programme(db)
    monthly = Mentee(person_id=_person(db, "mo@example.test", "Monthly Mo").id, programme_id=prog.id, payment_mode="monthly",
                     start_date=date(2026, 9, 1))
    paid_ahead = Mentee(person_id=_person(db, "ahead@example.test", "Ahead Ann").id, programme_id=prog.id, payment_mode="monthly",
                        start_date=date(2026, 9, 1))
    db.add_all([monthly, paid_ahead])
    db.flush()
    db.add_all([
        Payment(mentee_id=monthly.id, amount=Decimal("250"), paid_at=NOW - timedelta(days=29), cleared_at=NOW - timedelta(days=28),
                period_start=TODAY - timedelta(days=28), period_end=TODAY + timedelta(days=2)),
        Payment(mentee_id=paid_ahead.id, amount=Decimal("250"), paid_at=NOW - timedelta(days=11), cleared_at=NOW - timedelta(days=11),
                period_start=TODAY - timedelta(days=11), period_end=TODAY + timedelta(days=49)),
        Payment(mentee_id=paid_ahead.id, amount=Decimal("99.50"), paid_at=NOW - timedelta(days=2)),
    ])
    franchise = FranchiseOpportunity(person_id=_person(db, "fran@example.test", "Franchise Fran").id, stage="enquiry")
    db.add(franchise)
    db.flush()
    db.add(Task(opportunity_id=franchise.id, due_on=TODAY, title="Send the prospectus"))
    db.commit()


def test_build_lists_what_is_due_in_order_and_leaves_the_rest(db):
    seed(db)
    ctx = today_service.build(db, True, [], now=NOW)
    groups = {g.key: g for g in ctx["waiting"]}
    sales = [i.title for i in groups["sales"].items]
    assert sales == ["Overdue Olive", "Today Tom"], "due today or earlier, open stages only, oldest first"
    assert groups["sales"].items[0].due_words(TODAY) == "3 days overdue" and groups["sales"].items[1].due_words(TODAY) == "Due today"
    assert [i.title for i in groups["leads"].items] == ["Fresh Fiona"], "only leads with no next step"
    assert [i.title for i in groups["property"].items] == ["Six-unit HMO, SA1"]
    assert [i.title for i in groups["crm"].items] == ["Callum Crm"] and groups["crm"].items[0].detail.startswith("Call · ")
    money = [(i.title, i.detail) for i in groups["mentees"].items]
    assert ("Monthly Mo", f"Paid up to {(TODAY + timedelta(days=2)).strftime('%-d %b')}; next payment due") in money
    assert any(t == "Ahead Ann" and "not cleared yet" in d for t, d in money)
    assert not any(t == "Ahead Ann" and "next payment" in d for t, d in money), "paid well ahead is not due"
    assert [i.title for i in groups["franchise"].items] == ["Franchise Fran"]
    assert "replies" not in groups and "regions" not in groups
    assert ctx["total"] == sum(g.total for g in ctx["waiting"])
    assert db.query(AuditLog).count() == 0, "Today changes nothing"


def test_replies_and_disconnected_inbox_accounts(db):
    acct = Account(key="kp-fb", brand="kp", channel="facebook", label="Key Properties Facebook", provider="zernio", account_id="1")
    threads = [{"name": "Sam Seller", "preview": "  Can we talk about my four flats?  ", "account": acct, "ticket": "t1", "needs_owner": True},
               {"name": "Quiet Q", "preview": "thanks", "account": acct, "ticket": "t2", "needs_owner": False}]
    results = [{"account": acct, "threads": threads, "has_more": False, "error": None},
               {"account": Account(key="pp-ig", brand="pp", channel="instagram", label="Property Predator Instagram",
                                   provider="zernio", account_id="2"), "threads": [], "has_more": False, "error": "Reconnect this account."}]
    panel = today_service.build_inbox(threads, results, None)
    replies = panel["group"]
    assert [i.title for i in replies.items] == ["Sam Seller"] and replies.total == 1
    assert replies.items[0].href == "/inbox?account=kp-fb&thread=t1"
    assert replies.items[0].detail == "Key Properties Facebook · Can we talk about my four flats?"
    assert [(h.level, h.title) for h in panel["problems"]] == [("bad", "Property Predator Instagram is not connected")]
    failed = today_service.build_inbox([], [], "The inbox could not be read just now.")
    assert failed["group"].total == 0 and failed["problems"][0].title == "The inbox could not be read"
    titles = [h.title for h in today_service.health(db, False, now=NOW)]
    assert "Inbox sorting is paused" in titles


def test_health_reads_failures_in_plain_english_and_says_when_all_is_well(db, monkeypatch):
    monkeypatch.setattr(today_service.config, "R72_GROUP_HQ_INBOX_SECRET", "x" * 40)
    person = _person(db, "g@example.test", "Grant Gail")
    prog = _programme(db)
    mentee = Mentee(person_id=person.id, programme_id=prog.id, payment_mode="full", start_date=TODAY)
    db.add(mentee)
    db.flush()
    db.add(EntitlementGrant(person_id=person.id, mentee_id=mentee.id, product="pp", kind="comp_tier", value="pro_buyer", state="failed"))
    db.add(MetricSource(source="ga4", status="failing", last_error="401 from Google"))
    db.commit()
    health = today_service.health(db, True, now=NOW)
    words = {h.title: h for h in health}
    assert "1 access grant failed" in words and words["1 access grant failed"].href == "/grants?state=failed"
    assert "Growth figures from Google Analytics 4 are failing" in words and "401 from Google" in words["Growth figures from Google Analytics 4 are failing"].why
    assert "The LAPS board has not pulled leads yet" in words

    db.query(EntitlementGrant).delete()
    db.query(MetricSource).delete()
    db.commit()
    from app.services import laps_sync
    from app.models import AppSetting
    db.add(AppSetting(key=laps_sync.SYNC_KEY, value={"at": (NOW - timedelta(minutes=5)).isoformat(), "brands": {}, "problem": ""}))
    db.commit()
    assert [(h.level, h.title) for h in today_service.health(db, True, now=NOW)] == [("ok", "Leads, access and reports are all running")]


def test_a_failed_call_behind_a_failed_grant_is_one_problem_not_two(db, monkeypatch):
    from app.models import ConnectorCall
    person = _person(db, "c@example.test", "Call Cal")
    grant = EntitlementGrant(person_id=person.id, product="pp", kind="comp_tier", value="pro_buyer", state="failed")
    db.add(grant)
    db.flush()
    db.add(ConnectorCall(grant_id=grant.id, product="pp", action="grant", idempotency_key="k1", status="failed"))
    db.commit()
    titles = [h.title for h in today_service.health(db, True, now=NOW)]
    assert "1 access grant failed" in titles and not any("product call" in t for t in titles)
    db.add(ConnectorCall(grant_id=None, product="kp", action="lookup", idempotency_key="k2", status="failed"))
    db.commit()
    assert "1 product call failed" in [h.title for h in today_service.health(db, True, now=NOW)]


def test_home_opens_without_reading_the_inbox_and_the_old_dashboard_is_overview(admin_client, db, monkeypatch):
    seed(db)
    def never(*a, **k):
        raise AssertionError("the home page must not wait on a live inbox read")
    monkeypatch.setattr("app.routers.inbox.conversation_list", never)
    page = admin_client.get("/")
    assert page.status_code == 200 and "<h1>Today</h1>" in page.text
    assert 'data-fragment-src="/today/replies"' in page.text and "Checking your inboxes" in page.text
    assert "Overdue Olive" in page.text and "Send the proposal" in page.text and "3 days overdue" in page.text
    assert "Later Lily" not in page.text and "Won Walter" not in page.text
    assert 'href="/laps/deals/' in page.text and 'href="/overview"' in page.text
    assert "/portal/approvals" in page.text, "content approvals are one tap away"
    assert 'for="nav-toggle"' in page.text and "Admin and set-up" in page.text
    overview = admin_client.get("/overview")
    assert overview.status_code == 200 and "<h1>Overview</h1>" in overview.text and "Latest activity" in overview.text
    assert '<details class="nav-more" open>' in overview.text, "the folded menu is open on a screen inside it"


def test_replies_panel_loads_after_the_page_and_survives_a_broken_inbox(admin_client, db, monkeypatch):
    acct = Account(key="kp-fb", brand="kp", channel="facebook", label="Key Properties Facebook", provider="zernio", account_id="1")
    threads = [{"name": "Sam <b>Seller</b>", "preview": "Four flats", "account": acct, "ticket": "t1", "needs_owner": True}]
    monkeypatch.setattr("app.routers.inbox.conversation_list",
                        lambda *a, **k: ([acct], [{"account": acct, "threads": threads, "error": None}], threads, None))
    panel = admin_client.get("/today/replies")
    assert panel.status_code == 200 and panel.headers["cache-control"] == "no-store"
    assert "Sam &lt;b&gt;Seller&lt;/b&gt;" in panel.text and 'data-count="1"' in panel.text and "<html" not in panel.text
    monkeypatch.setattr("app.routers.inbox.conversation_list", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    broken = admin_client.get("/today/replies")
    assert broken.status_code == 200 and "The inbox could not be read" in broken.text and 'data-count="0"' in broken.text


def _leadrelay_answer(**over):
    base = {"brand": "pp", "switchedOn": True, "problem": None, "lastSucceededAt": "2026-11-12T22:00:00.000Z",
            "lastAnsweredAt": "2026-11-12T22:00:00.000Z", "today": {}, "countingSince": "2026-11-12T00:00:00.000Z",
            "waitingAfterRepeatedFailures": 0, "needsTheOwner": [], "sentences": [],
            "summary": "LeadRelay is connected. 3 enquiries and 2 call results copied to the CRM today.",
            "workspaceId": "d975ff66-336a-46c2-b36b-d7095a11af97"}
    return {**base, **over}


def test_leadrelay_line_says_what_relaunch72_says_and_never_hides_a_failed_check(db, monkeypatch):
    import json
    from types import SimpleNamespace

    import httpx

    from app import config
    from app.services import inbox_crm
    user = SimpleNamespace(email="founder@example.test")
    monkeypatch.setattr(config, "R72_GROUP_HQ_INBOX_SECRET", "")
    assert inbox_crm.leadrelay_status(user) is None, "no CRM link at all: nothing to say, and nothing is called"
    monkeypatch.setattr(config, "R72_GROUP_HQ_INBOX_SECRET", "fixture-credential-" * 3)
    asked = []

    def relaunch72(answer, status=200):
        def handler(request):
            asked.append(json.loads(request.content))
            return httpx.Response(status, json=answer)
        return httpx.MockTransport(handler)

    ok = inbox_crm.leadrelay_status(user, transport=relaunch72(_leadrelay_answer()))
    assert ok == {"level": "ok", "title": "LeadRelay is connected",
                  "why": "LeadRelay is connected. 3 enquiries and 2 call results copied to the CRM today."}
    assert asked[-1]["action"] == "leadrelayFeedStatus" and asked[-1]["brand"] == "pp" and asked[-1]["actorEmail"] == user.email
    off = inbox_crm.leadrelay_status(user, transport=relaunch72(_leadrelay_answer(
        switchedOn=False, summary="LeadRelay is not connected yet, so its enquiries are not reaching the CRM.")))
    assert off["level"] == "warn" and off["title"] == "LeadRelay is not connected" and "not reaching the CRM" in off["why"]
    down = inbox_crm.leadrelay_status(user, transport=relaunch72(_leadrelay_answer(
        problem="unreachable", summary="LeadRelay did not answer (last heard 14:05 yesterday). Nothing was sent to anyone.")))
    assert down["level"] == "bad" and down["title"] == "LeadRelay needs a look"
    owner = inbox_crm.leadrelay_status(user, transport=relaunch72(_leadrelay_answer(
        needsTheOwner=[{"leadRelayLeadId": "x", "reason": "two_contacts", "at": "2026-11-12T21:00:00.000Z"}],
        summary="1 LeadRelay enquiry matches two CRM contacts and was not copied.")))
    assert owner["level"] == "warn" and owner["title"] == "LeadRelay needs a look"
    # An older Relaunch72, a refusal, or an answer for another business is reported as "could not be checked".
    for transport in (relaunch72({"error": "invalid_request"}, 400), relaunch72(_leadrelay_answer(brand="mhp")),
                      relaunch72(_leadrelay_answer(summary="")), relaunch72(_leadrelay_answer(switchedOn="yes"))):
        failed = inbox_crm.leadrelay_status(user, transport=transport)
        assert failed["level"] == "warn" and failed["title"] == "LeadRelay could not be checked"
        assert "Nothing was sent to anyone" in failed["why"]


def test_today_panel_shows_the_leadrelay_line_escaped_and_survives_its_failure(admin_client, db, monkeypatch):
    monkeypatch.setattr("app.routers.inbox.conversation_list", lambda *a, **k: ([], [], [], None))
    monkeypatch.setattr("app.services.inbox_crm.leadrelay_status", lambda user: {
        "level": "ok", "title": "LeadRelay is connected", "why": "2 enquiries copied <b>today</b>."})
    panel = admin_client.get("/today/replies")
    assert panel.status_code == 200 and "<strong>LeadRelay is connected</strong>" in panel.text
    assert "2 enquiries copied &lt;b&gt;today&lt;/b&gt;." in panel.text and 'class="today-health-ok"' in panel.text
    assert 'data-count="0"' in panel.text, "a status line is not something waiting for the founder"
    monkeypatch.setattr("app.services.inbox_crm.leadrelay_status", lambda user: None)
    assert "LeadRelay" not in admin_client.get("/today/replies").text
