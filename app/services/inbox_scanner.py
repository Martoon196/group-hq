"""Owner-enabled triage of changed conversations. Classification cannot create a contact or send a message."""
import hashlib
import json
import logging
import threading
from datetime import timedelta

import httpx
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from .. import config
from ..db import SessionLocal, engine
from ..models import AppSetting, User, utcnow
from . import inbox, inbox_claude, inbox_triage
from .inbox_providers import client, InboxError

LABELS = {"business": "Business for me", "property": "Property offered to me", "pitch": "Selling to me",
          "personal": "Friends & personal", "unclassified": "Unclear"}
FILTERS = {**LABELS, "handoff": "Needs me", "unscanned": "Awaiting scan"}
SCAN_VERSION = "20261109-2"      # -2: the property category; every conversation is looked at again
LOG = logging.getLogger(__name__)


def setting_key(user_id):
    return "inbox.scan.owner." + str(user_id)


def digest(fields):
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def result_key(user_id, account, thread_id):
    return "inbox.scan.v1." + digest([user_id, account.binding, thread_id])


def save_value(db, key, value, actor):
    values = dict(value=value, updated_by=actor, updated_at=utcnow())
    db.execute(insert(AppSetting).values(key=key, **values).on_conflict_do_update(index_elements=[AppSetting.key], set_=values))


def enabled(db, user):
    row = db.get(AppSetting, setting_key(user.id))
    return bool(row and row.value.get("enabled")) and user.email.lower() == config.GHQ_PERSONAL_INBOX_OWNER_EMAIL


def account_hint(account):
    return None if account.owner_email else account.brand


def message_stamp(messages):
    return digest([(m.get("id"), m.get("direction"), m.get("body", "")) for m in messages[-12:]])


def stamp(thread):
    return digest([SCAN_VERSION, inbox_claude.model() if inbox_claude.configured() else "local",
                   thread["updated_at"], thread.get("preview", "")])


def read_results(db, user_id, threads):
    keys = [result_key(user_id, t["account"], t["id"]) for t in threads]
    if not keys:
        return {}
    return {row.key: row.value for row in db.scalars(select(AppSetting).where(AppSetting.key.in_(keys)))}


def effective_kind(result, organisation, owner_choice=False):
    if owner_choice or organisation["kind"] != "unclassified":
        return organisation["kind"]
    return result.get("kind", "")


def scan_cycle(db, user, budget=16):
    _, _, threads = inbox.snapshot(user.id, [], db=db, user_email=user.email)
    saved = read_results(db, user.id, threads)
    attempted = 0
    for t in threads:
        account, thread_id = t["account"], t["id"]
        key = result_key(user.id, account, thread_id)
        version = stamp(t)
        old = saved.get(key, {})
        if old.get("stamp") == version and (not old.get("retry_at") or old["retry_at"] > utcnow().isoformat()):
            continue
        if attempted >= budget or not enabled(db, user):
            break
        attempted += 1
        try:
            if account.provider == "local":
                messages = inbox.local_thread(db, account, thread_id, user.id).messages
            elif account.provider == "r72_whatsapp":
                from .inbox_r72_whatsapp import WhatsAppReader
                messages, _ = WhatsAppReader(account, user.email).messages(thread_id, t["participant_id"])
            else:
                messages, _ = client(account).messages(thread_id)
        except InboxError:
            # A failing provider must leave room for the next conversation on the next pass.
            save_value(db, key, {**old, "stamp": version, "retry_at": (utcnow() + timedelta(minutes=15)).isoformat()}, user.email)
            db.commit()
            continue
        result = inbox_triage.analyse(messages, account_hint(account))
        if inbox_claude.configured() and any(m.get("direction") == "incoming" and m.get("body") for m in messages):
            try:
                result = inbox_claude.analyse_with_claude(messages, account_hint(account))
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                result["retry_at"] = (utcnow() + timedelta(minutes=30)).isoformat()
                result["reason"] = "Claude unavailable. " + result["reason"]
        result.update(stamp=version, message_stamp=message_stamp(messages), scanned_at=utcnow().isoformat())
        save_value(db, key, result, user.email)
        db.commit()
        db.expire_all()
    return attempted


def poll_once():
    with SessionLocal() as db:
        owner = db.scalar(select(User).where(User.email == config.GHQ_PERSONAL_INBOX_OWNER_EMAIL,
                                            User.is_active.is_(True)))
        if owner is None or not enabled(db, owner):
            return
        # Keep the lease on its own connection across commits. A dead process releases it.
        with engine.connect() as lock:
            if lock.scalar(text("SELECT pg_try_advisory_xact_lock(1248917763)")):
                scan_cycle(db, owner)


def start():
    stop = threading.Event()
    def run():
        while not stop.wait(35):
            try:
                poll_once()
            except Exception as error:
                LOG.warning("Inbox triage cycle deferred (%s)", type(error).__name__)
    worker = threading.Thread(target=run, name="inbox-triage", daemon=True)
    worker.start()
    return stop
