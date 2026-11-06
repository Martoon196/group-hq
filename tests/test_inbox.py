import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app import config
from app.db import SessionLocal
from app.models import InboxReply, User
from app.services import inbox
from app.services.inbox_providers import InboxError


@pytest.fixture
def connected(monkeypatch):
    monkeypatch.setattr(config, "GHQ_INBOX_ACCOUNTS", json.dumps([
        {"key": "kp-fb", "label": "Key Properties", "brand": "kp", "provider": "zernio", "channel": "facebook",
         "account_id": "kp-native-account", "profile_id": "kp-profile", "credential_env": "GHQ_INBOX_TEST", "can_reply": True},
        {"key": "mhp-ig", "label": "Martin Howard Property", "brand": "mhp", "provider": "zernio", "channel": "instagram",
         "account_id": "mhp-native-account", "profile_id": "mhp-profile", "credential_env": "GHQ_INBOX_TEST", "can_reply": True}]))
    monkeypatch.setenv("GHQ_INBOX_TEST", "test-inbox-token")
    sends = []

    class Fake:
        def __init__(self, account):
            self.account = account
        def conversations(self):
            return [{"id": "same-provider-thread-id", "name": "<script>Contact</script>", "participant_id": "native-person",
                     "preview": "Durham enquiry", "updated_at": "2026-11-06T12:00:00Z", "unread": 1}], False
        def messages(self, thread, cursor=None):
            return [{"id": "m1", "body": "<script>Message</script>", "direction": "incoming", "name": "Test contact",
                     "occurred_at": "2026-11-06T12:00:00Z"}], None
        def send(self, thread, text, request_id):
            sends.append((self.account.key, thread, text, request_id))
            return "provider-message"

    monkeypatch.setattr(inbox, "client", Fake)
    monkeypatch.setattr("app.routers.inbox.client", Fake)
    return sends


def owner(db):
    return db.scalars(select(User)).first()


def form(db, account="kp-fb", **changes):
    a = next(a for a in inbox.accounts() if a.key == account)
    result = {"thread_ticket": inbox.ticket(a, "same-provider-thread-id", owner(db).id, "Test contact"),
              "request_id": str(uuid.uuid4()), "message": "This is the requested test reply"}
    return {**result, **changes}


def test_owner_screen_auth_filters_and_escaping(client, admin_client, connected, db):
    assert client.get("/inbox").status_code == 303
    page = admin_client.get("/inbox")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Key Properties" in page.text and "Martin Howard Property" in page.text
    assert "&lt;script&gt;Contact&lt;/script&gt;" in page.text and "<script>Contact</script>" not in page.text
    _, _, rows = inbox.snapshot(owner(db).id, ["kp-fb"], "", "facebook")
    assert len(rows) == 1 and rows[0]["account"].brand == "kp"
    assert admin_client.get("/inbox?account=unknown").status_code == 400
    assert connected == []


def test_connection_default_brand_change_invalidates_composer_but_keeps_source(admin_client, connected, db):
    before = next(a for a in inbox.accounts(db) if a.key == 'kp-fb')
    old_ticket = inbox.ticket(before, 'same-provider-thread-id', owner(db).id, 'Buyer', 'native-person')
    response = admin_client.post('/inbox/channels/brand', data={'account_key': 'kp-fb', 'brand': 'pp'}, follow_redirects=False)
    assert response.status_code == 303
    after = next(a for a in inbox.accounts(db) if a.key == 'kp-fb')
    assert after.brand == 'pp' and after.account_id == before.account_id and after.identity == before.identity
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        inbox.resolve(old_ticket, owner(db).id, db=db)
    page = admin_client.get('/inbox/channels')
    assert page.status_code == 200 and 'current connection health' in page.text
    assert connected == []


def test_official_opened_conversation_projects_only_explicit_business_copy(admin_client, connected, db, monkeypatch):
    from app.services import inbox_crm, inbox_organisation
    user = owner(db)
    account = next(a for a in inbox.accounts(db) if a.key == 'kp-fb')
    ticket = inbox.ticket(account, 'same-provider-thread-id', user.id, 'Buyer', 'native-person')
    contact_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())
    calls = []
    def native(actor, brand, action, **payload):
        calls.append(action)
        return {'brand': brand, 'contactId': contact_id, 'workspaceId': workspace_id}
    monkeypatch.setattr(inbox_crm, 'command', native)
    inbox_crm.connect(db, user, ticket, 'kp', 'Buyer', copy_messages=False)
    assert admin_client.get('/inbox', params={'thread': ticket}).status_code == 200
    assert calls == ['create']
    inbox_organisation.save(db, user, account, 'same-provider-thread-id', 'business', ['kp'], ['kp'])
    assert admin_client.get('/inbox', params={'thread': ticket}).status_code == 200
    assert calls == ['create', 'activity']
    assert admin_client.get('/inbox', params={'thread': ticket}).status_code == 200
    assert calls == ['create', 'activity']
    inbox_organisation.save(db, user, account, 'same-provider-thread-id', 'personal', ['kp'], ['kp'])
    monkeypatch.setattr(inbox.client, 'messages', lambda *args, **kwargs: ([{'id':'m2','body':'Private reply',
        'direction':'incoming','name':'Buyer','occurred_at':'2026-11-06T12:01:00Z'}], None))
    assert admin_client.get('/inbox', params={'thread': ticket}).status_code == 200
    assert calls == ['create', 'activity'] and connected == []


def test_reply_stays_on_signed_account_and_replay_has_no_effect(admin_client, connected, db):
    payload = form(db)
    payload["account"] = "mhp-ig"  # A submitted filter cannot retarget the signed conversation.
    response = admin_client.post("/inbox/reply", data=payload)
    assert response.status_code == 303
    assert connected == [("kp-fb", "same-provider-thread-id", payload["message"], "ghq-" + payload["request_id"])]
    assert admin_client.post("/inbox/reply", data=payload).status_code == 303
    assert len(connected) == 1
    receipt = db.get(InboxReply, payload["request_id"])
    assert receipt.state == "accepted" and receipt.account_key == "kp-fb"


def test_changed_content_same_request_id_conflicts_and_retains_draft(admin_client, connected, db):
    payload = form(db)
    assert admin_client.post("/inbox/reply", data=payload).status_code == 303
    changed = {**payload, "message": "Changed private draft"}
    response = admin_client.post("/inbox/reply", data=changed)
    assert response.status_code == 409 and "Changed private draft" in response.text
    assert len(connected) == 1
    changed_account = form(db, "mhp-ig", request_id=payload["request_id"])
    assert admin_client.post("/inbox/reply", data=changed_account).status_code == 409
    assert len(connected) == 1


def test_origin_tamper_and_rotated_binding_refused(admin_client, connected, db, monkeypatch):
    payload = form(db)
    assert admin_client.post("/inbox/reply", data=payload, headers={"Origin": "https://evil.test"}).status_code == 403
    assert admin_client.post("/inbox/reply", data={**payload, "thread_ticket": "tampered"}).status_code == 409
    monkeypatch.setenv("GHQ_INBOX_TEST", "rotated-test-token")
    assert admin_client.post("/inbox/reply", data=payload).status_code == 409
    assert connected == []
    assert db.scalar(select(func.count()).select_from(InboxReply)) == 0


def test_unknown_outcome_is_durable_and_never_retried(admin_client, connected, db, monkeypatch):
    class Timeout:
        def send(self, *args):
            connected.append(args)
            raise InboxError("outcome_unknown")
    monkeypatch.setattr(inbox, "client", lambda a: Timeout())
    payload = form(db)
    assert admin_client.post("/inbox/reply", data=payload).status_code == 303
    assert db.get(InboxReply, payload["request_id"]).state == "outcome_unknown"
    assert admin_client.post("/inbox/reply", data=payload).status_code == 303
    assert len(connected) == 1


def test_provider_crash_after_claim_is_not_repeated(connected, db, monkeypatch):
    class Crash:
        def send(self, *args):
            connected.append(args)
            raise RuntimeError("process interrupted")
    monkeypatch.setattr(inbox, "client", lambda a: Crash())
    a, u, request_id = inbox.accounts()[0], owner(db), str(uuid.uuid4())
    with pytest.raises(RuntimeError):
        inbox.send(db, u, a, "thread", "Test reply", request_id)
    assert db.get(InboxReply, request_id).state == "calling"
    receipt = inbox.send(db, u, a, "thread", "Test reply", request_id)
    assert receipt.state == "calling" and len(connected) == 1


def test_concurrent_duplicate_calls_provider_once(connected, db):
    user_id, account, request_id = owner(db).id, inbox.accounts()[0], str(uuid.uuid4())
    def submit(_):
        with SessionLocal() as s:
            return inbox.send(s, s.get(User, user_id), account, "thread", "Test reply", request_id).state
    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(submit, range(2)))
    assert all(state in ("calling", "accepted") for state in states)
    assert len(connected) == 1


def test_partial_account_failure_does_not_erase_other_inboxes(connected, db, monkeypatch):
    original = inbox.client
    class Fails:
        def conversations(self):
            raise InboxError("permission_needed")
    monkeypatch.setattr(inbox, "client", lambda a: Fails() if a.key == "mhp-ig" else original(a))
    _, results, threads = inbox.snapshot(owner(db).id, [])
    assert len(threads) == 1 and threads[0]["account"].key == "kp-fb"
    assert results[1]["error"] and not results[0]["error"]


def test_empty_inbox_is_setup_not_claimed_connected(admin_client, monkeypatch):
    monkeypatch.setattr(config, "GHQ_INBOX_ACCOUNTS", "[]")
    page = admin_client.get("/inbox")
    assert page.status_code == 200
    assert "No inbox account has been connected" in page.text
    assert "YouTube and public comment feeds have not been connected" in page.text
