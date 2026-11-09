"""Durable message history. Importing old messages never dispatches a reply or CRM event."""
import hashlib
import json
import re
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import text

from .inbox_history_registration import ensure_thread
from .inbox_providers import clean, opaque


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def timestamp(value):
    try:
        instant = datetime.fromisoformat(clean(value, 100).replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError()
        return instant.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, "Invalid history timestamp.") from None


def bind_thread(db, user, account, thread):
    ident, peer = opaque(thread.get("id")), opaque(thread.get("participant_id"))
    keys = {"owner": user.id, "scope": account.identity, "thread": digest(ident)}
    bound = db.execute(text("""
        INSERT INTO inbox_history_threads(owner_id,scope,thread_key,native_id,participant,name)
        VALUES (:owner,:scope,:thread,:id,:peer,:name)
        ON CONFLICT (owner_id,scope,thread_key) DO UPDATE SET name=excluded.name
        WHERE inbox_history_threads.native_id=excluded.native_id AND inbox_history_threads.participant=excluded.participant
        RETURNING thread_key
    """), {**keys, "id": ident, "peer": peer, "name": clean(thread.get("name") or "Unnamed contact", 500)}).scalar()
    if not bound:
        raise HTTPException(409, "Conversation participant changed.")
    return keys


def insert_messages(db, keys, messages):
    records = []
    ids = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("direction") not in ("incoming", "outgoing"):
            raise HTTPException(400, "Invalid message direction.")
        ident = opaque(message.get("id"))
        if ident in ids:
            raise HTTPException(400, "Duplicate message in history page.")
        ids.add(ident)
        records.append({**keys, "id": ident, "key": digest(ident), "at": timestamp(message.get("occurred_at")),
                        "direction": message["direction"], "body": clean(message.get("body")),
                        "name": clean(message.get("name") or "", 500), "revision": clean(message.get("revision") or "", 100)})
    if records:
        db.execute(text("""
            INSERT INTO inbox_history_messages(owner_id,scope,thread_key,message_key,native_id,sent_at,direction,body,name,revision)
            VALUES (:owner,:scope,:thread,:key,:id,:at,:direction,:body,:name,:revision)
            ON CONFLICT (owner_id,scope,thread_key,message_key) DO NOTHING
        """), records)


def store_history(db, user, account, data):
    chunk = data.get("chunk_id")
    if not isinstance(chunk, str) or not re.fullmatch("[0-9a-f]{64}", chunk) or type(data.get("reached_start")) is not bool:
        raise HTTPException(400, "Invalid history receipt.")
    if abs((datetime.now(timezone.utc) - timestamp(data.get("observed_at"))).total_seconds()) > 300:
        raise HTTPException(400, "Refresh the history observation.")
    thread = data.get("thread")
    ensure_thread(db, user, account, thread)
    messages = thread.get("messages")
    if not isinstance(messages, list) or len(messages) > 200 or any(not isinstance(m, dict) for m in messages):
        raise HTTPException(400, "Invalid history packet.")
    if data.get("oldest_id") != (messages[0].get("id") if messages else None) or data.get("newest_id") != (messages[-1].get("id") if messages else None):
        raise HTTPException(400, "History page boundaries changed.")
    dates = [timestamp(m.get("occurred_at")) for m in messages]
    if dates != sorted(dates):
        raise HTTPException(400, "History page is out of order.")
    keys = bind_thread(db, user, account, thread)
    evidence = data.get("evidence") or thread.get("evidence") or {}
    if not isinstance(evidence, dict):
        raise HTTPException(400, "Invalid history evidence.")
    omitted = evidence.get("omitted_rows", 0)
    reached = data["reached_start"]
    fingerprint = digest(json.dumps([thread["participant_id"], messages, reached, omitted], sort_keys=True, separators=(",", ":")))
    receipt = db.execute(text("""
        SELECT content_hash,stored FROM inbox_history_chunks
        WHERE owner_id=:owner AND scope=:scope AND thread_key=:thread AND chunk_id=:chunk
    """), {**keys, "chunk": chunk}).first()
    if receipt and receipt.content_hash != fingerprint:
        raise HTTPException(409, "History receipt conflicts with an earlier packet.")
    if not receipt:
        insert_messages(db, keys, messages)
        db.execute(text("""
            INSERT INTO inbox_history_chunks(owner_id,scope,thread_key,chunk_id,content_hash,stored)
            VALUES (:owner,:scope,:thread,:chunk,:fingerprint,:count)
        """), {**keys, "chunk": chunk, "fingerprint": fingerprint, "count": len(messages)})
        db.execute(text("""
            UPDATE inbox_history_threads SET reached_start=reached_start OR :reached,
                incomplete=incomplete OR :omitted, last_seen=now()
            WHERE owner_id=:owner AND scope=:scope AND thread_key=:thread
        """), {**keys, "reached": reached, "omitted": bool(omitted)})
    db.commit()
    return {"ok": True, "chunk_id": chunk, "stored": receipt.stored if receipt else len(messages), "reached_start": reached}
