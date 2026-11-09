"""Private one-to-one text from the existing, device-bound WhatsApp listener."""
import hashlib
import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import update

from .. import config
from ..models import InboxReply, InboxThread, KeywordSuppression, utcnow
from . import personal_inbox, whatsapp_keywords as keywords
from .audit import audit
from .inbox_providers import Account

KEY = "inbox.whatsapp.personal.v1"
ACCOUNT_KEY = "personal-whatsapp-device"
SOURCE = "whatsapp-device"


class DeviceAccount(Account):
    @property
    def binding(self):
        # The owner may pause replies without changing the identity of saved private chats.
        return Account.binding.fget(replace(self, can_reply=True))


def status(db):
    return keywords.setting(db, KEY, {})


def account(db):
    saved = status(db)
    email = config.GHQ_PERSONAL_INBOX_OWNER_EMAIL
    if not email or saved.get("owner") != email or not saved.get("since"):
        return None
    return DeviceAccount(key=ACCOUNT_KEY, brand="group", channel="whatsapp", provider="local",
        account_id=saved["device"], user_id=saved["device"], owner_email=email, label="Personal WhatsApp",
        can_reply=saved.get("collect") is True and saved.get("reply") is True,
        token=config.WHATSAPP_KEYWORD_ADAPTER_SECRET)


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(400, "Invalid message timestamp") from None


def online(db):
    seen = status(db).get("seen")
    return bool(seen and (utcnow() - timestamp(seen)).total_seconds() < 120)


def iso(value):
    return value.isoformat() + "Z"


def configure(db, user, collect, reply):
    keywords.lock_rules(db)
    saved = dict(status(db))
    if not saved.get("device") or saved.get("owner") != user.email:
        raise HTTPException(409, "Start the linked WhatsApp listener before configuring this account.")
    if reply and not collect:
        raise HTTPException(400, "Enable private conversation collection before allowing replies.")
    if collect and not saved.get("collect"):
        saved["since"] = iso(utcnow())
    saved.update(collect=collect, reply=reply)
    keywords.put_setting(db, KEY, saved, user.email)
    if not reply:
        db.execute(update(InboxReply).where(InboxReply.user_id == user.id, InboxReply.account_key == ACCOUNT_KEY,
            InboxReply.state == "queued").values(state="rejected", reason="replies_disabled", settled_at=utcnow()))
    audit(db, user, "inbox.whatsapp.configure", "inbox-account:" + ACCOUNT_KEY, after={"collect":collect,"reply":reply})
    db.commit()


def peer_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{4,29}@(c\.us|lid)", value):
        raise HTTPException(400, "A native one-to-one WhatsApp identity is required.")
    return value


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def phone_key(device, peer):
    return "inbox.wa.peer." + sha(device + "/" + peer)


def reply_allowed(db, device, peer):
    number = peer.split("@")[0] if peer.endswith("@c.us") else None
    phone_sha = sha(number) if number else keywords.setting(db, phone_key(device, peer), {}).get("phone_sha")
    if not phone_sha:
        return False
    suppression = db.get(KeywordSuppression, phone_sha)
    return not suppression or not suppression.blocked


def ingest(db, saved, user, data):
    occurred = timestamp(data.get("at"))
    if not saved.get("collect") or occurred < timestamp(saved["since"]):
        return {"stored": False}
    if (occurred - utcnow()).total_seconds() > 300:
        raise HTTPException(400, "Invalid message time")
    peer = peer_id(data.get("peer"))
    if saved["device"] == "wa-device:" + sha(peer):
        raise HTTPException(400, "Self conversations cannot be imported.")
    ident = data.get("id")
    body, direction = data.get("body"), data.get("direction")
    if (not isinstance(ident, str) or not re.fullmatch(r"baileys:[A-Za-z0-9_:-]{1,200}", ident)
        or direction not in ("incoming", "outgoing") or not isinstance(body, str) or not 1 <= len(body.encode()) <= 20000):
        raise HTTPException(400, "Invalid private text event")
    phone = data.get("phone")
    if phone is not None:
        if not isinstance(phone, str) or not re.fullmatch(r"[1-9][0-9]{6,14}", phone) or (peer.endswith("@c.us") and peer != phone + "@c.us"):
            raise HTTPException(400, "Invalid native peer mapping")
        if peer.endswith("@lid"):
            key = phone_key(saved["device"], peer)
            old = keywords.setting(db, key, {}).get("phone_sha")
            if old and old != sha(phone):
                raise HTTPException(409, "Native peer identity changed")
            if not old:
                keywords.put_setting(db, key, {"phone_sha": sha(phone)}, "device:whatsapp")
        # Store the mapping first. put_setting expires pending ORM changes.
        if direction == "incoming" and body.strip().upper() in keywords.STOP_WORDS | {"START"}:
            keywords.apply_control(db, sha(phone), occurred, body.strip().upper() != "START")
    display = str(data.get("name") or "")[:200] if direction == "incoming" else ""
    thread_sha = sha(peer)
    selected = account(db)
    row = db.get(InboxThread, (selected.key, thread_sha))
    if row and row.owner_user_id != user.id:
        raise HTTPException(409, "This conversation belongs to a different owner.")
    if row:
        # The registered native device is immutable. A fresh event can rebind its cached chat after signing-key rotation.
        row.binding_sha256 = selected.binding
    if row is None:
        row = InboxThread(account_key=selected.key, thread_sha256=thread_sha, thread_id=peer, participant_id=peer,
            owner_user_id=user.id, binding_sha256=selected.binding, name=display or "WhatsApp contact", messages=[], has_more=False)
        db.add(row)
    messages = row.messages
    previous = next((m for m in messages if m["id"] == ident), None)
    if previous:
        if previous["direction"] != direction or previous["body"] != body:
            raise HTTPException(409, "Native message identity changed")
        return {"stored": False}
    stamp = iso(occurred)
    message = {"id":ident, "body":body, "name":display, "direction":direction, "occurred_at":stamp, "revision":""}
    ordered = sorted([*messages, message], key=lambda item: timestamp(item["occurred_at"]))
    row.has_more = row.has_more or len(ordered) > 100
    row.messages = ordered[-100:]
    if display:
        row.name = display
    row.preview = row.messages[-1]["body"]
    row.provider_updated_at = row.messages[-1]["occurred_at"]
    row.seen_at = utcnow()
    db.commit()
    return {"stored":True,"thread_sha256":thread_sha}


def dispatch(db, action, data):
    # This entry point is called only after the listener-specific HMAC is verified.
    keywords.lock_rules(db)
    user = personal_inbox.owner(db)
    saved = dict(status(db))
    device = data.get("device")
    if (data.get("listenerId") != keywords.LISTENER or not isinstance(device, str)
            or not re.fullmatch(r"wa-device:[0-9a-f]{64}", device) or saved.get("device", device) != device
            or saved.get("owner", user.email) != user.email):
        raise HTTPException(403, "Wrong linked WhatsApp account")
    if action == "policy":
        return {"history": bool(saved.get("collect"))}
    if action == "status":
        saved.update(owner=user.email, device=device, seen=iso(utcnow()))
        keywords.put_setting(db, KEY, saved, "device:whatsapp")
        db.commit()
        return {"collect":bool(saved.get("collect")), "reply":bool(saved.get("reply")), "since":saved.get("since"),
                "history":bool(saved.get("collect"))}
    if not saved.get("device"):
        raise HTTPException(409, "Register the linked device first")
    if action == "history":
        if not saved.get("collect"):
            raise HTTPException(409, "Personal conversation collection is paused.")
        from . import inbox_archive
        from .inbox_history_registration import ensure_thread
        selected = account(db)
        thread = data.get("thread") or {}
        peer = peer_id(thread.get("id"))
        if (data.get("connector_id") != SOURCE or data.get("account_key") != ACCOUNT_KEY
                or data.get("account_id") != device or data.get("user_id") != device
                or thread.get("participant_id") != peer or device == "wa-device:" + sha(peer)):
            raise HTTPException(403, "The archive belongs to a different account or conversation.")
        ensure_thread(db, user, selected, thread)
        packet = {k:v for k,v in data.items() if k not in ("device", "listenerId")}
        return inbox_archive.store_history(db, user, selected, packet)
    if action == "event":
        return ingest(db, saved, user, data)
    if action == "pending":
        if not saved.get("reply"):
            return {"reply_ids":[]}
        return personal_inbox.pending(db, SOURCE, "whatsapp")
    parts = action.split("/")
    if len(parts) != 3 or parts[0] != "replies":
        raise HTTPException(404)
    try:
        if str(uuid.UUID(parts[1])) != parts[1]:
            raise ValueError()
    except ValueError:
        raise HTTPException(400, "Invalid reply ID") from None
    row = db.get(InboxReply, parts[1])
    if row is None or row.account_key != ACCOUNT_KEY or row.user_id != user.id:
        raise HTTPException(404, "Unknown private reply")
    if parts[2] == "settle":
        if row.state == "queued" and data.get("state") == "outcome_unknown":
            # The connector persisted its intention but the begin request never claimed this row.
            row.state, row.reason, row.settled_at = "rejected", "dispatch_interrupted", utcnow()
            db.commit()
            return {"state":"rejected"}
        if row.state == "rejected" and data.get("state") == "outcome_unknown":
            return {"state":"rejected"}
        return personal_inbox.settle(db, row.id, data, source=SOURCE)
    if parts[2] == "begin":
        if row.state == "queued" and (not saved.get("reply") or not reply_allowed(db, device, peer_id(row.thread_id))):
            row.state, row.reason, row.settled_at = "rejected", "private_reply_blocked", utcnow()
            db.commit()
            return {"dispatch":False, "state":"rejected"}
        return personal_inbox.begin(db, row.id, SOURCE, "whatsapp")
    raise HTTPException(404)
