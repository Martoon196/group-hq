"""Owner-controlled native conversation discovery; metadata is not a CRM grant."""
import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from ..models import AppSetting, InboxThread, utcnow
from . import personal_inbox, inbox
from .inbox_providers import clean, opaque

MAX_CANDIDATES = 2000


def selected_candidate(saved, row):
    return bool((row["selected"] or saved.get("sync_all") is True)
                and row["participantId"] and not row["reason"])


def registry_key(user_id, account):
    return "inbox.disc.v1." + hashlib.sha256(json.dumps([user_id, account.identity]).encode()).hexdigest()


def read(db, user_id, account):
    row = db.get(AppSetting, registry_key(user_id, account))
    return row.value if row else {"candidates": [], "request": None, "coverage": None}


def cached_matches(db, user_id, account, candidates, query, sync_all=False):
    """Search selected, identity-bound inbox text. Return only names and thread IDs."""
    selected = {r["threadId"]: r["participantId"] for r in candidates
                if selected_candidate({"sync_all": sync_all}, r)}
    if not selected:
        return []
    rows = db.execute(text(r"""
        SELECT t.thread_id, t.participant_id, t.name FROM inbox_threads AS t
        WHERE t.account_key = :account AND t.binding_sha256 = :binding AND t.owner_user_id = :owner
          AND t.thread_id = ANY(CAST(:ids AS text[]))
          AND EXISTS (
            SELECT 1 FROM jsonb_array_elements(t.messages) AS m
            WHERE m->>'direction' IN ('incoming', 'outgoing')
              AND strpos(lower(regexp_replace(m->>'body', '\s+', ' ', 'g')), lower(:phrase)) > 0
          )
        ORDER BY t.name, t.thread_id
    """), {"account": account.key, "binding": account.binding, "owner": user_id, "ids": list(selected),
           "phrase": " ".join(query.split())})
    return [{"threadId": row.thread_id, "name": row.name} for row in rows
            if selected.get(row.thread_id) == row.participant_id]


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
    request = saved.get("request")
    if data.get("request_id") and (not request or data["request_id"] != request["id"] or request["done"]):
        return {"ok": True, "count": 0}
    previous = {r["threadId"]: r for r in saved["candidates"]}
    seen = set()
    matches = []
    for row in rows:
        if not isinstance(row, dict):
            raise HTTPException(400, "Invalid native candidate.")
        thread_id, participant_id = opaque(row.get("threadId")), row.get("participantId") or ""
        if not thread_id.isascii() or not thread_id.isdecimal() or not isinstance(participant_id, str) or (participant_id and (not participant_id.isascii() or not participant_id.isdecimal())) or thread_id in seen:
            raise HTTPException(400, "Invalid native candidate identity.")
        seen.add(thread_id)
        old = previous.get(thread_id)
        thread_kind = row.get("threadKind", "e2ee")
        if thread_kind not in ("e2ee", "page") or (thread_kind == "page" and participant_id and participant_id != thread_id):
            raise HTTPException(400, "Unsupported native conversation kind.")
        if old and old.get("threadKind", "e2ee") != thread_kind:
            raise HTTPException(409, "Candidate conversation kind changed.")
        if old and old["participantId"] and participant_id and old["participantId"] != participant_id:
            raise HTTPException(409, "Candidate participant changed.")
        cached = db.get(InboxThread, (account.key, hashlib.sha256(thread_id.encode()).hexdigest()))
        inherited = bool(cached and cached.owner_user_id == user.id and cached.binding_sha256 == account.binding
                         and cached.participant_id == participant_id)
        if request and request["id"] == data.get("request_id") and participant_id and not row.get("reason"):
            status = "Already imported" if inherited else "Previously discovered" if old else "New candidate"
            matches.append({"name": clean(row.get("name"), 500), "status": status, "threadId": thread_id})
        previous[thread_id] = {"threadId": thread_id, "participantId": participant_id or (old or {}).get("participantId", ""),
            "name": clean(row.get("name"), 500), "textOnly": row.get("textOnly") is True, "threadKind": thread_kind,
            "selected": old["selected"] if old else inherited,
            "reason": clean(row.get("reason") or "", 200), "query": clean(data.get("query") or "", 100)}
    if len(previous) > MAX_CANDIDATES:
        raise HTTPException(400, "Review your existing discovered conversations before adding more.")
    saved["candidates"] = list(previous.values())
    if data.get("seed_existing") is True:
        saved["seeded"] = True
    saved["coverage"] = {"query": clean(data.get("query") or "", 100), "count": len(rows),
        "complete": data.get("complete") is True, "at": utcnow().isoformat(), "note": clean(data.get("note") or "", 300)}
    if saved.get("request") and data.get("request_id") == saved["request"]["id"]:
        saved["coverage"]["matches"] = matches
        saved["request"]["done"] = True
    write(db, user.id, account, saved, "desktop:native-discovery")
    return {"ok": True, "count": len(rows)}


def selections(db, data):
    account, user = bound(db, data)
    saved = read(db, user.id, account)
    request = saved.get("request")
    return {"initialised": saved.get("seeded") is True,
            "sync_all": saved.get("sync_all") is True,
            "threads": [r for r in saved["candidates"] if selected_candidate(saved, r)],
            "request": {k: request[k] for k in ("id", "query", "done")} if request else None}


def permits_thread(db, user_id, account, thread_id, participant_id):
    row = db.get(AppSetting, registry_key(user_id, account))
    # Compatible cutover: the existing client keeps its named scope until the
    # new client seeds the registry from already imported conversations.
    if row is None:
        return True
    return any(selected_candidate(row.value, r) and r["threadId"] == thread_id
               and r["participantId"] == participant_id for r in row.value["candidates"])


def owner_account(db, user, key):
    if user.email != personal_inbox.config.GHQ_PERSONAL_INBOX_OWNER_EMAIL:
        raise HTTPException(403, "These conversations belong to their account owner.")
    account = next((a for a in inbox.accounts(db) if a.key == key and a.owner_email == user.email), None)
    if account is None or personal_inbox.connector_for(db, key) != "native-messenger":
        raise HTTPException(400, "Choose your enabled native Messenger connection.")
    return account
