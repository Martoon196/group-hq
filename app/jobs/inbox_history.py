"""Incremental read-only Zernio history, with Postgres checkpoints across service restarts."""
import logging
import threading

from fastapi import HTTPException
from sqlalchemy import text

from ..db import SessionLocal, engine
from ..services import inbox, personal_inbox
from ..services.inbox_archive import bind_thread, insert_messages
from ..services.inbox_providers import client, InboxError

LOG = logging.getLogger(__name__)


def check_cursor(previous, following, more):
    if more and (not following or following == previous):
        raise InboxError("stalled_history_cursor")


def account_page(db, owner, account, api):
    keys = {"owner": owner.id, "scope": account.identity}
    db.execute(text("""INSERT INTO inbox_history_accounts(owner_id,scope) VALUES (:owner,:scope)
                       ON CONFLICT DO NOTHING"""), keys)
    row = db.execute(text("""SELECT cursor FROM inbox_history_accounts WHERE owner_id=:owner
                             AND scope=:scope AND next_attempt<=now() FOR UPDATE"""), keys).first()
    if row is None:
        db.commit()
        return
    try:
        items, more, following = api.conversation_page(row.cursor)
        check_cursor(row.cursor, following, more)
        for item in items:
            bound = bind_thread(db, owner, account, item)
            db.execute(text("""UPDATE inbox_history_threads SET provider_updated=:stamp
                      WHERE owner_id=:owner AND scope=:scope AND thread_key=:thread"""),
                       {**bound, "stamp": item.get("updated_at") or ""})
        db.execute(text("""UPDATE inbox_history_accounts SET cursor=:cursor,error=NULL,
                    next_attempt=now()+(:seconds * interval '1 second') WHERE owner_id=:owner AND scope=:scope"""),
                   {**keys, "cursor": following if more else None, "seconds": 15 if more else 900})
    except (InboxError, HTTPException) as exc:
        db.rollback()
        db.execute(text("""INSERT INTO inbox_history_accounts(owner_id,scope,error,next_attempt)
                   VALUES(:owner,:scope,:error,now()+interval '10 minutes') ON CONFLICT(owner_id,scope)
                   DO UPDATE SET error=excluded.error,next_attempt=excluded.next_attempt"""),
                   {**keys, "error": getattr(exc, "code", "history_verification_failed")})
    db.commit()


def message_page(db, owner, account, api):
    keys = {"owner": owner.id, "scope": account.identity}
    row = db.execute(text("""SELECT thread_key,native_id,participant,older_cursor,reached_start,incremental,
             provider_updated,scanned_version FROM inbox_history_threads
             WHERE owner_id=:owner AND scope=:scope AND (NOT reached_start OR incremental OR provider_updated IS DISTINCT FROM scanned_version)
             AND (last_attempt IS NULL OR last_attempt<now()-(CASE WHEN error IS NULL THEN interval '5 seconds'
                ELSE interval '10 minutes' END))
             ORDER BY last_attempt ASC NULLS FIRST,thread_key LIMIT 1 FOR UPDATE SKIP LOCKED"""), keys).first()
    if row is None:
        db.commit()
        return False
    keys["thread"] = row.thread_key
    try:
        if account.provider == "r72_whatsapp":
            messages, following = api.messages(row.native_id, row.participant, row.older_cursor)
        else:
            messages, following = api.messages(row.native_id, row.older_cursor)
        check_cursor(row.older_cursor, following, following is not None)
        incremental = row.reached_start
        ids = [m["id"] for m in messages]
        overlap = incremental and ids and db.scalar(text("""SELECT EXISTS (SELECT 1 FROM inbox_history_messages
            WHERE owner_id=:owner AND scope=:scope AND thread_key=:thread AND native_id=ANY(CAST(:ids AS text[])))"""), {**keys, "ids": ids})
        done = not following or bool(overlap)
        version = row.provider_updated if row.older_cursor is None else row.scanned_version
        insert_messages(db, keys, messages)
        db.execute(text("""UPDATE inbox_history_threads SET older_cursor=:cursor,reached_start=reached_start OR :done,
                    scanned_version=:version,incremental=:incremental,
                    last_attempt=now(),last_seen=now(),error=NULL WHERE owner_id=:owner AND scope=:scope AND thread_key=:thread"""),
                   {**keys, "cursor": None if done else following, "done": done, "version": version,
                    "incremental": bool(incremental and not done)})
    except (InboxError, HTTPException) as exc:
        db.rollback()
        db.execute(text("""UPDATE inbox_history_threads SET error=:error,last_attempt=now()
                    WHERE owner_id=:owner AND scope=:scope AND thread_key=:thread"""),
                   {**keys, "error": getattr(exc, "code", "history_verification_failed")})
    db.commit()
    return True


def run_cycle(stop):
    with engine.connect() as lease:
        if not lease.scalar(text("SELECT pg_try_advisory_xact_lock(1248917768)")):
            return
        with SessionLocal() as db:
            owner = personal_inbox.owner(db)
            for account in inbox.accounts(db):
                if stop.is_set():
                    return
                if account.provider not in ("zernio","r72_whatsapp") or not account.configured or (account.owner_email and account.owner_email != owner.email):
                    continue
                if account.provider == "r72_whatsapp":
                    from ..services.inbox_r72_whatsapp import WhatsAppReader
                    api = WhatsAppReader(account, owner.email)
                else:
                    api = client(account)
                account_page(db, owner, account, api)
                for _ in range(2):
                    if stop.is_set() or not message_page(db, owner, account, api):
                        break


def start():
    stop = threading.Event()
    def poll():
        while not stop.wait(30):
            try:
                run_cycle(stop)
            except Exception as exc:
                LOG.warning("Inbox history read deferred (%s)", type(exc).__name__)
    threading.Thread(target=poll, name="inbox-history", daemon=True).start()
    return stop
