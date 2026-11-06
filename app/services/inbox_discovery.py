"""Owner-controlled native conversation discovery; metadata is not a CRM grant."""
import hashlib
from fastapi import HTTPException
from sqlalchemy.dialects.postgresql import insert
from ..models import AppSetting, InboxThread, utcnow
from . import personal_inbox, inbox
from .inbox_providers import clean, opaque


def registry_key(user_id, account):
    return "inbox.discovery." + str(user_id) + "." + account.identity


def read(db, user_id, account):
    row = db.get(AppSetting, registry_key(user_id, account))
    return row.value if row else {"candidates": [], "request": None, "coverage": None}


def write(db, user_id, account, value, actor):
    key = registry_key(user_id, account)
    db.execute(insert(AppSetting).values(key=key, value=value, updated_by=actor, updated_at=utcnow())
        .on_conflict_do_update(index_elements=[AppSetting.key], set_={"value": value, "updated_by": actor, "updated_at": utcnow()}))
    db.commit()


def bound(db, data):
    account, user = personal_inbox.enabled_account(db, data.get("account_key"))
    if (personal_inbox.connector(data) != "native-messenger" or personal_inbox.connector_for(db, account.key) != "native-messenger"
            or data.get("account_id") != account.account_id or data.get("user_id") != account.user_id):
        raise HTTPException(403, "Wrong native discovery account.")
    return account, user


def report(db, data):
    account, user = bound(db, data)
    rows = data.get("candidates")
    if not isinstance(rows, list) or len(rows) > 100:
        raise HTTPException(400, "Invalid discovery window.")
    saved = read(db, user.id, account)
    previous = {r["threadId"]: r for r in saved["candidates"]}
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(400, "Invalid native candidate.")
        thread_id, participant_id = opaque(row.get("threadId")), row.get("participantId") or ""
        if not thread_id.isascii() or not thread_id.isdecimal() or not isinstance(participant_id, str) or (participant_id and (not participant_id.isascii() or not participant_id.isdecimal())) or thread_id in seen:
            raise HTTPException(400, "Invalid native candidate identity.")
        seen.add(thread_id)
        old = previous.get(thread_id)
        if old and old["participantId"] and participant_id and old["participantId"] != participant_id:
            raise HTTPException(409, "Candidate participant changed.")
        cached = db.get(InboxThread, (account.key, hashlib.sha256(thread_id.encode()).hexdigest()))
        inherited = bool(cached and cached.owner_user_id == user.id and cached.binding_sha256 == account.binding
                         and cached.participant_id == participant_id)
        previous[thread_id] = {"threadId": thread_id, "participantId": participant_id or (old or {}).get("participantId", ""),
            "name": clean(row.get("name"), 500), "textOnly": row.get("textOnly") is True,
            "selected": old["selected"] if old else inherited,
            "reason": clean(row.get("reason") or "", 200), "query": clean(data.get("query") or "", 100)}
    if len(previous) > 200:
        raise HTTPException(400, "Review your existing discovered conversations before adding more.")
    saved["candidates"] = list(previous.values())
    if data.get("seed_existing") is True:
        saved["seeded"] = True
    saved["coverage"] = {"query": clean(data.get("query") or "", 100), "count": len(rows),
        "complete": data.get("complete") is True, "at": utcnow().isoformat(), "note": clean(data.get("note") or "", 300)}
    if saved.get("request") and data.get("request_id") == saved["request"]["id"]:
        saved["request"]["done"] = True
    write(db, user.id, account, saved, "desktop:native-discovery")
    return {"ok": True, "count": len(rows)}


def selections(db, data):
    account, user = bound(db, data)
    saved = read(db, user.id, account)
    return {"initialised": saved.get("seeded") is True,
            "threads": [r for r in saved["candidates"] if r["selected"] and r["participantId"] and not r["reason"]],
            "request": saved.get("request")}


def permits_thread(db, user_id, account, thread_id, participant_id):
    row = db.get(AppSetting, registry_key(user_id, account))
    # Compatible cutover: the existing client keeps its named scope until the
    # new client seeds the registry from already imported conversations.
    if row is None:
        return True
    return any(r["selected"] and not r["reason"] and r["threadId"] == thread_id
               and r["participantId"] == participant_id for r in row.value["candidates"])


def owner_account(db, user, key):
    if user.email != personal_inbox.config.GHQ_PERSONAL_INBOX_OWNER_EMAIL:
        raise HTTPException(403, "These conversations belong to their account owner.")
    account = next((a for a in inbox.accounts(db) if a.key == key and a.owner_email == user.email), None)
    if account is None or personal_inbox.connector_for(db, key) != "native-messenger":
        raise HTTPException(400, "Choose your enabled native Messenger connection.")
    return account
