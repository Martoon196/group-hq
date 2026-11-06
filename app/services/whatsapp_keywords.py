"""Group-keyword rules and durable delivery decisions, separate from Meta receipts.

Claude's normalise/findTrigger matcher is ported below for server-side validation;
the companion Node adapter reuses its original JavaScript implementation.
No provider credential or WhatsApp session belongs in this web application.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import config
from ..models import AppSetting, KeywordDelivery, KeywordEvent, KeywordSuppression, User, utcnow
from .audit import audit

BRANDS = {"mhp": "Martin Howard Property", "pp": "Property Predator", "kp": "Key Properties UK",
          "hnh": "Hello New Home", "ordris": "Ordris"}
RULES_KEY = "whatsapp_keywords.rules"
GROUPS_KEY = "whatsapp_keywords.groups"
LISTENER = "personal-7510"
SOURCE = "whatsapp_web_group_keyword"
BASE = "/api/whatsapp-keywords/v1"
GROUP = re.compile(r"^[0-9-]{5,80}@g\.us$")
SENDER = re.compile(r"^[0-9]{5,30}@(c\.us|lid)$")


def normalise(value: str) -> str:
    return " ".join("".join(c if c.isalnum() or c.isspace() else " " for c in value.lower()).split())


def matches(body: str, keywords: list[str], mode: str) -> bool:
    body = normalise(body)
    return bool(body) and any(body == normalise(k) if mode == "exact" else
                              f" {normalise(k)} " in f" {body} " for k in keywords)


def problem(message: str, status: int = 400):
    raise HTTPException(status_code=status, detail=message)


def setting(db: Session, key: str, default):
    row = db.get(AppSetting, key)
    return row.value if row else default


def lock_rules(db: Session):
    # One short transaction serialises edits and dispatch admission, not provider IO.
    db.execute(text("SELECT pg_advisory_xact_lock(741935832)"))


def rules(db: Session) -> dict:
    return setting(db, RULES_KEY, {"revision": 0, "rules": []})


def put_setting(db: Session, key: str, value, actor: str):
    db.execute(insert(AppSetting).values(key=key, value=value, updated_by=actor, updated_at=utcnow())
               .on_conflict_do_update(index_elements=[AppSetting.key], set_={
                   "value": value, "updated_by": actor, "updated_at": utcnow()}))
    db.expire_all()


def save_rule(db: Session, user: User, form: dict):
    lock_rules(db)
    current = rules(db)
    if form["revision"] != current["revision"]:
        problem("These rules changed. Refresh before saving.", 409)
    rule_id = form.get("id") or str(uuid.uuid4())
    try:
        uuid.UUID(rule_id)
    except (ValueError, TypeError):
        problem("Invalid rule")
    existing = next((r for r in current["rules"] if r["id"] == rule_id), None)
    if form.get("id") and not existing:
        problem("Rule not found", 404)
    brand, name, message = form["brand"], form["name"].strip(), form["message"].strip()
    keywords = list(dict.fromkeys(k.strip() for k in form["keywords"].splitlines() if k.strip()))
    group_ids = list(dict.fromkeys(form["groups"]))
    known = {g["id"] for g in setting(db, GROUPS_KEY, {}).get("groups", [])}
    if brand not in BRANDS or not 1 <= len(name) <= 100 or not 1 <= len(message) <= 4000:
        problem("Choose a business, a rule name and a reply of up to 4,000 characters.")
    if not 1 <= len(keywords) <= 20 or any(not normalise(k) or len(k) > 80 for k in keywords):
        problem("Add 1–20 keywords, one per line, up to 80 characters each.")
    if form["matchMode"] not in ("exact", "word") or not group_ids or any(g not in known for g in group_ids):
        problem("Choose at least one connected group and a valid matching option.")
    enabled = form["enabled"]
    if enabled and not form.get("requestedReply"):
        problem("Confirm the group invitation promises this private reply before enabling it.")
    for other in current["rules"]:
        if other["id"] != rule_id and other["enabled"] and enabled and set(group_ids) & set(other["groupIds"]):
            # Reject overlap, including phrase/whole-word combinations, rather than choosing a brand arbitrarily.
            if any(matches(k, other["keywords"], other["matchMode"]) or matches(o, [k], form["matchMode"])
                   for k in keywords for o in other["keywords"]):
                problem("An enabled rule already matches that keyword in one of these groups.", 409)
    item = {"id": rule_id, "brand": brand, "name": name, "keywords": keywords, "message": message,
            "groupIds": group_ids, "matchMode": form["matchMode"], "enabled": enabled}
    updated = {"revision": current["revision"] + 1,
               "rules": [r for r in current["rules"] if r["id"] != rule_id] + [item]}
    if len(updated["rules"]) > 100:
        problem("Keep at most 100 rules.")
    put_setting(db, RULES_KEY, updated, user.email)
    audit(db, user, "whatsapp.keyword.save", "whatsapp-keyword:" + rule_id, before=existing, after=item)
    db.commit()


async def authenticated_body(request: Request) -> bytes:
    secret = config.WHATSAPP_KEYWORD_ADAPTER_SECRET
    if not 32 <= len(secret) <= 256:
        problem("Keyword adapter is not connected", 503)
    stamp = request.headers.get("x-ghq-timestamp", "")
    supplied = request.headers.get("x-ghq-signature", "")
    if not re.fullmatch(r"[0-9]{10}", stamp) or abs(time.time() - int(stamp)) > 120:
        problem("Adapter authentication failed", 401)
    if request.query_params or request.headers.get("content-encoding"):
        problem("Invalid adapter request")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > (400000 if request.url.path == BASE + '/groups' else 32000):
            problem("Request too large", 413)
    material = f"{stamp}\n{request.method}\n{request.url.path}\n".encode() + body
    expected = hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()
    if not re.fullmatch(r"[a-f0-9]{64}", supplied) or not hmac.compare_digest(expected, supplied):
        problem("Adapter authentication failed", 401)
    return bytes(body)


def json_object(body: bytes) -> dict:
    try:
        value = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        problem("Invalid JSON")
    if not isinstance(value, dict):
        problem("Expected an object")
    return value


def sync_groups(db: Session, data: dict):
    if set(data) != {"listenerId", "groups"} or data["listenerId"] != LISTENER or not isinstance(data["groups"], list) or len(data["groups"]) > 1000:
        problem("Invalid group inventory")
    groups = []
    for g in data["groups"]:
        if not isinstance(g, dict) or set(g) != {"id", "name"} or not isinstance(g["id"], str) or not GROUP.fullmatch(g["id"]) or not isinstance(g["name"], str) or not 1 <= len(g["name"]) <= 200:
            problem("Invalid group inventory")
        groups.append(g)
    if len({g["id"] for g in groups}) != len(groups):
        problem("Repeated group")
    put_setting(db, GROUPS_KEY, {"groups": groups, "checkedAt": utcnow().isoformat(), "listenerId": LISTENER}, "adapter:" + LISTENER)
    db.commit()
    return {"ok": True}


def ingest(db: Session, data: dict, body: bytes) -> dict:
    expected = {"eventId", "listenerId", "groupId", "senderId", "senderPhone", "senderName", "text", "observedAt"}
    if set(data) != expected or any(not isinstance(v, str) for v in data.values()):
        problem("Invalid keyword event")
    if data["listenerId"] != LISTENER or not GROUP.fullmatch(data["groupId"]) or not SENDER.fullmatch(data["senderId"]) or not re.fullmatch(r"[1-9][0-9]{6,14}", data["senderPhone"]) or not 1 <= len(data["eventId"]) <= 200 or not 1 <= len(data["text"]) <= 4096 or len(data["senderName"]) > 200:
        problem("Invalid keyword event")
    try:
        observed = datetime.fromisoformat(data["observedAt"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            problem("Invalid event time")
    except ValueError:
        problem("Invalid event time")
    lock_rules(db)
    sha = hashlib.sha256(body).hexdigest()
    old = db.scalar(select(KeywordEvent).where(KeywordEvent.listener_id == LISTENER, KeywordEvent.external_id == data["eventId"]))
    if old:
        if old.request_sha256 != sha:
            problem("Event ID has different evidence", 409)
        return {"outcome": "replayed", "deliveryId": old.delivery_id}
    if abs((datetime.now(timezone.utc) - observed).total_seconds()) > 300:
        problem("Keyword event is outside its admission window")
    state = rules(db)
    hits = [r for r in state["rules"] if r["enabled"] and data["groupId"] in r["groupIds"] and matches(data["text"], r["keywords"], r["matchMode"])]
    if len(hits) != 1:
        return {"outcome": "ignored", "deliveryId": None}
    rule = hits[0]
    phone_sha = hashlib.sha256(data["senderPhone"].encode()).hexdigest()
    suppression = db.get(KeywordSuppression, phone_sha)
    if suppression and suppression.blocked:
        db.add(KeywordEvent(listener_id=LISTENER, external_id=data["eventId"], request_sha256=sha))
        db.commit()
        return {"outcome": "opted_out", "deliveryId": None}
    previous = db.scalar(select(KeywordDelivery).where(KeywordDelivery.rule_id == rule["id"], KeywordDelivery.sender_sha256 == phone_sha))
    job = None
    if previous is None:
        first = (data["senderName"].split() or ["there"])[0]
        reply = rule["message"].replace("{name}", first)
        job = KeywordDelivery(id=str(uuid.uuid4()), rule_id=rule["id"], rule_revision=state["revision"], brand=rule["brand"],
                              sender_sha256=phone_sha, sender_phone=data["senderPhone"], sender_name=data["senderName"],
                              sender_id=data["senderId"], group_id=data["groupId"], reply=reply, state="pending")
        db.add(job)
        db.flush()
    event = KeywordEvent(listener_id=LISTENER, external_id=data["eventId"], request_sha256=sha, delivery_id=job.id if job else None)
    db.add(event)
    db.commit()
    return {"outcome": "queued" if job else "already_contacted", "deliveryId": job.id if job else None}


def begin(db: Session, job_id: str) -> dict:
    lock_rules(db)
    job = db.scalar(select(KeywordDelivery).where(KeywordDelivery.id == job_id).with_for_update())
    if not job:
        problem("Delivery not found", 404)
    if job.state != "pending":
        return {"send": False, "state": job.state}
    state = rules(db)
    rule = next((r for r in state["rules"] if r["id"] == job.rule_id), None)
    suppression = db.get(KeywordSuppression, job.sender_sha256)
    if state["revision"] != job.rule_revision or not rule or not rule["enabled"] or job.group_id not in rule["groupIds"] or (suppression and suppression.blocked):
        job.state = "held"
        db.commit()
        return {"send": False, "state": "held"}
    job.state, job.begun_at = "calling", utcnow()
    db.commit()
    return {"send": True, "deliveryId": job.id, "recipientId": job.sender_id, "message": job.reply}


def settle(db: Session, job_id: str, data: dict) -> dict:
    if set(data) != {"state", "providerMessageId"} or data["state"] not in ("sent", "outcome_unknown") or not isinstance(data["providerMessageId"], str) or len(data["providerMessageId"]) > 250:
        problem("Invalid delivery result")
    if data["state"] == "sent" and not data["providerMessageId"]:
        problem("A sent message needs its provider ID")
    job = db.scalar(select(KeywordDelivery).where(KeywordDelivery.id == job_id).with_for_update())
    if not job:
        problem("Delivery not found", 404)
    if job.state in ("sent", "outcome_unknown"):
        if (job.state, job.provider_message_id or "") != (data["state"], data["providerMessageId"]):
            problem("Result has different evidence", 409)
        return {"ok": True, "replayed": True}
    if job.state != "calling":
        problem("Delivery has not begun", 409)
    job.state, job.provider_message_id, job.settled_at = data["state"], data["providerMessageId"], utcnow()
    db.commit()
    return {"ok": True, "replayed": False}


def opt_out(db: Session, data: dict, body: bytes) -> dict:
    """Only private STOP/START controls; other personal-message content is never ingested."""
    if set(data) != {"eventId", "senderPhone", "action", "observedAt"} or any(not isinstance(v, str) for v in data.values()) or data["action"] not in ("STOP", "START") or not 1 <= len(data["eventId"]) <= 200 or not re.fullmatch(r"[1-9][0-9]{6,14}", data["senderPhone"]):
        problem("Invalid opt-out control")
    try:
        occurred = datetime.fromisoformat(data["observedAt"].replace("Z", "+00:00"))
        if occurred.tzinfo is None:
            problem("Invalid opt-out time")
        occurred = occurred.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        problem("Invalid opt-out time")
    lock_rules(db)
    sha = hashlib.sha256(body).hexdigest()
    old = db.scalar(select(KeywordEvent).where(KeywordEvent.listener_id == LISTENER, KeywordEvent.external_id == data["eventId"]))
    if old:
        if old.request_sha256 != sha:
            problem("Event ID has different evidence", 409)
        return {"ok": True, "replayed": True}
    age = (utcnow() - occurred).total_seconds()
    if age < -300 or age > (604800 if data['action'] == 'STOP' else 300):
        problem("Invalid opt-out time")
    phone_sha = hashlib.sha256(data["senderPhone"].encode()).hexdigest()
    old_control = db.get(KeywordSuppression, phone_sha)
    # Older callbacks cannot release a newer STOP; at equal times STOP wins.
    if not old_control or occurred > old_control.occurred_at or (occurred == old_control.occurred_at and data["action"] == "STOP"):
        db.merge(KeywordSuppression(sender_sha256=phone_sha, blocked=data["action"] == "STOP", occurred_at=occurred))
    db.add(KeywordEvent(listener_id=LISTENER, external_id=data["eventId"], request_sha256=sha))
    db.commit()
    return {"ok": True, "replayed": False}
