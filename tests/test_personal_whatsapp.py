import hashlib
import hmac
import json
import time
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app import config
from app.models import InboxReply, InboxThread, User, utcnow
from app.services import inbox, inbox_crm, personal_inbox, whatsapp_direct as direct, whatsapp_keywords

SECRET = "test-wa-direct-" + "k" * 32
DEVICE = "wa-device:" + "a" * 64
PEER = "447700900120@c.us"


@pytest.fixture
def wa(db, client, monkeypatch):
    user = db.scalars(select(User)).first()
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", user.email)
    monkeypatch.setattr(config, "WHATSAPP_KEYWORD_ADAPTER_SECRET", SECRET)
    def call(action, **fields):
        path = whatsapp_keywords.BASE + "/direct/" + action
        raw = json.dumps({"listenerId":whatsapp_keywords.LISTENER, "device":DEVICE, **fields}).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(SECRET.encode(), f"{stamp}\nPOST\n{path}\n".encode() + raw, hashlib.sha256).hexdigest()
        return client.post(path, content=raw, headers={"x-ghq-timestamp":stamp, "x-ghq-signature":signature})
    return user, call


def event(**kw):
    return {"peer":PEER, "id":"baileys:m-one", "direction":"incoming", "body":"Fixture enquiry",
        "name":"Fixture", "at":direct.iso(utcnow()), "phone":PEER.split("@")[0], **kw}


def test_owner_opts_in_to_new_one_to_one_text_and_original_channel(admin_client, db, wa, monkeypatch):
    user, call = wa
    assert call("status").json() == {"collect":False, "reply":False, "since":None, "history":False}
    assert call("event", **event()).json() == {"stored":False}
    assert admin_client.get("/inbox/connect").status_code == 200
    form = "/inbox/connect/whatsapp"
    assert admin_client.post(form, data={"reply":"true"}).status_code == 400
    assert admin_client.post(form, data={"collect":"true"}).status_code == 303
    assert call("event", **event(at=direct.iso(utcnow()-timedelta(days=1)))).json() == {"stored":False}
    assert call("event", **event(peer="123456789@g.us")).status_code == 400
    assert call("event", **event(device="wa-device:"+"b"*64)).status_code == 403
    data = event()
    response = call("event", **data)
    assert response.status_code == 200, response.text
    assert response.json()["stored"]
    assert call("event", **data).json() == {"stored":False}
    assert call("event", **{**data, "body":"conflicting text"}).status_code == 409
    account = direct.account(db)
    assert account and not account.can_reply
    thread = inbox.local_thread(db, account, PEER, user.id)
    assert thread.participant_id == PEER and len(thread.messages) == 1
    thread.seen_at = utcnow()-timedelta(days=2)
    db.commit()
    assert admin_client.post(form, data={"collect":"true","reply":"true"}).status_code == 303
    db.expire_all()
    account = direct.account(db)
    assert account.can_reply and account.binding == thread.binding_sha256
    ident = str(uuid.uuid4())
    inbox.send(db, user, account, PEER, "Fixture manual reply", ident)
    assert personal_inbox.pending(db)["reply_ids"] == []
    with pytest.raises(HTTPException):
        personal_inbox.begin(db, ident, "beeper")
    db.rollback()  # The rejected service call ran inside this test's separate transaction.
    assert call("pending").json()["reply_ids"] == [ident]
    job = call("replies/" + ident + "/begin").json()
    assert job["dispatch"] and (job["account_id"],job["thread_id"],job["channel"]) == (DEVICE,PEER,"whatsapp")
    assert not call("replies/" + ident + "/begin").json()["dispatch"]
    assert call("replies/"+ident+"/settle", state="bridge_accepted", provider_message_id="baileys:m-two").status_code == 200
    assert call("event", **event(id="baileys:m-two",direction="outgoing",body="Fixture manual reply")).json()["stored"]
    db.expire_all()
    origins=[]
    def command(actor, brand, action, **payload):
        origins.append(payload["source"])
        return {"brand":brand, "contactId":str(uuid.uuid4()), "workspaceId":str(uuid.uuid4())}
    monkeypatch.setattr(inbox_crm,"command",command)
    inbox_crm.connect(db,user,inbox.ticket(account,PEER,user.id,"Fixture",PEER),"kp","Fixture")
    assert origins == [{"channel":"whatsapp","accountId":DEVICE,"threadId":PEER,"participantId":PEER}]


def test_stop_disable_and_interrupted_claim_never_send(admin_client, db, wa):
    user, call = wa
    call("status")
    form = "/inbox/connect/whatsapp"
    admin_client.post(form, data={"collect":"true","reply":"true"})
    peer = "123456789@lid"
    assert call("event", **event(peer=peer, phone=None)).json()["stored"]
    account = direct.account(db)
    with pytest.raises(HTTPException):
        inbox.send(db, user, account, peer, "No mapping", str(uuid.uuid4()))
    call("event", **event(peer=peer,id="baileys:m-two"))
    first = str(uuid.uuid4())
    inbox.send(db, user, account, peer, "Private reply", first)
    call("event", **event(peer=peer,id="baileys:m-stop",body="STOP"))
    assert call("replies/"+first+"/begin").json() == {"state":"rejected","dispatch":False}
    resumed = call("event", **event(peer=peer,id="baileys:m-start",body="START"))
    assert resumed.status_code == 200 and resumed.json()["stored"], resumed.text
    db.expire_all()
    assert direct.reply_allowed(db, DEVICE, peer)
    second = str(uuid.uuid4())
    inbox.send(db, user, account, peer, "Interrupted reply", second)
    assert call("replies/"+second+"/settle",state="outcome_unknown").json() == {"state":"rejected"}
    third = str(uuid.uuid4())
    inbox.send(db,user,account,peer,"Future disabled reply",third)
    admin_client.post(form, data={})
    assert call("pending").json() == {"reply_ids":[]}
    assert call("replies/"+third+"/begin").json() == {"dispatch":False,"state":"rejected"}
    assert call("event", **event(peer=peer,id="baileys:m-disabled")).json() == {"stored":False}
    db.expire_all()
    assert db.get(InboxReply,third).reason == "replies_disabled"


def test_history_registers_only_verified_cold_dm_without_live_effects(admin_client, db, wa, monkeypatch):
    import types
    from app import services
    user, call = wa
    call("status")
    assert call("policy").json() == {"history":False}
    admin_client.post("/inbox/connect/whatsapp",data={"collect":"true"})
    assert call("policy").json() == {"history":True}
    seen=[]
    def store(session, actor, account, data):
        assert actor.id == user.id and account.account_id == DEVICE
        row = inbox.local_thread(session, account, PEER, actor.id)
        assert row.participant_id == PEER and row.messages == [] and row.preview == ""
        assert "listenerId" not in data and "device" not in data
        seen.extend(data["thread"]["messages"])
        session.commit()
        return {"ok":True, "stored":len(seen), "chunk_id":data["chunk_id"], "reached_start":data["reached_start"]}
    monkeypatch.setattr(services,"inbox_archive",types.SimpleNamespace(store_history=store),raising=False)
    packet={"connector_id":direct.SOURCE,"account_id":DEVICE,"user_id":DEVICE,"account_key":direct.ACCOUNT_KEY,
        "chunk_id":"1"*64, "oldest_id":"baileys:old", "newest_id":"baileys:old","reached_start":True,
        "observed_at":direct.iso(utcnow()), "thread":{"id":PEER,"participant_id":PEER,"is_group":False,"is_merged":False,
        "name":"Historic peer", "messages":[{"id":"baileys:old","body":"STOP","direction":"incoming",
            "name":"Historic peer","occurred_at":"2016-03-04T09:00:00Z","revision":""}]}}
    wrong={**packet,"thread":{**packet["thread"],"participant_id":"something-else"}}
    assert call("history",**wrong).status_code == 403
    assert call("history",**{**packet,"account_id":"different"}).status_code == 403
    assert call("history",**packet).json() == {"ok":True,"stored":1,"chunk_id":"1"*64,"reached_start":True}
    assert db.scalar(select(InboxReply.id)) is None and direct.reply_allowed(db, DEVICE, PEER)
    assert direct.account(db) and not direct.account(db).can_reply
    admin_client.post("/inbox/connect/whatsapp",data={})
    assert call("history",**packet).status_code == 409
