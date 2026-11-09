import uuid

import pytest
from sqlalchemy import select

from app import config
from app.models import InboxReply, User
from app.services import inbox, inbox_crm
from app.services.inbox_providers import InboxError
from app.services.inbox_r72_whatsapp import WhatsAppReader


@pytest.fixture
def canonical(monkeypatch):
    account_id, thread, person, message = (str(uuid.uuid4()) for _ in range(4))
    monkeypatch.setattr(config, "GHQ_MHP_WHATSAPP_INBOX_ID", account_id)
    monkeypatch.setattr(config, "R72_GROUP_HQ_INBOX_SECRET", "fixture-credential-" * 3)
    calls = []

    def read(actor, brand, action, **data):
        calls.append((actor.email, action, data))
        assert brand == "mhp" and data["accountId"] == account_id
        common = dict(brand=brand, accountId=account_id)
        if action == "whatsappThreads":
            return dict(common, hasMore=False, threads=[dict(id=thread, participantId=person,
                name="Durham owner", preview="Received message", updatedAt="2026-11-07T11:00:00Z")])
        assert action == "whatsappMessages" and data["threadId"] == thread
        return dict(common, threadId=thread, nextCursor=None, messages=[dict(id=message, participantId=person,
            name="Durham owner", body="Received from the MHP number", occurredAt="2026-11-07T11:00:00Z")])

    monkeypatch.setattr(inbox_crm, "command", read)
    return account_id, thread, person, calls


def test_canonical_whatsapp_read_uses_owner_and_existing_contact(admin_client, db, canonical):
    account_id, thread, person, calls = canonical
    user = db.scalars(select(User)).first()
    all_accounts, statuses, threads = inbox.snapshot(user.id, ["mhp-whatsapp"], "mhp", "whatsapp", db=db, user_email=user.email)
    assert len(threads) == 1 and statuses[0]["error"] is None
    assert threads[0]["participant_id"] == person and threads[0]["account"].account_id == account_id
    ticket = threads[0]["ticket"]
    response = admin_client.get("/inbox", params={"account":"mhp-whatsapp", "thread":ticket})
    assert response.status_code == 200
    assert "Received from the MHP number" in response.text and "Open MHP CRM contact" in response.text
    assert person in response.text and 'action="/inbox/reply"' not in response.text
    response = admin_client.post("/inbox/reply", data={"thread_ticket":ticket, "request_id":str(uuid.uuid4()), "message":"must not send"})
    assert response.status_code == 409 and db.scalars(select(InboxReply)).all() == []
    remap = admin_client.post("/inbox/channels/brand", data={"account_key":"mhp-whatsapp", "brand":"pp"})
    assert remap.status_code == 400
    assert all(email == user.email and action in ("whatsappThreads","whatsappMessages") for email,action,_ in calls)


def test_canonical_whatsapp_refuses_a_foreign_account_or_participant(monkeypatch, canonical):
    account_id, thread, person, _ = canonical
    account = next(a for a in inbox.accounts() if a.key == "mhp-whatsapp")
    reader = WhatsAppReader(account, "owner@example.test")
    with pytest.raises(InboxError, match="unbound_target"):
        reader.messages(thread, str(uuid.uuid4()))
    monkeypatch.setattr(inbox_crm, "command", lambda *args, **kwargs: dict(brand="pp", accountId=account_id))
    with pytest.raises(InboxError, match="unbound_target"):
        reader.conversations()


def test_owner_manual_reply_uses_received_thread_and_original_request_once(admin_client, db, canonical, monkeypatch):
    from app.services import inbox_automations
    _, thread, _, _ = canonical
    monkeypatch.setattr(config, "GHQ_MHP_WHATSAPP_MANUAL_REPLIES", True)
    account = next(a for a in inbox.accounts() if a.key == "mhp-whatsapp")
    user = db.scalars(select(User)).first()
    assert not inbox_automations.available(account,user)
    request_id = str(uuid.uuid4())
    calls = []

    def bridge(actor, brand, action, **payload):
        calls.append((actor.email,brand,action,payload))
        return dict(brand=brand, accountId=account.account_id, threadId=thread, requestId=request_id, state="queued")

    monkeypatch.setattr(inbox_crm,"command",bridge)
    for _ in range(2):
        receipt=inbox.send(db,user,account,thread,"An owner reply",request_id)
        assert receipt.state=="bridge_accepted"
    assert calls==[(user.email,"mhp","whatsappReply",dict(accountId=account.account_id,threadId=thread,
                  body="An owner reply",request_id=request_id))]


def test_first_contact_is_inbox_only_and_renders_eligible_reply(admin_client, db, canonical, monkeypatch):
    account_id, thread, _, _=canonical
    monkeypatch.setattr(config, "GHQ_MHP_WHATSAPP_MANUAL_REPLIES", True)
    user=db.scalars(select(User)).first()
    account=next(a for a in inbox.accounts() if a.key=="mhp-whatsapp")
    tag=inbox.ticket(account,thread,user.id,"New sender",thread)

    def bridge(actor,brand,action,**payload):
        common=dict(brand=brand,accountId=account_id)
        if action=="whatsappThreads": return dict(common,hasMore=False,threads=[])
        assert action=="whatsappMessages"
        return dict(common,threadId=thread,canReply=True,nextCursor=None,messages=[
            dict(id=str(uuid.uuid4()),participantId=thread,contactId=None,name="New sender",body="First enquiry",
                 direction="incoming",occurredAt="2026-11-09T14:10:00Z")])

    monkeypatch.setattr(inbox_crm,"command",bridge)
    page=admin_client.get("/inbox",params={"account":account.key,"thread":tag})
    assert page.status_code==200 and "First enquiry" in page.text
    assert "Open MHP CRM contact" not in page.text and 'action="/inbox/crm"' in page.text
    assert 'action="/inbox/reply"' in page.text
