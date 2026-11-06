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
from ..models import InboxReply, utcnow
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


def accounts():
    return load_accounts(config.GHQ_INBOX_ACCOUNTS, os.environ)


def signer():
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-inbox-thread-v1")


def ticket(account, thread_id, user_id, name=""):
    return signer().dumps({"account": account.key, "binding": account.binding, "thread": opaque(thread_id),
                           "user": user_id, "name": str(name)[:500]})


def resolve(value, user_id):
    try:
        bound = signer().loads(value, max_age=7200)
        account = next((a for a in accounts() if a.key == bound["account"]), None)
        if (account is None or bound.get("user") != user_id or account.binding != bound.get("binding")):
            raise ValueError()
        opaque(bound["thread"])
        return account, bound
    except (BadData, ValueError, TypeError, KeyError, InboxError):
        raise HTTPException(409, "Refresh the inbox before continuing.") from None


def snapshot(user_id, selected, brand="", channel=""):
    allowed = accounts()
    if set(selected) - {a.key for a in allowed} or (brand and brand not in BRANDS) or (channel and channel not in CHANNELS):
        raise HTTPException(400, "Unknown inbox filter")
    chosen = [a for a in allowed if (not selected or a.key in selected)
              and (not brand or a.brand == brand) and (not channel or a.channel == channel)]

    def read(account):
        try:
            conversations, has_more = client(account).conversations()
            seen, result = set(), []
            for row in conversations:
                if row["id"] in seen:
                    continue
                seen.add(row["id"])
                result.append({**row, "account": account, "ticket": ticket(account, row["id"], user_id, row["name"])})
            return {"account": account, "threads": result, "has_more": has_more, "error": None}
        except InboxError as error:
            return {"account": account, "threads": [], "has_more": False, "error": MESSAGES.get(error.code, "Connection unavailable.")}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(read, chosen))
    threads = sorted((t for r in results for t in r["threads"]), key=lambda t: t["updated_at"], reverse=True)
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
    digest = hashlib.sha256(json.dumps([user.id, account.binding, thread_id, text], ensure_ascii=False).encode()).hexdigest()
    created = db.execute(insert(InboxReply).values(id=request_id, user_id=user.id, account_key=account.key,
        binding_sha256=account.binding, thread_sha256=thread_sha, request_sha256=digest,
        state="calling", created_at=utcnow()).on_conflict_do_nothing(index_elements=[InboxReply.id])
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
