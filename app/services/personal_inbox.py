"""Private outbound-only desktop bridge. No public access to the desktop API."""
import hashlib
import hmac
import json
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .. import config
from ..models import AppSetting, InboxReply, InboxThread, User, utcnow
from . import inbox
from .audit import audit
from .inbox_providers import InboxError, clean, opaque

BASE = "/api/personal-inbox/v1"
ACCOUNTS = "personal_inbox.accounts"


def problem(code, status=400):
    raise HTTPException(status, code)


async def authenticated_body(request):
    secret = config.GHQ_PERSONAL_INBOX_SECRET
    if not 32 <= len(secret) <= 256 or not config.GHQ_PERSONAL_INBOX_OWNER_EMAIL:
        problem("Personal connector is not configured", 503)
    stamp, signature = request.headers.get("x-ghq-timestamp", ""), request.headers.get("x-ghq-signature", "")
    if not re.fullmatch(r"\d{10}", stamp) or abs(time.time() - int(stamp)) > 300 or not re.fullmatch(r"[a-f0-9]{64}", signature):
        problem("Unauthorised", 401)
    if request.url.query:
        problem("Invalid request")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 750000:
            problem("Request too large", 413)
    expected = hmac.new(secret.encode(), f"{stamp}\n{request.method}\n{request.url.path}\n".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        problem("Unauthorised", 401)
    try:
        value = json.loads(body) if body else {}
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError:
        problem("Invalid JSON")


def owner(db):
    user = db.scalar(select(User).where(User.email == config.GHQ_PERSONAL_INBOX_OWNER_EMAIL, User.is_active.is_(True)))
    if user is None:
        problem("Personal connector owner is unavailable", 403)
    return user


def catalog(db):
    saved = db.get(AppSetting, ACCOUNTS)
    return saved.value if saved else {"accounts": [], "seen_at": None}


def save_catalog(db, value, actor):
    db.execute(insert(AppSetting).values(key=ACCOUNTS, value=value, updated_by=actor, updated_at=utcnow())
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value, "updated_by": actor, "updated_at": utcnow()}))


def discover(db, data):
    user = owner(db)
    rows = data.get("accounts")
    if not isinstance(rows, list) or len(rows) > 5:
        problem("Invalid personal accounts")
    old = {r["key"]: r for r in catalog(db)["accounts"]}
    result, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or row.get("channel") not in ("facebook", "linkedin")
                or row.get("engine_provider") not in ("local", "self-hosted", "platform-sdk")):
            problem("Only the selected on-device personal connections are supported")
        native, user_id = opaque(row.get("account_id")), opaque(row.get("user_id"))
        key = "personal-" + hashlib.sha256((row["channel"] + "\0" + native).encode()).hexdigest()[:24]
        if key in seen:
            problem("Duplicate personal account")
        seen.add(key)
        previous = old.get(key)
        if previous and previous["user_id"] != user_id:
            problem("Personal account identity changed", 409)
        result.append({"key": key, "provider": "local", "account_id": native, "user_id": user_id,
            "channel": row["channel"], "label": clean(row.get("label") or row["channel"], 200),
            "brand": previous.get("brand", "group") if previous else "group", "owner_email": user.email,
            "credential_env": "GHQ_INBOX_PERSONAL_SECRET", "can_reply": True,
            "enabled": previous.get("enabled", False) if previous else False,
            "status": row.get("status") if row.get("status") in ("connected", "backfilling", "connecting", "attention_required", "disconnected") else "attention_required"})
    # Missing accounts remain visible as disconnected, never silently rerouted.
    result.extend({**r, "status": "disconnected"} for k, r in old.items() if k not in seen)
    save_catalog(db, {"accounts": result, "seen_at": utcnow().isoformat()}, "desktop:personal-inbox")
    db.commit()
    return {"accounts": [{"key": r["key"], "account_id": r["account_id"], "user_id": r["user_id"],
                          "channel": r["channel"], "enabled": r["enabled"]} for r in result]}


def enabled_account(db, key):
    user = owner(db)
    account = next((a for a in inbox.accounts(db) if a.key == key and a.provider == "local" and a.owner_email == user.email), None)
    if account is None:
        problem("Personal account is not enabled", 403)
    return account, user


def ingest(db, data):
    account, user = enabled_account(db, data.get("account_key"))
    if data.get("account_id") != account.account_id or data.get("user_id") != account.user_id:
        problem("Wrong personal account", 403)
    value = data.get("thread")
    if not isinstance(value, dict) or value.get("is_group") is not False or value.get("is_merged") is not False:
        problem("Only original one-to-one conversations can be synced")
    thread_id, participant_id = opaque(value.get("id")), opaque(value.get("participant_id"))
    observed = data.get("observed_at")
    try:
        observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
        if abs((utcnow() - observed_at).total_seconds()) > 300:
            raise ValueError()
    except (AttributeError, TypeError, ValueError):
        problem("Invalid observation time")
    messages = value.get("messages")
    if not isinstance(messages, list) or len(messages) > 100:
        problem("Invalid message window")
    safe, ids = [], set()
    for m in messages:
        if not isinstance(m, dict) or m.get("direction") not in ("incoming", "outgoing"):
            problem("Invalid message")
        ident = opaque(m.get("id"))
        if ident in ids:
            problem("Duplicate message")
        ids.add(ident)
        safe.append({"id": ident, "body": clean(m.get("body") or "[Attachment]", 20000),
            "direction": m["direction"], "name": clean(m.get("name") or "", 500),
            "occurred_at": clean(m.get("occurred_at"), 100), "revision": clean(m.get("revision") or "", 100)})
    digest = hashlib.sha256(thread_id.encode()).hexdigest()
    values = {"account_key": account.key, "thread_sha256": digest, "thread_id": thread_id,
        "owner_user_id": user.id, "binding_sha256": account.binding, "participant_id": participant_id,
        "name": clean(value.get("name") or "Unnamed contact", 500), "preview": clean(value.get("preview") or "", 20000),
        "provider_updated_at": clean(value.get("updated_at") or "", 100), "messages": safe,
        "has_more": value.get("has_more") is True, "seen_at": observed_at}
    db.execute(insert(InboxThread).values(**values).on_conflict_do_update(
        index_elements=[InboxThread.account_key, InboxThread.thread_sha256], set_=values,
        where=InboxThread.seen_at <= observed_at))
    db.commit()
    return {"ok": True, "thread_sha256": digest}


def pending(db):
    user = owner(db)
    keys = [a.key for a in inbox.accounts(db) if a.provider == "local" and a.owner_email == user.email]
    result = db.scalars(select(InboxReply.id).where(InboxReply.account_key.in_(keys),
        InboxReply.user_id == user.id, InboxReply.state == "queued").order_by(InboxReply.created_at).limit(20)).all()
    return {"reply_ids": list(result)}


def begin(db, reply_id):
    row = db.scalar(select(InboxReply).where(InboxReply.id == reply_id).with_for_update())
    if row is None:
        problem("Unknown reply", 404)
    account, user = enabled_account(db, row.account_key)
    if row.user_id != user.id or row.binding_sha256 != account.binding:
        problem("Reply account changed", 409)
    if row.state != "queued":
        return {"state": row.state, "dispatch": False}
    if (utcnow() - row.created_at).total_seconds() > 900:
        row.state, row.reason, row.settled_at = "rejected", "expired", utcnow()
        db.commit()
        return {"state": "rejected", "dispatch": False}
    row.state = "calling"
    audit(db, "desktop:personal-inbox", "inbox.reply.dispatch", "inbox-reply:" + row.id,
          after={"account": row.account_key, "thread_sha256": row.thread_sha256})
    db.commit()
    return {"dispatch": True, "reply_id": row.id, "account_key": account.key, "account_id": account.account_id,
            "user_id": account.user_id, "channel": account.channel, "thread_id": row.thread_id, "body": row.body}


def settle(db, reply_id, data):
    row = db.scalar(select(InboxReply).where(InboxReply.id == reply_id).with_for_update())
    if row is None:
        problem("Unknown reply", 404)
    account, user = enabled_account(db, row.account_key)
    if row.user_id != user.id or row.binding_sha256 != account.binding:
        problem("Reply account changed", 409)
    state = data.get("state")
    if state not in ("bridge_accepted", "accepted", "rejected", "outcome_unknown"):
        problem("Invalid reply outcome")
    provider_id = data.get("provider_message_id")
    if state in ("bridge_accepted", "accepted"):
        provider_id = opaque(provider_id)
    elif provider_id is not None:
        provider_id = opaque(provider_id)
    if row.state not in ("calling", "bridge_accepted"):
        if row.state != state or row.provider_message_id != provider_id:
            problem("Reply outcome conflict", 409)
        return {"state": row.state}
    if row.provider_message_id and row.provider_message_id != provider_id:
        # The desktop API first acknowledges a temporary ID, then resolves it to
        # the original channel's message ID. Only that bound transition is valid.
        if (row.state != "bridge_accepted" or state not in ("accepted", "rejected")
                or data.get("previous_message_id") != row.provider_message_id or not provider_id):
            problem("Reply message identity changed", 409)
    row.state, row.provider_message_id, row.settled_at = state, provider_id, utcnow()
    audit(db, "desktop:personal-inbox", "inbox.reply." + state, "inbox-reply:" + row.id,
          after={"account": account.key, "state": state})
    db.commit()
    return {"state": state}
