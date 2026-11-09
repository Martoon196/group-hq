"""Owner-authored, account-bound replies and per-conversation human handoff."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import exists, func, literal, select, update as sql_update

from ..models import AppSetting, InboxReply, InboxThread, User, utcnow
from . import inbox, inbox_organisation
from .audit import audit
from .inbox_providers import InboxError
from .inbox_keyword_policy import matching_rule, receipt_id, timestamp

PREFIX = "inbox.auto.v1."
HOLD_PREFIX = "inbox.hold.v1."
JOB_PREFIX = "inbox.autojob."
POLL_SECONDS = 60


def now():
    return datetime.now(timezone.utc)


def _digest(parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def available(account, user):
    return account.configured and account.can_reply and (
        not account.owner_email or account.owner_email == user.email.lower())


def lock_account(db, account):
    # Common linearization point for rule edits, handoff and inbox.send's durable reservation.
    number = int.from_bytes(bytes.fromhex(account.identity)[:8], "big", signed=True)
    db.execute(select(func.pg_advisory_xact_lock(number)))


def _row(db, key):
    return db.get(AppSetting, key, populate_existing=True)


def _store(db, key, value, actor):
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_by=actor))
    else:
        row.value, row.updated_by, row.updated_at = value, actor, utcnow()


def settings(db, user, account):
    row = _row(db, PREFIX + account.identity)
    if row and row.value.get("binding") == account.binding:
        return dict(row.value)
    return {"owner_id": user.id, "binding": account.binding, "key": account.key,
            "enabled": False, "rules": [], "status": "Paused"}


def _normal_rule(keyword, reply):
    keyword = " ".join(keyword.strip().split())
    reply = reply.strip()
    if not 1 <= len(keyword) <= 100 or not re.search(r"\w", keyword):
        raise HTTPException(400, "Enter a word or phrase of up to 100 characters.")
    if not reply or len(reply.encode()) > 10000:
        raise HTTPException(400, "Enter a reply of up to 10,000 bytes.")
    return keyword, reply


def update(db, user, account, action, *, rule_id="", keyword="", reply="", topics=""):
    if not available(account, user):
        raise HTTPException(409, "This connection is unavailable for replies.")
    lock_account(db, account)
    config = settings(db, user, account)
    if config["owner_id"] != user.id:
        raise HTTPException(403, "These rules belong to another inbox owner.")
    current = now().isoformat()
    rules = [dict(item) for item in config["rules"]]
    rule = next((item for item in rules if item["id"] == rule_id), None)
    if action == "ai-save":
        phrases = list(dict.fromkeys(" ".join(line.split()).casefold() for line in topics.splitlines() if line.strip()))
        if not 1 <= len(phrases) <= 20 or any(len(p) > 100 or not re.search(r"\w", p) for p in phrases):
            raise HTTPException(400, "Enter between 1 and 20 words or phrases, one per line, up to 100 characters each.")
        config.update(ai_enabled=False, ai_topics=phrases)
    elif action == "ai-enable":
        from . import inbox_claude, inbox_scanner
        if not config.get("ai_topics"):
            raise HTTPException(400, "Save allowed phrases for Claude first.")
        if not inbox_claude.configured() or not inbox_scanner.enabled(db, user):
            raise HTTPException(409, "Connect Claude and turn on inbox scanning before enabling AI replies.")
        if not config.get("ai_enabled"):
            config.update(ai_enabled=True, ai_since=current)
    elif action == "ai-disable":
        config["ai_enabled"] = False
    elif action == "start":
        if not config.get("ai_enabled") and not any(item["enabled"] for item in rules):
            raise HTTPException(400, "Enable a keyword or Claude reply mode before starting this account.")
        if not config["enabled"]:
            config.update(enabled=True, since=current, next_poll=current, scanned_since=current, status="Waiting for new messages")
    elif action == "pause":
        config.update(enabled=False, status="Paused")
    elif action in {"add", "edit"}:
        keyword, reply = _normal_rule(keyword, reply)
        if any(item["keyword"].casefold() == keyword.casefold() and (action == "add" or item["id"] != rule_id) for item in rules):
            raise HTTPException(409, "This account already has that keyword.")
        if action == "add":
            if len(rules) >= 50:
                raise HTTPException(400, "Remove a keyword before adding another.")
            rules.append({"id": str(uuid.uuid4()), "keyword": keyword, "reply": reply, "enabled": False})
        elif rule is None:
            raise HTTPException(404, "Keyword no longer exists.")
        else:
            rule.update(keyword=keyword, reply=reply, enabled=False)
    elif action in {"enable", "disable", "delete"}:
        if rule is None:
            raise HTTPException(404, "Keyword no longer exists.")
        if action == "delete":
            rules.remove(rule)
        elif action == "disable":
            rule["enabled"] = False
        elif not rule["enabled"]:
            rule.update(enabled=True, since=current)
    else:
        raise HTTPException(400, "Unknown automation action.")
    config["rules"] = rules
    if not config.get("ai_enabled") and not any(item["enabled"] for item in rules):
        config.update(enabled=False, status="Paused")
    _store(db, PREFIX + account.identity, config, user.email)
    audit(db, user, "inbox.automation." + action, "inbox-account:" + account.key,
          after={"rule_id": rule_id, "enabled": config["enabled"]})
    db.commit()


def _hold_key(user_id, account, thread_id):
    return HOLD_PREFIX + _digest([user_id, account.identity, account.binding, thread_id])


def handoff_state(db, user_id, account, thread_id):
    row = _row(db, _hold_key(user_id, account, thread_id))
    return dict(row.value) if row else {"held": False, "reason": ""}


def _write_handoff(db, user, account, thread_id, held, reason):
    key = _hold_key(user.id, account, thread_id)
    _store(db, key, {"held": held, "reason": reason, "changed_at": now().isoformat()}, user.email)
    if held and account.provider == "local":
        marked = exists(select(AppSetting.key).where(AppSetting.key == literal(JOB_PREFIX) + InboxReply.id))
        db.execute(sql_update(InboxReply).where(InboxReply.user_id == user.id, InboxReply.account_key == account.key,
            InboxReply.binding_sha256 == account.binding, InboxReply.thread_id == thread_id, InboxReply.state == "queued",
            marked).values(state="rejected", reason="owner_handoff", settled_at=utcnow()).execution_options(synchronize_session=False))
    audit(db, user, "inbox.handoff." + ("takeover" if held else "resume"), key,
          after={"account": account.key, "held": held})


def set_handoff(db, user, account, thread_id, held):
    lock_account(db, account)
    _write_handoff(db, user, account, thread_id, held, "Owner took over" if held else "")
    db.commit()


def _claude_review(db, user, account, thread_id, messages, when):
    from . import inbox_scanner
    row = _row(db, inbox_scanner.result_key(user.id, account, thread_id))
    if row is None:
        return None
    result = row.value
    try:
        scanned = datetime.fromisoformat(result["scanned_at"])
        if scanned.tzinfo is None:
            scanned = scanned.replace(tzinfo=timezone.utc)  # The scanner records naive UTC.
    except (ValueError, KeyError, TypeError):
        return None
    if (result.get("source") != "Claude" or result.get("message_stamp") != inbox_scanner.message_stamp(messages)
            or not when <= scanned <= now() or now() - scanned > timedelta(minutes=10)):
        return None
    return result


def _ai_matches(config, latest, when):
    if not config.get("ai_enabled"):
        return False
    options = [{"keyword": phrase, "enabled": True, "since": config["ai_since"]} for phrase in config["ai_topics"]]
    return matching_rule(options, latest["body"], when, exact=False) is not None


def _message_window(messages):
    return _digest([(m.get("id"), m.get("direction"), m.get("body")) for m in messages[-12:]])


def local_job(db, receipt_id):
    row = _row(db, JOB_PREFIX + receipt_id)
    return row.value if row else None


def local_claim_allowed(db, user, account, receipt, job):
    """Called under the account lock and the receipt row lock, before native dispatch."""
    if not user.is_active or user.must_change_password or handoff_state(db, user.id, account, receipt.thread_id)["held"]:
        return False
    config = settings(db, user, account)
    if not config["enabled"] or config["owner_id"] != user.id or config["since"] != job["account_since"]:
        return False
    choice = _row(db, inbox_organisation.key(user.id, account, receipt.thread_id))
    if choice and choice.value.get("kind") != "business":
        return False
    messages = inbox.local_thread(db, account, receipt.thread_id, user.id).messages
    if not messages or messages[-1]["direction"] != "incoming" or _message_window(messages) != job["message_window"]:
        return False
    if job["rule_id"]:
        return any(r["id"] == job["rule_id"] and r["enabled"] and r["since"] == job["since"] for r in config["rules"])
    from . import inbox_claude, inbox_scanner
    review = _claude_review(db, user, account, receipt.thread_id, messages, timestamp(messages[-1].get("occurred_at")))
    return (config.get("ai_enabled") and config.get("ai_since") == job["since"] and inbox_claude.configured()
            and inbox_scanner.enabled(db, user) and review and review.get("kind") == "business"
            and review.get("needs_owner") is False and review.get("draft") == receipt.body)


def process_thread(db, user, account, thread_id, messages, analyse):
    if not messages or messages[-1].get("direction") != "incoming":
        return
    latest = messages[-1]
    when = timestamp(latest.get("occurred_at"))
    if not when or when > now() or not latest.get("id") or not isinstance(latest.get("body"), str):
        return
    lock_account(db, account)
    try:
        current_account = next((a for a in inbox.accounts(db) if a.key == account.key), None)
        actor = db.get(User, user.id, populate_existing=True)
        if (current_account is None or current_account.binding != account.binding or actor is None
                or not actor.is_active or actor.must_change_password or not available(current_account, actor)):
            return
        if account.provider == "local":
            cached = inbox.local_thread(db, account, thread_id, user.id)
            if not cached.messages or cached.messages[-1] != latest:
                return
        config = settings(db, user, account)
        if not config["enabled"] or config["owner_id"] != user.id or when <= timestamp(config["since"]):
            return
        hold = handoff_state(db, user.id, account, thread_id)
        if hold["held"] or (hold.get("changed_at") and when <= timestamp(hold["changed_at"])):
            return
        choice = _row(db, inbox_organisation.key(user.id, account, thread_id))
        if choice and choice.value.get("kind") != "business":
            return
        from .inbox_triage import HUMAN
        if HUMAN.search(latest["body"]):
            _write_handoff(db, user, account, thread_id, True, "Incoming message requires owner attention.")
            db.commit()
            return
        request_id = receipt_id(account, thread_id, latest["id"])
        existing = db.get(InboxReply, request_id)
        if existing is not None:
            interrupted = existing.state == "calling" and existing.created_at < utcnow() - timedelta(minutes=2)
            if interrupted or existing.state in {"outcome_unknown", "rejected"}:
                _write_handoff(db, user, account, thread_id, True, "Check the last reply in the original conversation.")
                db.commit()
            return
        assessment = analyse(messages, account_brand=None if account.owner_email else account.brand)
        if assessment["kind"] in {"personal", "pitch"}:
            return
        ai_match = _ai_matches(config, latest, when)
        review = _claude_review(db, user, account, thread_id, messages, when)
        rule = matching_rule(config["rules"], latest["body"], when)
        trusted_business = bool(choice and choice.value.get("kind") == "business") or (
            account.provider == "zernio" and not account.owner_email)
        exact_business_command = rule is not None and trusted_business
        for evaluation in (assessment, review):
            if not evaluation:
                continue
            if evaluation is assessment and evaluation.get("kind") == "unclassified" and exact_business_command:
                continue
            if evaluation.get("kind") in {"personal", "pitch"}:
                return
            if evaluation.get("needs_owner") or evaluation.get("kind") != "business":
                if evaluation.get("kind") == "business" or ai_match or review is not None:
                    _write_handoff(db, user, account, thread_id, True, str(evaluation.get("reason") or "Owner review needed")[:300])
                    db.commit()
                return
        text = rule["reply"] if rule else None
        if ai_match and text is None:
            from . import inbox_claude, inbox_scanner
            if not inbox_claude.configured() or not inbox_scanner.enabled(db, user):
                reason = "Inbox scanning is paused. Review this message."
            elif any(len(m.get("body") or "") > 1100 for m in messages[-12:]):
                reason = "The conversation exceeds the text reviewed by Claude."
            elif review is None and now() - when < timedelta(minutes=10):
                return "waiting_for_scan"
            elif review is None or not isinstance(review.get("draft"), str) or not review["draft"].strip():
                reason = "A current Claude reply is unavailable. Review this message."
            else:
                reason, text = None, review["draft"]
            if reason:
                _write_handoff(db, user, account, thread_id, True, reason)
                db.commit()
                return
        if text:
            if account.provider == "local":
                # The marker is committed atomically with inbox.send's queued receipt.
                _store(db, JOB_PREFIX + request_id, {"account_since": config["since"],
                    "rule_id": rule["id"] if rule else None, "since": rule["since"] if rule else config["ai_since"],
                    "message_window": _message_window(messages)}, user.email)
            result = inbox.send(db, user, account, thread_id, text, request_id)
            if result.state in {"calling", "outcome_unknown", "rejected"}:
                lock_account(db, account)
                _write_handoff(db, user, account, thread_id, True, "Check the last reply in the original conversation.")
                db.commit()
    finally:
        db.rollback()  # Releases read-only transaction locks. send commits its reservation before provider IO.


def _windows(db, user, account, since, stopping):
    if account.provider == "local":
        query = select(InboxThread).where(InboxThread.account_key == account.key,
            InboxThread.binding_sha256 == account.binding, InboxThread.owner_user_id == user.id,
            InboxThread.seen_at >= utcnow() - timedelta(seconds=120)).order_by(InboxThread.seen_at.desc())
        windows = [(row.thread_id, row.messages) for row in db.scalars(query)]
        db.rollback()
        yield from windows
        return
    adapter = inbox.client(account)
    rows, _ = adapter.conversations()
    for item in rows:
        if stopping():
            return
        updated = timestamp(item.get("updated_at"))
        if updated and updated < since:
            continue
        try:
            messages, _ = adapter.messages(item["id"])
        except InboxError:
            # Do not advance the account watermark past an unread conversation.
            yield item["id"], None
            continue
        yield item["id"], messages


def poll_account(db, user, account, analyse, stopping=lambda: False):
    lock_account(db, account)
    config = settings(db, user, account)
    current = now()
    if not config["enabled"] or config["owner_id"] != user.id or timestamp(config["next_poll"]) > current:
        db.rollback()
        return
    since = timestamp(config["scanned_since"]) - timedelta(minutes=2)
    config["next_poll"] = (current + timedelta(seconds=POLL_SECONDS)).isoformat()
    _store(db, PREFIX + account.identity, config, user.email)
    db.commit()
    ok, waiting = True, False
    try:
        for thread_id, messages in _windows(db, user, account, since, stopping):
            if stopping():
                return
            if messages is None:
                ok = False
            else:
                outcome = process_thread(db, user, account, thread_id, messages, analyse)
                waiting |= outcome == "waiting_for_scan"
    except (InboxError, HTTPException):
        ok = False
        db.rollback()
    lock_account(db, account)
    refreshed = settings(db, user, account)
    if refreshed["enabled"] and refreshed["next_poll"] == config["next_poll"]:
        refreshed["status"] = "Connection needs checking" if not ok else "Waiting for Claude scan" if waiting else "Listening"
        refreshed["checked_at"] = current.isoformat()
        if ok and not waiting:
            refreshed["scanned_since"] = current.isoformat()
        _store(db, PREFIX + account.identity, refreshed, user.email)
    db.commit()
