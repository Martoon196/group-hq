from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import select

from app import config
from app.db import SessionLocal
from app.jobs import inbox_automations as worker
from app.models import InboxReply, User
from app.routers.inbox_automations import account_ticket
from app.services import inbox, inbox_automations as auto, inbox_organisation
from app.services.inbox_providers import InboxError


@pytest.fixture
def channel(db, monkeypatch):
    account_config = {"key": "dm-one", "label": "Business Page", "brand": "mhp", "channel": "facebook",
        "provider": "zernio", "account_id": "page-123", "profile_id": "profile-123",
        "credential_env": "GHQ_INBOX_TEST", "can_reply": True}
    monkeypatch.setattr(config, "GHQ_INBOX_ACCOUNTS", json.dumps([account_config]))
    monkeypatch.setenv("GHQ_INBOX_TEST", "fake")
    clock = [datetime(2026, 11, 1, 12, tzinfo=timezone.utc)]
    monkeypatch.setattr(auto, "now", lambda: clock[0])
    seen, posts, window = [], [], []
    class FakeChannel:
        def conversations(self):
            seen.append("list")
            return [{"id": "thread-one", "updated_at": clock[0].isoformat()}], False

        def messages(self, ident):
            seen.append(ident)
            return list(window), None

        def send(self, ident, text, request_id):
            posts.append((ident, text, request_id))
            return "fake-receipt"

    monkeypatch.setattr(inbox, "client", lambda _: FakeChannel())
    return db.scalar(select(User)), inbox.accounts(db)[0], clock, posts, window, seen


def match(messages, account_brand=None):
    return {"kind": "business", "needs_owner": False, "reason": ""}


def enable(db, user, account):
    auto.update(db, user, account, "add", keyword="viewing", reply="Which day works for you?")
    rule = auto.settings(db, user, account)["rules"][0]
    auto.update(db, user, account, "enable", rule_id=rule["id"])
    auto.update(db, user, account, "start")
    return rule["id"]


def message(when, direction="incoming", ident="inbound-1"):
    return {"id": ident, "direction": direction, "body": "Viewing!", "occurred_at": when.isoformat()}


def test_no_account_or_provider_work_until_an_enabled_account_has_a_fresh_incoming(db, channel, monkeypatch):
    user, account, clock, posts, window, seen = channel
    monkeypatch.setattr(inbox, "accounts", lambda *_: pytest.fail("Paused account polled"))
    worker.tick(lambda: False)
    auto.update(db, user, account, "add", keyword="viewing", reply="Book here.")
    worker.tick(lambda: False)
    rule = auto.settings(db, user, account)["rules"][0]
    assert not rule["enabled"]
    auto.update(db, user, account, "enable", rule_id=rule["id"])
    worker.tick(lambda: False)
    assert not posts and not seen


def test_two_workers_remain_once_per_inbound_and_per_original_channel(db, channel):
    user, account, clock, posts, _, _ = channel
    enable(db, user, account)
    old = message(clock[0])
    clock[0] += timedelta(minutes=1)
    fresh = message(clock[0])
    auto.process_thread(db, user, account, "thread-one", [old], match)
    auto.process_thread(db, user, account, "thread-one", [fresh, message(clock[0], "outgoing")], match)
    assert not posts
    user_id = user.id

    def poll(_):
        with SessionLocal() as session:
            auto.process_thread(session, session.get(User, user_id), account, "thread-one", [fresh], match)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(poll, range(2)))
    assert len(posts) == 1
    request_id = auto.receipt_id(account, "thread-one", fresh["id"])
    assert posts[0] == ("thread-one", "Which day works for you?", "ghq-" + request_id)
    assert db.get(InboxReply, request_id).state == "accepted"


def test_privacy_handoff_and_resume_cutoff(db, channel):
    user, account, clock, posts, _, _ = channel
    enable(db, user, account)
    clock[0] += timedelta(minutes=1)
    row = message(clock[0])
    for kind in ("personal", "pitch", "unclassified"):
        sample = {**row, "body": "Mentioned a viewing earlier."} if kind == "unclassified" else row
        auto.process_thread(db, user, account, "thread-one", [sample], lambda *a, **k: {"kind": kind, "needs_owner": False})
    assert not posts
    inbox_organisation.save(db, user, account, "private-thread", "personal", [], [])
    auto.process_thread(db, user, account, "private-thread", [row], lambda *a, **k: pytest.fail("Private chat analysed"))
    auto.process_thread(db, user, account, "thread-one", [row],
                        lambda *a, **k: {"kind": "business", "needs_owner": True, "reason": "Asked for a human"})
    held = auto.handoff_state(db, user.id, account, "thread-one")
    assert held["held"] and held["reason"] == "Asked for a human"
    auto.process_thread(db, user, account, "thread-one", [row], match)
    auto.set_handoff(db, user, account, "thread-one", False)
    auto.process_thread(db, user, account, "thread-one", [row], match)
    assert not posts
    clock[0] += timedelta(minutes=1)
    auto.process_thread(db, user, account, "thread-one", [message(clock[0], ident="new")], match)
    assert len(posts) == 1


def test_explicit_controls_signed_scope_and_edit_requires_reactivation(client, admin_client, db, channel):
    from dataclasses import replace
    user, account, clock, posts, _, _ = channel
    assert client.get("/inbox/automations").status_code == 303
    page = admin_client.get("/inbox/automations")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Start enabled replies on this account" in page.text
    payload = {"action": "add", "keyword": "VIEWING", "reply": "<script>exact phrase</script>", "account_ticket": account_ticket(user, account)}
    for token in ("forged", account_ticket(user, replace(account, account_id="different"))):
        assert admin_client.post("/inbox/automations/rules", data={**payload, "account_ticket": token}).status_code == 409
    assert admin_client.post("/inbox/automations/rules", data=payload, headers={"Origin": "https://foreign.invalid"}).status_code == 403
    assert admin_client.post("/inbox/automations/rules", data=payload).status_code == 303
    assert "&lt;script&gt;exact phrase&lt;/script&gt;" in admin_client.get("/inbox/automations").text
    rule_id = auto.settings(db, user, account)["rules"][0]["id"]
    for action in ("enable", "start", "edit"):
        assert admin_client.post("/inbox/automations/rules", data={**payload, "action": action, "rule_id": rule_id}).status_code == 303
    assert not auto.settings(db, user, account)["enabled"]
    ticket = inbox.ticket(account, "thread-one", user.id)
    assert admin_client.post("/inbox/automations/handoff", data={"action": "takeover", "thread_ticket": "bad"}).status_code == 409
    assert admin_client.post("/inbox/automations/handoff", data={"action": "takeover", "thread_ticket": ticket}).status_code == 303
    assert auto.handoff_state(db, user.id, account, "thread-one")["held"]
    assert not auto.handoff_state(db, user.id + 1, account, "thread-one")["held"]
    assert not auto.handoff_state(db, user.id, replace(account, token="rotated"), "thread-one")["held"]
    assert not auto.handoff_state(db, user.id, account, "different-thread")["held"]
    assert not posts


def test_poll_sends_once_and_pausing_stops_scheduled_work(db, channel):
    user, account, clock, posts, window, seen = channel
    enable(db, user, account)
    clock[0] += timedelta(minutes=1)
    window.append(message(clock[0]))
    auto.poll_account(db, user, account, match)
    auto.poll_account(db, user, account, match)
    assert len(posts) == 1 and seen == ["list", "thread-one"]
    clock[0] += timedelta(minutes=2)
    auto.update(db, user, account, "pause")
    auto.poll_account(db, user, account, match)
    assert seen == ["list", "thread-one"]


def test_unknown_delivery_stays_with_owner_across_worker_restart(db, channel, monkeypatch):
    user, account, clock, posts, _, _ = channel
    enable(db, user, account)
    clock[0] += timedelta(minutes=1)

    class Timeout:
        def send(self, thread, body, request):
            posts.append(request)
            raise InboxError("outcome_unknown")
    monkeypatch.setattr(inbox, "client", lambda _: Timeout())
    fresh = message(clock[0])
    auto.process_thread(db, user, account, "thread-one", [fresh], match)
    with SessionLocal() as restart:
        auto.process_thread(restart, restart.get(User, user.id), account, "thread-one", [fresh], match)
    assert len(posts) == 1
    assert auto.handoff_state(db, user.id, account, "thread-one")["held"]


def test_claude_requires_authorised_phrase_and_exact_fresh_account_scan(db, channel, monkeypatch):
    from app.services import inbox_claude, inbox_scanner
    user, account, clock, posts, window, _ = channel
    monkeypatch.setattr(inbox_claude, "configured", lambda: True)
    monkeypatch.setattr(inbox_scanner, "enabled", lambda *_: True)
    auto.update(db, user, account, "ai-save", topics="viewing")
    assert not auto.settings(db, user, account).get("ai_enabled")
    auto.update(db, user, account, "ai-enable")
    assert not auto.settings(db, user, account)["enabled"]
    auto.update(db, user, account, "start")
    clock[0] += timedelta(minutes=2)
    window.append(message(clock[0]))

    def cache(**changes):
        result = {"source": "Claude", "kind": "business", "needs_owner": False, "reason": "Routine viewing question",
                  "draft": "Which property would you like to see?", "message_stamp": inbox_scanner.message_stamp(window),
                  "scanned_at": clock[0].replace(tzinfo=None).isoformat(), **changes}
        inbox_scanner.save_value(db, inbox_scanner.result_key(user.id, account, "thread-one"), result, user.email)
        db.commit()

    def poll():
        auto.poll_account(db, user, account, match)
        clock[0] += timedelta(minutes=1)

    watermark = auto.settings(db, user, account)["scanned_since"]
    poll()
    assert not posts and auto.settings(db, user, account)["scanned_since"] == watermark
    cache(message_stamp="another-thread-content")
    poll()
    cache(source="Local classifier")
    poll()
    cache(scanned_at=(clock[0]-timedelta(minutes=11)).isoformat())
    poll()
    assert not posts
    cache()
    poll()
    poll()
    assert len(posts) == 1 and posts[0][:2] == ("thread-one", "Which property would you like to see?")

    window[:] = [{**message(clock[0], ident="unapproved-phrase"), "body": "What is the asking price?"}]
    cache()
    poll()
    assert len(posts) == 1
    window[:] = [message(clock[0], ident="sensitive-request")]
    cache(needs_owner=True, reason="Price commitment needs checking")
    poll()
    assert auto.handoff_state(db, user.id, account, "thread-one")["held"]
    assert len(posts) == 1


def test_opt_out_holds_even_when_local_classifier_is_unclear(db, channel):
    user, account, clock, posts, _, _ = channel
    enable(db, user, account)
    clock[0] += timedelta(minutes=1)
    row = {**message(clock[0]), "body": "STOP"}
    auto.process_thread(db, user, account, "thread-one", [row],
                        lambda *a, **k: {"kind": "unclassified", "needs_owner": True, "reason": "Unclear"})
    assert auto.handoff_state(db, user.id, account, "thread-one")["held"]
    assert not posts


def test_exact_keyword_on_business_page_only_with_real_classifier(db, channel):
    from app.services.inbox_triage import analyse
    user, account, clock, posts, _, _ = channel
    auto.update(db, user, account, "add", keyword="income", reply="Here is the guide you requested.")
    rule = auto.settings(db, user, account)["rules"][0]
    auto.update(db, user, account, "enable", rule_id=rule["id"])
    auto.update(db, user, account, "start")
    clock[0] += timedelta(minutes=1)
    def inbound(body, ident):
        return [{**message(clock[0], ident=ident), "body": body}]
    assert analyse(inbound("INCOME!", "1"), account.brand)["kind"] == "unclassified"
    auto.process_thread(db, user, account, "ordinary", inbound("I'm looking to buy an investment property with my rental income.", "2"), analyse)
    auto.process_thread(db, user, account, "ordinary", inbound("incoming income tax", "3"), analyse)
    assert not posts
    auto.process_thread(db, user, account, "ordinary", inbound("INCOME!", "4"), analyse)
    assert len(posts) == 1 and posts[0][1] == "Here is the guide you requested."
    auto.process_thread(db, user, account, "ordinary", inbound("Do not contact me", "4"), analyse)
    assert auto.handoff_state(db, user.id, account, "ordinary")["held"]  # An edited inbound retains its source ID.
