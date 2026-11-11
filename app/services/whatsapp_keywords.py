"""Group-keyword rules and durable delivery decisions, separate from Meta receipts.

Claude's normalise/findTrigger matcher is ported below for server-side validation;
the companion Node adapter reuses its original JavaScript implementation.
No provider credential or WhatsApp session belongs in this web application.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from .. import config
from ..models import AppSetting, KeywordDelivery, KeywordEvent, KeywordSuppression, User, utcnow
from .audit import audit

BRANDS = {"mhp": "Martin Howard Property", "pp": "Property Predator", "kp": "Key Properties UK",
          "hnh": "Hello New Home", "ordris": "Ordris"}
RULES_KEY = "whatsapp_keywords.rules"
GROUPS_KEY = "whatsapp_keywords.groups"
CATCHUPS_KEY = "whatsapp_keywords.catchups"
LISTENER = "personal-7510"
SOURCE = "whatsapp_web_group_keyword"
BASE = "/api/whatsapp-keywords/v1"
GROUP = re.compile(r"^[0-9-]{5,80}@g\.us$")
SENDER = re.compile(r"^[0-9]{5,30}@(c\.us|lid)$")
EVENT_FIELDS = {"eventId", "listenerId", "groupId", "senderId", "senderPhone", "senderName", "text", "observedAt"}
SEND_GAP = 15                # seconds between two private replies leaving the owner's number, live or catch-up
CATCHUP_HOURS = (1, 168)     # the look-back an owner may ask for: one hour to seven days
CATCHUP_DEFAULT_HOURS = 48
CATCHUP_EXPIRY = 7200        # seconds an open catch-up waits for the listener to finish reading
CATCHUP_CAP = 300            # replies one catch-up may queue; later matches are counted, not queued
CATCHUP_SKEW = 120           # clock tolerance at the window's two edges, in seconds
CATCHUP_KEPT = 10            # how many catch-ups stay on record, newest first
CATCHUP_COUNTS = ("queued", "already_contacted", "opted_out", "over_cap", "replayed", "outside_window", "ignored")


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
    # A catch-up answers for the rules as they stood when the owner asked; any edit ends it (begin holds its unsent replies).
    kept = catchups(db)
    if any(c["state"] == "open" for c in kept):
        for c in kept:
            if c["state"] == "open":
                c.update(state="cancelled", reason="rules_changed", closedAt=_stamp(_now()))
        _save_catchups(db, kept, user.email)
    audit(db, user, "whatsapp.keyword.save", "whatsapp-keyword:" + rule_id, before=existing, after=item)
    db.commit()
    return item


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
        if len(body) > (400000 if request.url.path in (BASE + '/groups', BASE + '/direct/history') else 32000):
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


def _event(data: dict, fields: set) -> datetime:
    """A listener's keyword event, checked field by field; the time WhatsApp gave the message."""
    if set(data) != fields or any(not isinstance(v, str) for v in data.values()):
        problem("Invalid keyword event")
    if data["listenerId"] != LISTENER or not GROUP.fullmatch(data["groupId"]) or not SENDER.fullmatch(data["senderId"]) or not re.fullmatch(r"[1-9][0-9]{6,14}", data["senderPhone"]) or not 1 <= len(data["eventId"]) <= 200 or not 1 <= len(data["text"]) <= 4096 or len(data["senderName"]) > 200:
        problem("Invalid keyword event")
    try:
        observed = datetime.fromisoformat(data["observedAt"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            problem("Invalid event time")
    except ValueError:
        problem("Invalid event time")
    return observed


def _recorded(db: Session, event_id: str):
    return db.scalar(select(KeywordEvent).where(KeywordEvent.listener_id == LISTENER, KeywordEvent.external_id == event_id))


def _admit(db: Session, data: dict, sha: str, rule: dict, revision: int, room: bool = True) -> dict:
    """One matching request for one enabled rule, live or from a catch-up: STOP is respected, a person gets a rule's
    reply once, and the event is recorded so its replay has no effect. room=False (a catch-up at its cap) queues nothing
    and records nothing. The caller holds the rules lock and commits."""
    phone_sha = hashlib.sha256(data["senderPhone"].encode()).hexdigest()
    suppression = db.get(KeywordSuppression, phone_sha)
    if suppression and suppression.blocked:
        db.add(KeywordEvent(listener_id=LISTENER, external_id=data["eventId"], request_sha256=sha))
        return {"outcome": "opted_out", "deliveryId": None}
    previous = db.scalar(select(KeywordDelivery).where(KeywordDelivery.rule_id == rule["id"], KeywordDelivery.sender_sha256 == phone_sha))
    job_id = None
    if previous is None:
        if not room:
            return {"outcome": "over_cap", "deliveryId": None}
        first = (data["senderName"].split() or ["there"])[0]
        reply = rule["message"].replace("{name}", first)
        job_id = str(uuid.uuid4())
        db.add(KeywordDelivery(id=job_id, rule_id=rule["id"], rule_revision=revision, brand=rule["brand"],
                               sender_sha256=phone_sha, sender_phone=data["senderPhone"], sender_name=data["senderName"],
                               sender_id=data["senderId"], group_id=data["groupId"], reply=reply, state="pending"))
        db.flush()
    db.add(KeywordEvent(listener_id=LISTENER, external_id=data["eventId"], request_sha256=sha, delivery_id=job_id))
    return {"outcome": "queued" if job_id else "already_contacted", "deliveryId": job_id}


def ingest(db: Session, data: dict, body: bytes) -> dict:
    observed = _event(data, EVENT_FIELDS)
    lock_rules(db)
    sha = hashlib.sha256(body).hexdigest()
    old = _recorded(db, data["eventId"])
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
    result = _admit(db, data, sha, hits[0], state["revision"])
    db.commit()
    return result


# ---- catch-up: an explicit, bounded request to answer earlier messages ------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _stamp(value: datetime) -> str:
    return value.isoformat()


def catchups(db: Session) -> list[dict]:
    """The catch-ups on record, newest first, as they stand now: an open one whose two hours have passed reads as
    expired (and is saved so at the next write). A copy, safe to change before _save_catchups."""
    items = copy.deepcopy(setting(db, CATCHUPS_KEY, {"catchups": []})["catchups"])
    now = _now()
    for item in items:
        if item["state"] == "open" and now >= datetime.fromisoformat(item["expiresAt"]):
            item.update(state="expired", reason="listener_did_not_finish", closedAt=item["expiresAt"])
    return items


def _save_catchups(db: Session, items: list[dict], actor: str):
    put_setting(db, CATCHUPS_KEY, {"catchups": items[:CATCHUP_KEPT]}, actor)


def catchup_snapshot(db: Session) -> list[dict]:
    """What the listener is told to read: only a catch-up that is open now, with the groups it has yet to report."""
    return [{"id": c["id"], "ruleId": c["ruleId"], "since": c["since"], "until": c["until"], "expiresAt": c["expiresAt"],
             "groupIds": [g["id"] for g in c["groups"]],
             "pendingGroupIds": [g["id"] for g in c["groups"] if g["status"] == "pending"]}
            for c in catchups(db) if c["state"] == "open"]


def request_catchup(db: Session, user: User, rule_id: str, hours: int, requested_reply: bool) -> dict:
    """The owner asks one enabled rule to answer people who typed its keyword in its groups during the last `hours`.
    Nothing is read or sent here: the listener reads the groups' earlier messages and submits the matches."""
    lock_rules(db)
    state = rules(db)
    rule = next((r for r in state["rules"] if r["id"] == rule_id), None)
    if not rule:
        problem("Rule not found", 404)
    if isinstance(hours, bool) or not isinstance(hours, int) or not CATCHUP_HOURS[0] <= hours <= CATCHUP_HOURS[1]:
        problem("Choose how far back to look: from 1 hour to 7 days (168 hours).")
    if not rule["enabled"]:
        problem("Enable this rule before catching up on earlier messages.")
    if not requested_reply:
        problem("Confirm these group members were told this keyword requests this private reply before catching up.")
    kept = catchups(db)
    if any(c["state"] == "open" for c in kept):
        problem("A catch-up is already running. Wait for it to finish, or cancel it first.", 409)
    now = _now()
    item = {"id": str(uuid.uuid4()), "ruleId": rule["id"], "ruleName": rule["name"], "revision": state["revision"], "hours": hours,
            "since": _stamp(now - timedelta(hours=hours)), "until": _stamp(now), "requestedBy": user.email,
            "requestedAt": _stamp(now), "expiresAt": _stamp(now + timedelta(seconds=CATCHUP_EXPIRY)), "state": "open",
            "reason": None, "closedAt": None, "counts": dict.fromkeys(CATCHUP_COUNTS, 0), "deliveryIds": [],
            "groups": [{"id": g, "status": "pending", "reachedSince": False, "scanned": 0, "matched": 0, "unresolved": 0,
                        "reportedAt": None} for g in rule["groupIds"]]}
    _save_catchups(db, [item] + kept, user.email)
    audit(db, user, "whatsapp.keyword.catchup", "whatsapp-keyword:" + rule["id"], after={
        "catchupId": item["id"], "hours": hours, "since": item["since"], "until": item["until"],
        "revision": item["revision"], "groupIds": rule["groupIds"]})
    db.commit()
    return item


def cancel_catchup(db: Session, user: User, catchup_id: str) -> tuple[dict, int]:
    """Stop an open catch-up: nothing more is admitted, and its replies not yet sent are held (never sent later).
    The catch-up as cancelled, and how many replies were held."""
    lock_rules(db)
    kept = catchups(db)
    item = next((c for c in kept if c["id"] == catchup_id), None)
    if not item:
        problem("Catch-up not found", 404)
    if item["state"] != "open":
        problem("That catch-up is no longer running.", 409)
    item.update(state="cancelled", reason="owner", closedAt=_stamp(_now()))
    held = 0
    if item["deliveryIds"]:
        held = db.execute(update(KeywordDelivery).where(KeywordDelivery.id.in_(item["deliveryIds"]),
                                                        KeywordDelivery.state == "pending").values(state="held")).rowcount
    _save_catchups(db, kept, user.email)
    audit(db, user, "whatsapp.keyword.catchup.cancel", "whatsapp-keyword:" + item["ruleId"],
          before={"catchupId": item["id"], "state": "open"}, after={"catchupId": item["id"], "state": "cancelled", "held": held})
    db.commit()
    return item, held


def ingest_catchup(db: Session, data: dict, body: bytes) -> dict:
    """An earlier group message the listener read for an open catch-up. Admitted only inside that catch-up's rule,
    groups and window; then exactly the live rules (_admit). A message already recorded, live or by an earlier catch-up,
    is a replay without effect. Nothing here raises for a catch-up that has ended: the listener's outbox must drain."""
    observed = _event(data, EVENT_FIELDS | {"catchupId"})
    try:
        uuid.UUID(data["catchupId"])
    except ValueError:
        problem("Invalid keyword event")
    lock_rules(db)
    kept = catchups(db)
    item = next((c for c in kept if c["id"] == data["catchupId"] and c["state"] == "open"), None)
    state = rules(db)
    rule = next((r for r in state["rules"] if item and r["id"] == item["ruleId"]), None)
    old = _recorded(db, data["eventId"])
    if not item or not rule or not rule["enabled"] or state["revision"] != item["revision"]:
        return {"outcome": "replayed", "deliveryId": old.delivery_id} if old else {"outcome": "closed", "deliveryId": None}
    since, until = datetime.fromisoformat(item["since"]), datetime.fromisoformat(item["until"])
    skew = timedelta(seconds=CATCHUP_SKEW)
    if old:
        result = {"outcome": "replayed", "deliveryId": old.delivery_id}
    elif data["groupId"] not in [g["id"] for g in item["groups"]] or not matches(data["text"], rule["keywords"], rule["matchMode"]):
        result = {"outcome": "ignored", "deliveryId": None}
    elif not since - skew <= observed <= until + skew:
        result = {"outcome": "outside_window", "deliveryId": None}
    else:
        result = _admit(db, data, hashlib.sha256(body).hexdigest(), rule, state["revision"],
                        room=item["counts"]["queued"] < CATCHUP_CAP)
    item["counts"][result["outcome"]] += 1
    if result["outcome"] == "queued":
        item["deliveryIds"].append(result["deliveryId"])
    _save_catchups(db, kept, "adapter:" + LISTENER)
    db.commit()
    return result


def catchup_progress(db: Session, catchup_id: str, data: dict) -> dict:
    """The listener finished one group: read back to the window's start (reachedSince), read as far as the phone would
    serve, or could not read it at all (unavailable). When every group has reported, the catch-up is done."""
    numbers = ("scanned", "matched", "unresolved")
    if set(data) != {"listenerId", "groupId", "status", "reachedSince", *numbers} or data["listenerId"] != LISTENER or not isinstance(data["groupId"], str) or not GROUP.fullmatch(data["groupId"]) or data["status"] not in ("scanned", "unavailable") or not isinstance(data["reachedSince"], bool) or any(isinstance(data[n], bool) or not isinstance(data[n], int) or not 0 <= data[n] <= 1_000_000 for n in numbers):
        problem("Invalid catch-up progress")
    lock_rules(db)
    kept = catchups(db)
    item = next((c for c in kept if c["id"] == catchup_id), None)
    if not item or item["state"] != "open":
        return {"ok": True, "state": item["state"] if item else "closed"}
    group = next((g for g in item["groups"] if g["id"] == data["groupId"]), None)
    if not group:
        problem("Invalid catch-up progress")
    group.update(status=data["status"], reachedSince=data["reachedSince"] and data["status"] == "scanned",
                 reportedAt=_stamp(_now()), **{n: data[n] for n in numbers})
    if all(g["status"] != "pending" for g in item["groups"]):
        item.update(state="done", closedAt=_stamp(_now()))
    _save_catchups(db, kept, "adapter:" + LISTENER)
    db.commit()
    return {"ok": True, "state": item["state"]}


def pending(db: Session) -> dict:
    """The next private reply the listener may send: at most one, and none within SEND_GAP seconds of the last one
    begun, so a busy group or a catch-up leaves the owner's number slowly. A reply queued under earlier rules is held
    here, as begin would hold it, so it cannot delay the ones behind it."""
    lock_rules(db)
    db.execute(update(KeywordDelivery).where(KeywordDelivery.state == "pending",
                                             KeywordDelivery.rule_revision != rules(db)["revision"]).values(state="held"))
    db.commit()
    latest = db.scalar(select(func.max(KeywordDelivery.begun_at)))
    if latest and (utcnow() - latest).total_seconds() < SEND_GAP:
        return {"deliveryIds": []}
    return {"deliveryIds": list(db.scalars(select(KeywordDelivery.id).where(KeywordDelivery.state == "pending")
                                           .order_by(KeywordDelivery.created_at).limit(1)).all())}


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


STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPTOUT", "OPT OUT"}


def apply_control(db, phone_sha, occurred, blocked):
    old = db.get(KeywordSuppression, phone_sha)
    # Older callbacks cannot release a newer STOP. At equal times STOP wins.
    if not old or occurred > old.occurred_at or (occurred == old.occurred_at and blocked):
        db.merge(KeywordSuppression(sender_sha256=phone_sha, blocked=blocked, occurred_at=occurred))


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
    apply_control(db, phone_sha, occurred, data["action"] == "STOP")
    db.add(KeywordEvent(listener_id=LISTENER, external_id=data["eventId"], request_sha256=sha))
    db.commit()
    return {"ok": True, "replayed": False}
