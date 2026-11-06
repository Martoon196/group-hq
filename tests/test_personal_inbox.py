import hashlib
import hmac
import json
import time
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import config
from app.models import InboxCrmActivityReceipt, InboxCrmLink, InboxReply, User, utcnow
from app.services import inbox, inbox_crm, personal_inbox as personal


@pytest.fixture
def personal_account(db, monkeypatch):
    user = db.scalars(select(User)).first()
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", user.email)
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_SECRET", "x" * 32)
    monkeypatch.setenv("GHQ_INBOX_PERSONAL_SECRET", "x" * 32)
    result = personal.discover(db, {"accounts": [{"channel": "facebook", "account_id": "personal-account", "user_id": "me",
        "engine_provider": "local", "status": "connected", "label": "Martin personal Messenger"}]})
    saved = personal.catalog(db)
    saved["accounts"][0]["enabled"], saved["accounts"][0]["brand"] = True, "kp"
    personal.save_catalog(db, saved, user.email)
    db.commit()
    binding = result["accounts"][0]
    personal.ingest(db, {"account_key": binding["key"], "account_id": "personal-account", "user_id": "me",
        "observed_at": utcnow().isoformat() + "Z", "thread": {"id": "durham-thread", "participant_id": "native-peer",
        "name": "Durham prospect", "preview": "Interested in Durham", "updated_at": "2026-11-06T12:00:00Z",
        "is_group": False, "is_merged": False, "messages": [{"id": "native-message", "body": "Interested in Durham",
            "direction": "incoming", "name": "Durham prospect", "occurred_at": "2026-11-06T12:00:00Z"}]}})
    account = inbox.accounts(db)[0]
    return user, account, inbox.ticket(account, "durham-thread", user.id, "Durham prospect", "native-peer")


def signed(action, body):
    path = personal.BASE + "/" + action
    raw = json.dumps(body, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    signature = hmac.new(b"x" * 32, f"{stamp}\nPOST\n{path}\n".encode() + raw, hashlib.sha256).hexdigest()
    return path, raw, {"content-type": "application/json", "x-ghq-timestamp": stamp, "x-ghq-signature": signature}


def test_private_accounts_cache_and_tickets_are_owner_bound(admin_client, db, personal_account):
    user, account, ticket = personal_account
    page = admin_client.get("/inbox")
    assert "Durham prospect" in page.text and "Connect personal Messenger" in page.text
    assert "Interested in Durham" in admin_client.get("/inbox?thread=" + ticket).text
    assert inbox.snapshot(user.id, [], db=db, user_email="another@example.test")[0] == ()
    with pytest.raises(HTTPException) as error:
        inbox.resolve(ticket, user.id, "another@example.test", db)
    assert error.value.status_code == 409


def test_adapter_hmac_is_exact_body_and_path_bound(client, db, personal_account):
    _, account, _ = personal_account
    path, raw, headers = signed("accounts", {"accounts": []})
    assert client.post(path, content=raw, headers=headers).status_code == 200
    assert client.post(path, content=raw + b" ", headers=headers).status_code == 401
    assert client.post(path + "?x=1", content=raw, headers=headers).status_code == 400
    assert client.post(personal.BASE + "/threads", content=raw, headers=headers).status_code == 401


def test_original_account_queue_claim_and_pending_resolution(db, personal_account):
    user, account, _ = personal_account
    ident = str(uuid.uuid4())
    row = inbox.send(db, user, account, "durham-thread", "Requested reply", ident)
    assert row.state == "queued" and personal.pending(db)["reply_ids"] == [ident]
    assert personal.begin(db, ident)["dispatch"] is True
    assert personal.begin(db, ident)["dispatch"] is False
    assert inbox.send(db, user, account, "durham-thread", "Requested reply", ident).state == "calling"
    personal.settle(db, ident, {"state": "bridge_accepted", "provider_message_id": "temporary-id"})
    with pytest.raises(HTTPException):
        personal.settle(db, ident, {"state": "accepted", "provider_message_id": "different"})
    db.rollback()
    result = {"state": "accepted", "provider_message_id": "native-id", "previous_message_id": "temporary-id"}
    assert personal.settle(db, ident, result)["state"] == "accepted"
    assert personal.settle(db, ident, result)["state"] == "accepted"
    with pytest.raises(HTTPException):
        personal.settle(db, ident, {**result, "provider_message_id": "changed"})


def test_crm_uses_signed_native_source_and_only_stores_confirmed_ids(db, personal_account, monkeypatch):
    user, account, ticket = personal_account
    contact_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())
    calls = []
    def native(actor, brand, action, **payload):
        calls.append((brand, action, payload))
        return {"brand": brand, "contactId": contact_id, "workspaceId": workspace_id}
    monkeypatch.setattr(inbox_crm, "command", native)
    result = inbox_crm.connect(db, user, ticket, "kp", "Durham prospect")
    assert result["contactId"] == contact_id and calls[0][2]["source"] == {
        "channel": "facebook", "accountId": account.account_id, "threadId": "durham-thread", "participantId": "native-peer"}
    cached = inbox.local_thread(db, account, "durham-thread", user.id)
    assert inbox_crm.record_messages(db, user, account, "durham-thread", cached.messages) == 1
    assert inbox_crm.record_messages(db, user, account, "durham-thread", cached.messages) == 0
    assert db.scalars(select(InboxCrmActivityReceipt)).all()
    assert db.scalars(select(InboxCrmLink)).first().contact_id == contact_id
    def unavailable(*args, **kwargs):
        raise inbox_crm.CrmUnavailable("Unavailable")
    monkeypatch.setattr(inbox_crm, "command", unavailable)
    with pytest.raises(inbox_crm.CrmUnavailable):
        inbox_crm.connect(db, user, ticket, "mhp", "Durham prospect")
    assert len(db.scalars(select(InboxCrmLink)).all()) == 1
