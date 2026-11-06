"""Owner inbox orchestration, using existing auth, audit and Postgres conventions."""
import hashlib
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .. import config
from ..models import AppSetting, InboxReply, InboxThread, utcnow
from .audit import audit
from .inbox_providers import BRANDS, CHANNELS, InboxError, client, load_accounts, opaque

PAGE = "/inbox"
MESSAGES = {
    "setup_needed": "Connection needs setting up.",
    "sign_in_needed": "Please reconnect this account.",
    "permission_needed": "This connection needs inbox permission.",
    "rate_limited": "The channel is rate limited. Please wait before refreshing.",
    "provider_unavailable": "This channel could not be reached. Other accounts are still available.",
    "invalid_provider_response": "The channel returned an unreadable response.",
    "unbound_target": "The account or conversation no longer matches. Refresh before continuing.",
    "provider_rejected": "The channel refused this request. Check the conversation in its original app.",
    "encrypted_room_client_needed": "This bridge room needs an encryption-capable connection.",
    "outcome_unknown": "The reply's outcome is unknown. Check the original conversation before sending anything again.",
    "reply_not_enabled": "Replies have not been enabled for this connection.",
}


def accounts(db=None):
    result = list(load_accounts(config.GHQ_INBOX_ACCOUNTS, os.environ))
    if db is not None:
        from dataclasses import replace
        for index, account in enumerate(result):
            setting = db.get(AppSetting, "inbox.account.brand." + account.identity)
            if setting and setting.value.get("brand") in BRANDS:
                result[index] = replace(account, brand=setting.value["brand"])
    if db is not None:
        saved = db.get(AppSetting, "personal_inbox.accounts")
        local = (saved.value if saved else {}).get("accounts", [])
        enabled = [r for r in local if r.get("enabled") is True]
        result.extend(load_accounts(json.dumps(enabled), os.environ))
    keys = [a.key for a in result]
    if len(set(keys)) != len(keys):
        raise InboxError("invalid_account_configuration")
    return tuple(result)


def signer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-inbox-thread-v1")


def ticket(account, thread_id, user_id, name="", participant_id=None):
    return signer().dumps({"account": account.key, "binding": account.binding, "thread": opaque(thread_id),
                           "user": user_id, "name": str(name)[:500], "participant": participant_id})


def resolve(value, user_id, user_email="", db=None):
    try:
        bound = signer().loads(value, max_age=7200)
        account = next((a for a in accounts(db) if a.key == bound["account"]), None)
        if (account is None or bound.get("user") != user_id or account.binding != bound.get("binding")):
            raise ValueError()
        if account.owner_email and account.owner_email != user_email.lower():
            raise ValueError()
        opaque(bound["thread"])
        return account, bound
    except (BadData, ValueError, TypeError, KeyError, InboxError):
        raise HTTPException(409, "Refresh the inbox before continuing.") from None


def snapshot(user_id, selected, brand="", channel="", *, db=None, user_email="", kind=""):
    allowed = tuple(a for a in accounts(db) if not a.owner_email or a.owner_email == user_email.lower())
    if set(selected) - {a.key for a in allowed} or (brand and brand not in BRANDS) or (channel and channel not in CHANNELS):
        raise HTTPException(400, "Unknown inbox filter")
    from . import inbox_organisation, inbox_crm
    if kind and kind not in inbox_organisation.KINDS:
        raise HTTPException(400, "Choose a conversation type.")
    chosen = [a for a in allowed if (not selected or a.key in selected)
              and (not channel or a.channel == channel)]

    local = {}
    if db is not None:
        for account in chosen:
            if account.provider == "local":
                saved = db.execute(select(InboxThread.thread_id, InboxThread.name, InboxThread.participant_id,
                    InboxThread.preview, InboxThread.provider_updated_at, InboxThread.seen_at).where(InboxThread.account_key == account.key,
                    InboxThread.owner_user_id == user_id, InboxThread.binding_sha256 == account.binding)
                    .order_by(InboxThread.provider_updated_at.desc()).limit(101)).all()
                local[account.key] = [{"id": r.thread_id, "name": r.name, "participant_id": r.participant_id,
                    "preview": r.preview, "updated_at": r.provider_updated_at, "unread": None, "seen_at": r.seen_at} for r in saved]

    def read(account):
        try:
            if not account.configured:
                raise InboxError("setup_needed")
            if account.provider == "local":
                conversations = local.get(account.key, [])
                conversations, has_more = conversations[:100], len(conversations) > 100
            else:
                conversations, has_more = client(account).conversations()
            seen, result = set(), []
            for row in conversations:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                result.append({**row, "account": account, "ticket": ticket(account, row["id"], user_id, row["name"], row["participant_id"])})
            return {"account": account, "threads": result, "has_more": has_more, "error": None}
        except InboxError as error:
            return {"account": account, "threads": [], "has_more": False, "error": MESSAGES.get(error.code, "Connection unavailable.")}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(read, chosen))
    threads = []
    for result in results:
        for thread in result["threads"]:
            linked = inbox_crm.links(db, thread["account"], thread["id"]) if db is not None else ()
            settings = inbox_organisation.choices(db, user_id, thread["account"], thread["id"], linked)
            thread["organisation"] = settings
            if kind and settings["kind"] != kind:
                continue
            if brand and settings["kind"] == "personal":
                continue
            if brand and brand not in settings["brands"]:
                # Official business inboxes retain their connection default;
                # personal accounts need an explicit conversation association.
                if settings["kind"] == "personal" or thread["account"].owner_email or thread["account"].brand != brand:
                    continue
            threads.append(thread)
    threads.sort(key=lambda t: t["updated_at"], reverse=True)
    return allowed, results, threads


def replies(db, account, thread_id):
    digest = hashlib.sha256(thread_id.encode()).hexdigest()
    return db.scalars(select(InboxReply).where(InboxReply.account_key == account.key,
        InboxReply.thread_sha256 == digest).order_by(InboxReply.created_at.desc()).limit(10)).all()


def send(db, user, account, thread_id, text, request_id):
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Invalid reply request") from None
    if not isinstance(text, str) or not text.strip() or len(text.encode()) > 10000:
        raise HTTPException(400, "Write a reply of up to 10,000 bytes.")
    if not account.can_reply or not account.configured:
        raise HTTPException(409, "Replies are not configured for this account.")
    thread_sha = hashlib.sha256(opaque(thread_id).encode()).hexdigest()
    if account.provider == "local":
        cached = local_thread(db, account, thread_id, user.id)
        if (utcnow() - cached.seen_at).total_seconds() > 120:
            raise HTTPException(409, "Your personal connector is offline or catching up. Open it before replying.")
    digest = hashlib.sha256(json.dumps([user.id, account.binding, thread_id, text], ensure_ascii=False).encode()).hexdigest()
    created = db.execute(insert(InboxReply).values(id=request_id, user_id=user.id, account_key=account.key,
        binding_sha256=account.binding, thread_sha256=thread_sha, request_sha256=digest,
        state="queued" if account.provider == "local" else "calling", created_at=utcnow(),
        thread_id=thread_id if account.provider == "local" else None,
        body=text if account.provider == "local" else None).on_conflict_do_nothing(index_elements=[InboxReply.id])
        .returning(InboxReply.id)).scalar_one_or_none()
    if created is None:
        existing = db.get(InboxReply, request_id)
        if existing.user_id != user.id or existing.request_sha256 != digest:
            db.rollback()
            raise HTTPException(409, "This reply request was already used with different content or an account.")
        db.rollback()
        return existing
    audit(db, user, "inbox.reply.begin", "inbox-reply:" + request_id,
          after={"account": account.key, "thread_sha256": thread_sha, "request_sha256": digest})
    db.commit()  # Durable before provider IO. A lost response/commit must never repeat the send.
    if account.provider == "local":
        return db.get(InboxReply, request_id)
    state, reason, provider_id = "outcome_unknown", None, None
    try:
        provider_id = client(account).send(thread_id, text, "ghq-" + request_id)
        state = "bridge_accepted" if account.provider == "matrix" else "accepted"
    except InboxError as error:
        reason = error.code
        state = "outcome_unknown" if error.code == "outcome_unknown" else "rejected"
    receipt = db.get(InboxReply, request_id)
    receipt.state, receipt.reason, receipt.provider_message_id = state, reason, provider_id
    receipt.settled_at = utcnow()
    audit(db, user, "inbox.reply." + state, "inbox-reply:" + request_id,
          after={"account": account.key, "state": state, "reason": reason})
    db.commit()
    return receipt


def local_thread(db, account, thread_id, user_id):
    row = db.get(InboxThread, (account.key, hashlib.sha256(thread_id.encode()).hexdigest()))
    if row is None or row.owner_user_id != user_id or row.binding_sha256 != account.binding:
        raise HTTPException(409, "Refresh your personal inbox before continuing.")
    return row
