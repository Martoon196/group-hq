"""The Inbox's property category and Inbox conversations attached to property deals."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from app import config
from app.models import Acquisition, AcquisitionThread, AuditLog, User
from app.services import acquisitions as service
from app.services import inbox, inbox_claude, inbox_organisation, inbox_scanner, inbox_triage

THREAD = "seller-thread-id"


def msg(text, direction="incoming"):
    return {"body": text, "direction": direction, "id": "msg"}


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setattr(config, "GHQ_INBOX_ACCOUNTS", json.dumps([
        {"key": "kp-fb", "label": "Key Properties", "brand": "kp", "provider": "zernio", "channel": "facebook",
         "account_id": "kp-native-account", "profile_id": "kp-profile", "credential_env": "GHQ_INBOX_TEST", "can_reply": True}]))
    monkeypatch.setenv("GHQ_INBOX_TEST", "test-inbox-token")

    class Fake:
        def __init__(self, account):
            self.account = account

        def conversations(self):
            return [{"id": THREAD, "name": "Sample Landlord", "participant_id": "native-person",
                     "preview": "Selling my portfolio", "updated_at": "2026-11-06T12:00:00Z", "unread": 1}], False

        def messages(self, thread, cursor=None):
            return [{"id": "m1", "body": "I want to sell my six houses <b>quickly</b>", "direction": "incoming",
                     "name": "Sample Landlord", "occurred_at": "2026-11-06T12:00:00Z"},
                    {"id": "m2", "body": "Happy to look. What are the addresses?", "direction": "outgoing",
                     "name": "", "occurred_at": "2026-11-06T12:05:00Z"}], None

    monkeypatch.setattr(inbox, "client", Fake)
    monkeypatch.setattr("app.routers.inbox.client", Fake)


def ticket(db, thread=THREAD):
    account = next(a for a in inbox.accounts() if a.key == "kp-fb")
    return account, inbox.ticket(account, thread, db.scalars(select(User)).first().id, "Sample Landlord", "native-person")


@pytest.mark.parametrize("text,kind", [
    ("Hi Martin, I'm a landlord with 6 houses in Swansea and I'm looking to sell the whole portfolio. Interested?", "property"),
    ("I am selling my house, what would you offer?", "property"),
    ("We have a development site for sale with planning for 12 units", "property"),
    ("Do you want to buy my flat in Cardiff?", "property"),
    ("I am looking to buy an investment property in Durham", "business"),
    ("Are you selling the house on Durham Road? What's the asking price", "unclassified"),
    ("I am selling my course on property investing, interested?", "unclassified"),
    ("I want to sell more of my services to landlords", "unclassified"),
])
def test_local_triage_tells_sellers_from_buyers_and_pitches(text, kind):
    assert inbox_triage.analyse([msg(text)])["kind"] == kind


def test_a_property_lead_always_goes_to_the_owner_and_is_never_drafted_or_branded():
    local = inbox_triage.analyse([msg("I am retiring and need to sell my three rentals, are you buying?")], "kp")
    assert (local["kind"], local["brands"], local["needs_owner"], local["draft"]) == ("property", [], True, "")
    assert inbox_triage.analyse([msg("i am selling my house what would you offer", "outgoing")])["kind"] == "unclassified"


def test_claude_property_result_is_forced_to_owner_without_draft(monkeypatch):
    monkeypatch.setenv("GHQ_INBOX_ANTHROPIC_API_KEY", "fixture")
    sent = []

    def provider(url, **kw):
        sent.append(kw)
        data = {"kind": "property", "brands": ["kp"], "intent": "Landlord selling six houses", "reason": "Offers a portfolio",
                "needs_owner": False, "draft": "We will buy them"}
        return httpx.Response(200, request=httpx.Request("POST", url),
                              json={"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(data)}]})
    result = inbox_claude.analyse_with_claude([msg("I want to sell my six houses")], "kp", provider)
    assert (result["kind"], result["brands"], result["needs_owner"], result["draft"]) == ("property", [], True, "")
    assert "property" in sent[0]["json"]["output_config"]["format"]["schema"]["properties"]["kind"]["enum"]
    assert "property" in sent[0]["json"]["system"]


def test_property_is_a_conversation_type_and_filter():
    assert "property" in inbox_organisation.KINDS and "property" in inbox_scanner.FILTERS
    assert inbox_scanner.effective_kind({"kind": "property"}, {"kind": "unclassified"}) == "property"
    assert inbox_organisation.permits_projection({"kind": "property", "sync_brands": ["kp"]}, "kp")
    assert not inbox_organisation.permits_projection({"kind": "personal", "sync_brands": ["kp"]}, "kp")


def test_start_a_deal_from_a_conversation_attaches_it_and_shows_its_messages(admin_client, connected, db):
    account, value = ticket(db)
    conversation = admin_client.get("/inbox", params={"thread": value})
    assert conversation.status_code == 200 and "Start a property deal from this conversation" in conversation.text
    assert "Property offered to me" in conversation.text

    started = admin_client.post("/acquisitions/start", data={"thread_ticket": value, "seller_name": "Sample Landlord", "source": "Facebook"})
    assert started.status_code == 200 and 'name="thread_ticket"' in started.text and "will be attached" in started.text
    assert db.query(Acquisition).count() == 0, "opening the form saves nothing"

    added = admin_client.post("/acquisitions/deals", data={"title": "Sample portfolio", "seller_name": "Sample Landlord",
                                                           "source": "Facebook", "thread_ticket": value})
    deal = db.query(Acquisition).one()
    assert added.status_code == 303 and [(t.account_key, t.thread_id, t.name) for t in service.attached(db, deal.id)] == [
        ("kp-fb", THREAD, "Sample Landlord")]
    user = db.scalars(select(User)).first()
    assert inbox_organisation.choices(db, user.id, account, THREAD)["kind"] == "property"

    page = admin_client.get(f"/acquisitions/deals/{deal.id}")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "I want to sell my six houses &lt;b&gt;quickly&lt;/b&gt;" in page.text and "<b>quickly</b>" not in page.text
    assert "Happy to look. What are the addresses?" in page.text and "Reply in the Inbox" in page.text
    again = admin_client.get("/inbox", params={"thread": value})
    assert "Open property deal: Sample portfolio" in again.text
    assert "acquisition.attach" in [a.action for a in db.query(AuditLog)]


def test_link_to_an_existing_deal_once_then_remove(admin_client, connected, db):
    _, value = ticket(db)
    deal = Acquisition(title="Existing deal", stage="lead", lead_at=service.laps.utcnow(), checks={})
    db.add(deal)
    db.commit()
    assert "Existing deal" in admin_client.get("/inbox", params={"thread": value}).text
    linked = admin_client.post("/acquisitions/link", data={"thread_ticket": value, "deal_id": deal.id})
    assert linked.status_code == 303 and "ok=" in linked.headers["location"] and linked.headers["location"].startswith("/inbox?")
    assert "err=" in admin_client.post("/acquisitions/link", data={"thread_ticket": value, "deal_id": deal.id}).headers["location"]
    row = db.query(AcquisitionThread).one()
    assert admin_client.post("/acquisitions/link", data={"thread_ticket": value, "deal_id": 999999}).status_code == 404

    removed = admin_client.post(f"/acquisitions/deals/{deal.id}/unlink", data={"account_key": row.account_key, "thread_sha256": row.thread_sha256})
    assert "ok=" in removed.headers["location"] and db.query(AcquisitionThread).count() == 0
    assert "err=" in admin_client.post(f"/acquisitions/deals/{deal.id}/unlink", data={"account_key": "kp-fb", "thread_sha256": "0" * 64}).headers["location"]
    page = admin_client.get(f"/acquisitions/deals/{deal.id}")
    assert "No conversation is attached yet" in page.text and "I want to sell my six houses" not in page.text


def test_stale_or_tampered_tickets_are_refused_and_a_gone_account_shows_no_messages(admin_client, connected, db, monkeypatch):
    _, value = ticket(db)
    assert admin_client.post("/acquisitions/start", data={"thread_ticket": "tampered"}).status_code == 409
    assert admin_client.post("/acquisitions/deals", data={"title": "A", "thread_ticket": "tampered"}).status_code == 409
    assert admin_client.post("/acquisitions/link", data={"thread_ticket": "tampered", "deal_id": 1}).status_code == 409
    assert db.query(Acquisition).count() == 0
    admin_client.post("/acquisitions/deals", data={"title": "Sample portfolio", "thread_ticket": value})
    deal = db.query(Acquisition).one()
    monkeypatch.setattr(config, "GHQ_INBOX_ACCOUNTS", "[]")
    page = admin_client.get(f"/acquisitions/deals/{deal.id}")
    assert page.status_code == 200 and "not available to you here" in page.text and "I want to sell my six houses" not in page.text


def test_deleting_a_deal_row_removes_its_conversation_links(admin_client, connected, db):
    _, value = ticket(db)
    admin_client.post("/acquisitions/deals", data={"title": "Sample portfolio", "thread_ticket": value})
    db.query(Acquisition).delete()
    db.commit()
    assert db.query(AcquisitionThread).count() == 0
