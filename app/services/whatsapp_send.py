"""The owner's "Send a WhatsApp" screen: one message to one CRM contact, from the Martin Howard Property number.

Relaunch72 is the engine (its docs/mhp-whatsapp-owner-one-to-one.md is the contract): it holds the number, the
permission, the approved messages, the exact preview, the queue and every outcome. Growth HQ is only the cockpit. This
module asks Relaunch72 through the signed owner commands of app.services.inbox_crm and turns its answers into the plain
sentences the screen shows. Nothing here marks a message sent: every status on the screen is Relaunch72's own.

Each send attempt writes two audit rows (before and after the call), with a digest of the message and never its text.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import select

from .. import config
from ..models import CampaignLead
from . import inbox_crm
from .audit import audit
from .inbox_providers import BRANDS as BRAND_NAMES

PAGE = "/crm/whatsapp/send"
BRANDS = {key: BRAND_NAMES[key] for key in ("pp", "mhp", "kp", "piuk")}      # the brands Relaunch72 keeps a CRM for
SENDER = "Martin Howard Property"
OFF = "WhatsApp sending isn't switched on yet"
CONFIRM_NUMBER = "I have checked this is their WhatsApp number"
# The two ratified answers to "how do you know this person?"; Relaunch72 records each as legitimate interests.
REASONS = {"existing_client": "They are an existing or recent client",
           "business_introduction": "This is a one-off business introduction"}
UNAVAILABLE = "We couldn't reach the CRM just now, so nothing was sent. Try again in a minute."
UNCONFIRMED = ("We couldn't confirm that with the CRM. Press the button to try again: the same message will not be "
               "sent twice.")
NOT_CONFIRMED = "Not confirmed by WhatsApp — do not resend yet"

# What a send's state reads as. "Sent" only once Relaunch72 reports that WhatsApp accepted the message.
STATES = {"queued": "Queued", "calling": "Sending now", "accepted": "Sent", "sent": "Sent", "delivered": "Delivered",
          "read": "Read", "failed": "Not sent", "outcome_unknown": NOT_CONFIRMED,
          "expired": "Not sent — it wasn't picked up in time", "blocked": "Not sent — a safety check stopped it"}
STATE_HELP = {
    "queued": "It is waiting its turn to go out. This page does not update by itself.",
    "calling": "It is being handed to WhatsApp now. This page does not update by itself.",
    "accepted": "WhatsApp has accepted it. It has not reached their phone yet.",
    "sent": "WhatsApp has accepted it. It has not reached their phone yet.",
    "delivered": "It has reached their phone.",
    "read": "They have opened it.",
    "outcome_unknown": "We can't tell whether it went. Check the conversation on the phone before sending anything else.",
    "expired": "Nothing went out. You can send it again.",
    "blocked": "Nothing went out."}
WAITING = ("queued", "calling", "accepted", "sent", "delivered", "outcome_unknown")     # worth checking again

# Why WhatsApp turned a message down, by its error number, in plain words.
FAILURES = {
    131026: "WhatsApp couldn't deliver it. This number may not be on WhatsApp.",
    131047: "More than 24 hours have passed since they last messaged, so only an approved message can be sent.",
    131049: "WhatsApp chose not to deliver this to them for now. Try later, or wait for them to message you.",
    131050: "This person has stopped marketing messages from this business on WhatsApp.",
    131056: "Too many messages went to this person in a short time. Wait a while before trying again.",
    131048: "WhatsApp has paused sending from this number because too many messages were blocked or reported.",
    368: "WhatsApp has temporarily blocked this number. It needs checking in WhatsApp Manager.",
    131031: "The WhatsApp business account is locked. It needs checking in WhatsApp Manager.",
    130429: "WhatsApp is receiving messages from this number too quickly. Sending is paused for a few minutes.",
    80007: "The WhatsApp business account has hit its limit for now. Sending is paused for about an hour.",
    190: "The WhatsApp number needs reconnecting before anything can be sent.",
    132000: "The blanks didn't match what WhatsApp expects for this approved message.",
    132001: "WhatsApp no longer has this approved message.",
    132015: "WhatsApp has paused this approved message.",
    132016: "WhatsApp has switched this approved message off."}

# Relaunch72's reason codes in the owner's words. An unknown code falls back to Relaunch72's own sentence.
PLAIN = {
    "owner_send_unavailable": OFF + ".",
    "reply_unavailable": "Typed replies aren't switched on yet. Choose an approved message instead.",
    "sender_not_connected": "The Martin Howard Property WhatsApp number isn't connected right now.",
    "contact_not_found": "This contact could not be found. It may have been archived.",
    "number_not_found": "That phone number is no longer on this contact.",
    "number_not_international": "Type the number with its country code, for example +44 7700 900123.",
    "mhp_contact_ambiguous": ("This person appears more than once in the Martin Howard Property contacts. "
                              "Merge those records first, then come back."),
    "link_not_found": "This number is no longer linked to this contact. Choose the number again.",
    "mhp_contact_unavailable": "This person's Martin Howard Property record is no longer available, so nothing can be sent.",
    "number_unverified": "Tick the box to say you have checked this is their WhatsApp number.",
    "no_permission": "Say how you know this person before sending.",
    "permission_stale": "The number has changed since you last sent to this person. Say how you know them again.",
    "basis_not_allowed": "Say how you know this person before sending.",
    "evidence_note_invalid": "Add a short note (8 to 500 characters, on one line) saying how you know this person.",
    "permission_withdrawn": "Permission to WhatsApp this person was withdrawn. Nothing can be sent.",
    "permission_denied": "This person said no to WhatsApp messages. Nothing can be sent.",
    "permission_changed": "Permission or the number changed after this was queued, so it was not sent.",
    "opted_out": "This person has opted out of WhatsApp. Nothing can be sent.",
    "suppressed": "This number is on the do-not-message list. Nothing can be sent.",
    "whatsapp_stop": "This person replied STOP on WhatsApp. Nothing can be sent.",
    "account_restricted": "WhatsApp has paused sending from the Martin Howard Property number for now.",
    "recipient_paused": "WhatsApp has asked us not to message this person for now.",
    "daily_cap_reached": "You've reached today's limit of 20 approved messages. Try again tomorrow.",
    "recipient_recently_messaged": ("This person was sent an approved message in the last 24 hours. "
                                    "Wait for their reply, or try again tomorrow."),
    "template_not_approved": ("WhatsApp hasn't confirmed this message as approved in the last few minutes. "
                              "Choose it again and try once more."),
    "template_not_registered": "This approved message could not be prepared for sending.",
    "variables_invalid": "Fill in every blank. WhatsApp doesn't allow new lines or long gaps inside a blank.",
    "preview_changed": "The message has changed since you checked it. Check it again before sending.",
    "window_closed": ("It has been more than 24 hours since this person last messaged, so only an approved "
                      "message can be sent."),
    "request_conflict": "That send was already used for a different message. Check the message again.",
    "authority_expired": "It wasn't picked up in time, so nothing was sent.",
    "not_owner": "Your account isn't set up to send WhatsApp messages from the Martin Howard Property number.",
    "forbidden_scope": "Your account isn't set up to send WhatsApp messages for this business.",
    "not_eligible": "A safety check failed just before sending, so nothing was sent.",
    "invalid_request": "That couldn't be sent as written. Check the message and try again.",
}
# A number that only needs the owner's "how do you know them" answer before an approved message can go.
NEEDS_REASON = ("number_unverified", "no_permission", "permission_stale", "basis_not_allowed")
# After one of these the same message can be checked again; anything else is a no for now.
CHECK_AGAIN = NEEDS_REASON + ("evidence_note_invalid", "template_not_approved", "variables_invalid", "preview_changed",
                              "request_conflict", "invalid_request")
USE_APPROVED = ("reply_unavailable", "window_closed")

META_ID = re.compile(r"[1-9][0-9]{4,29}")
SHA256 = re.compile(r"[0-9a-f]{64}")
NAME = re.compile(r"[a-z_][a-z0-9_]{0,63}|[1-9][0-9]?")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class Invalid(Exception):
    """A form that cannot be what this screen rendered; the route answers 400."""


def canonical_uuid(value) -> str:
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise Invalid() from None
    if parsed != value:
        raise Invalid()
    return parsed


def plain(reason, fallback=None, limit=None) -> str:
    """Relaunch72's reason code as a sentence for the owner."""
    if reason == "daily_cap_reached" and isinstance(limit, int) and not isinstance(limit, bool) and 0 < limit < 1000:
        return f"You've reached today's limit of {limit} approved messages. Try again tomorrow."
    if reason in PLAIN:
        return PLAIN[reason]
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()[:300]
    return "This can't be sent right now."


def when(value) -> str:
    """Relaunch72's ISO time in UK time, or '' when it is not one."""
    from ..templating import fmt_uk
    try:
        return fmt_uk(datetime.fromisoformat(value.replace("Z", "+00:00")), "%d %b %Y, %H:%M")
    except (ValueError, TypeError, AttributeError):
        return ""


def _text(value, limit) -> str:
    return value[:limit] if isinstance(value, str) else ""


def paused(reason, hold) -> str:
    """A pause in plain words, with why and until when if Relaunch72 says."""
    hold = hold if isinstance(hold, dict) else {}
    parts = [plain(reason, hold.get("message"))]
    if FAILURES.get(hold.get("errorCode")):
        parts.append(FAILURES[hold["errorCode"]])
    if when(hold.get("until")):
        parts.append(f"You can try again after {when(hold['until'])}.")
    return " ".join(dict.fromkeys(parts))


def describe(send: dict) -> dict:
    """One send as the screen shows it. An unknown state is never read as sent."""
    state = send.get("state")
    label = STATES.get(state, NOT_CONFIRMED)
    detail = STATE_HELP.get(state, STATE_HELP["outcome_unknown"] if state not in STATES else "")
    if state == "failed":
        detail = FAILURES.get(send.get("errorCode"), "WhatsApp did not accept this message.")
    elif state in ("blocked", "expired") and send.get("stateReason") in PLAIN:
        detail = plain(send["stateReason"])
    return {"id": _text(send.get("sendId"), 36), "state": _text(state, 30), "label": label, "detail": detail,
            "waiting": state in WAITING or state not in STATES, "good": state in ("accepted", "sent", "delivered", "read"),
            "bad": state in ("failed", "expired", "blocked"),
            "text": _text(send.get("text"), 4096), "number": _text(send.get("number"), 40),
            "at": when(send.get("createdAt")), "typed": send.get("kind") == "text"}


def signer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-whatsapp-send-number-v1")


def number_checked(user, link_id: str) -> str:
    """Proof, for an hour, that this owner ticked the confirmation when adding this number."""
    return signer().dumps({"user": user.id, "link": link_id})


def was_checked(user, link_id: str, token: str) -> bool:
    try:
        return bool(token) and signer().loads(token, max_age=3600) == {"user": user.id, "link": link_id}
    except BadData:
        return False


def local_lead(db, brand: str, contact_id: str) -> CampaignLead | None:
    return db.scalars(select(CampaignLead).where(CampaignLead.brand == brand, CampaignLead.source_system == "relaunch72",
                                                 CampaignLead.external_id == contact_id)).first()


def ask(user, brand: str, action: str, **fields) -> dict:
    """One owner command. The answer, {"off": True} while sending is switched off, {"stop": sentence} for any other
    definite no, or {"unavailable": True} when Relaunch72 gave no confirmed answer."""
    try:
        result = inbox_crm.command(user, brand, action, **fields)
    except inbox_crm.OwnerWhatsAppRefused as refusal:
        if refusal.code == "owner_send_unavailable":
            return {"off": True, "reason": refusal.code}
        return {"stop": plain(refusal.code), "reason": refusal.code}
    except inbox_crm.CrmUnavailable:
        return {"unavailable": True}
    if result.get("state") == "refused":
        reason = _text(result.get("reason"), 60)
        return {"stop": plain(reason, result.get("message"), result.get("limit")), "reason": reason,
                "hold": result.get("hold")}
    return {"ok": result}


def _number(row: dict) -> dict | None:
    """One number Relaunch72 has linked for this contact, with what can be done with it now."""
    try:
        link_id = str(uuid.UUID(row["linkId"]))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    gate = row.get("gate") if isinstance(row.get("gate"), dict) else {}
    window = row.get("window") if isinstance(row.get("window"), dict) else {}
    reason = gate.get("reason")
    ready = gate.get("ok") is True
    needs_reason = not ready and reason in NEEDS_REASON
    return {"link": link_id, "masked": _text(row.get("number"), 40) or "Number hidden",
            "window_open": window.get("open") is True, "closes": when(window.get("closesAt")),
            "ready": ready, "needs_reason": needs_reason,
            "needs_tick": needs_reason and row.get("verification") not in ("meta_verified", "owner_attested"),
            # Anything else Relaunch72 says no to (opted out, STOP, paused, not connected, or a code this screen
            # does not know) stops here.
            "stop": None if ready or needs_reason else (
                paused(reason, gate.get("hold")) if reason in ("account_restricted", "recipient_paused")
                else plain(reason, gate.get("message")))}


def _candidates(rows) -> tuple[list[dict], bool]:
    """Phone numbers on the CRM contact that are not linked yet, and whether one of them could not be read."""
    usable, unreadable = [], False
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("linked") is True:
            continue
        try:
            point = str(uuid.UUID(row["pointId"]))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if row.get("usable") is True and isinstance(row.get("number"), str):
            usable.append({"point": point, "masked": row["number"][:40],
                           "kind": "WhatsApp" if row.get("kind") == "whatsapp" else "Phone"})
        else:
            unreadable = True
    return usable[:10], unreadable


def _message(row: dict) -> dict | None:
    """One approved message that can be sent from here: its words and its blanks."""
    if not isinstance(row, dict) or row.get("sendable") is not True:
        return None
    meta, names, layout = row.get("metaTemplateId"), row.get("parameterNames"), row.get("parameterFormat")
    words = row.get("previewText") or row.get("bodyText")
    if (not isinstance(meta, str) or not META_ID.fullmatch(meta) or layout not in ("NONE", "POSITIONAL", "NAMED")
            or not isinstance(words, str) or not words.strip() or not isinstance(names, list) or len(names) > 20
            or any(not isinstance(n, str) or not NAME.fullmatch(n) for n in names)):
        return None
    title = _text(row.get("name"), 120).replace("_", " ").strip().capitalize() or "Approved message"
    return {"id": meta, "title": title, "text": words[:4096], "format": layout, "names": names,
            "blanks": [{"name": n, "label": f"Blank {n}" if n.isdigit() else n.replace("_", " ").capitalize()}
                       for n in names]}


def variables(layout: str, names: list[str], values: list[str]):
    """The blanks as Relaunch72 wants them: by name, in order, or none."""
    if (layout not in ("NONE", "POSITIONAL", "NAMED") or len(names) != len(values) or len(names) > 20
            or len(set(names)) != len(names) or any(not NAME.fullmatch(n) for n in names)
            or any(len(v) > 500 for v in values) or (layout == "NONE" and names)):
        raise Invalid()
    if layout == "NAMED":
        return dict(zip(names, values))
    return list(values)


def load(db, user, brand: str, contact_id: str, link: str = "") -> dict:
    """Everything step 1 needs, from one request to Relaunch72 (and one more for the name only when Growth HQ does
    not already hold it). {"off"}, {"unavailable"} or {"stop"} when there is nothing to offer."""
    lead = local_lead(db, brand, contact_id)
    view = {"brand": brand, "brand_name": BRANDS[brand], "contact": contact_id, "name": lead.display_name if lead else "",
            "back": f"/crm/leads/{lead.id}" if lead and brand == "pp" else
                    f"/inbox/crm/contact?brand={brand}&contact={contact_id}",
            "off": False, "unavailable": False, "stop": None, "numbers": [], "chosen": None, "candidates": [],
            "unreadable": False, "history": [], "remaining": None, "limit": 20}
    answer = ask(user, brand, "whatsappOwnerState", contactId=contact_id)
    if "ok" not in answer:
        view.update({k: v for k, v in answer.items() if k in ("off", "unavailable", "stop")})
        return view
    state = answer["ok"]
    sender, cap = state.get("sender"), state.get("dailyCap")
    if (state.get("state") != "ok" or not isinstance(state.get("numbers"), list) or not isinstance(sender, dict)
            or not isinstance(state.get("sends"), list)):
        view["unavailable"] = True
        return view
    if not view["name"]:
        try:
            found = inbox_crm.command(user, brand, "contact", contactId=contact_id)
            view["name"] = _text(found["contact"]["name"], 200)
        except (inbox_crm.CrmUnavailable, KeyError, TypeError):
            view["name"] = ""
    view["history"] = [describe(s) for s in state["sends"][:20] if isinstance(s, dict)]
    if isinstance(cap, dict) and all(isinstance(cap.get(k), int) and not isinstance(cap.get(k), bool) for k in ("limit", "remaining")):
        view["remaining"], view["limit"] = cap["remaining"], cap["limit"]
    view["numbers"] = [n for n in (_number(r) for r in state["numbers"] if isinstance(r, dict)) if n]
    view["candidates"], view["unreadable"] = _candidates(state.get("candidates"))
    if sender.get("connected") is not True:
        view["stop"] = plain("sender_not_connected")
    elif isinstance(state.get("accountHold"), dict):
        view["stop"] = paused("account_restricted", state["accountHold"])
    elif view["numbers"]:
        view["chosen"] = next((n for n in view["numbers"] if n["link"] == link), None) or view["numbers"][0]
    return view


def approved_messages(user, brand: str) -> dict:
    """The approved messages that can be sent from here: {"messages": [...]} or the reason there are none."""
    answer = ask(user, brand, "whatsappOwnerTemplates")
    if "ok" not in answer:
        return answer
    rows = answer["ok"].get("templates")
    if answer["ok"].get("connected") is not True or not isinstance(rows, list):
        return {"stop": plain("sender_not_connected")}
    return {"messages": [m for m in (_message(r) for r in rows[:200]) if m]}


def link_number(user, brand: str, contact_id: str, point: str = "", number: str = "") -> dict:
    """Ask Relaunch72 to link one number of this contact to the sender: {"link": id} or why not."""
    number = number.strip()
    if number:
        if not 7 <= len(number) <= 32 or CONTROL.search(number):
            return {"stop": plain("number_not_international")}
        fields = {"number": number}
    else:
        fields = {"pointId": canonical_uuid(point)}
    answer = ask(user, brand, "whatsappOwnerLink", contactId=contact_id, **fields)
    if "ok" not in answer:
        return answer
    try:
        return {"link": str(uuid.UUID(answer["ok"]["linkId"]))}
    except (ValueError, TypeError, KeyError, AttributeError):
        return {"unavailable": True}


def preview(user, brand: str, meta: str, values) -> dict:
    """Relaunch72's exact rendering of an approved message: {"text", "sha256"} or why not."""
    answer = ask(user, brand, "whatsappOwnerPreview", metaTemplateId=meta, variables=values)
    if "ok" not in answer:
        return answer
    text, digest = answer["ok"].get("text"), answer["ok"].get("sha256")
    if (answer["ok"].get("state") != "ok" or not isinstance(text, str) or not text or len(text) > 4096
            or not isinstance(digest, str) or not SHA256.fullmatch(digest)
            or hashlib.sha256(text.encode()).hexdigest() != digest):
        return {"unavailable": True}
    return {"text": text, "sha256": digest}


def status(user, brand: str, contact_id: str, send_id: str) -> dict | None:
    """One send's current status from Relaunch72, or None when it has none for this contact."""
    answer = ask(user, brand, "whatsappOwnerStatus", sendId=send_id)
    if "ok" not in answer or answer["ok"].get("originContactId") != contact_id or answer["ok"].get("sendId") != send_id:
        return None
    return describe(answer["ok"])


def send(db, user, brand: str, contact_id: str, link: str, request_id: str, *, kind: str, meta: str = "",
         values=None, sha256: str = "", body: str = "", reason: str = "", note: str = "", attest: bool = False) -> dict:
    """One send attempt, exactly once per request_id. Records how the owner knows the person first when the screen
    asked for it, then queues the message. Returns {"sent": send id} when Relaunch72 holds the send (queued now, or
    already held under this request id), {"off"}, {"stop", "reason"} for a definite no, or {"unconfirmed": True}
    when Relaunch72 gave no confirmed answer: the same request id may then be tried again and cannot send twice.

    The audit rows carry a digest of the message, never its words or the number."""
    digest = sha256 if kind == "template" else hashlib.sha256(body.encode()).hexdigest()
    entity = "whatsapp-send:" + request_id
    about = {"brand": brand, "contact": contact_id, "kind": "approved message" if kind == "template" else "typed reply",
             "message_sha256": digest}
    audit(db, user, "whatsapp.send.begin", entity, after=about | ({"how_known": reason} if reason else {}))
    db.commit()                 # durable before Relaunch72 is asked: an attempt is on record whatever comes back

    def settle(outcome: dict, state: str) -> dict:
        audit(db, user, "whatsapp.send." + state, entity,
              after=about | {"state": state, "reason": outcome.get("reason"), "relaunch72_state": outcome.get("state")})
        db.commit()
        return outcome

    if reason:
        answer = ask(user, brand, "whatsappOwnerPermission", contactId=contact_id, linkId=link,
                     commandId=str(uuid.uuid5(uuid.UUID(request_id), "how-known")), decision="granted",
                     lawfulBasis="legitimate_interests", evidenceKind=reason, note=note, attestNumber=attest)
        if "unavailable" in answer:
            return settle({"unconfirmed": True}, "unconfirmed")
        if "ok" not in answer:
            return settle(answer, "refused")
        if answer["ok"].get("state") != "granted":
            return settle({"unconfirmed": True}, "unconfirmed")
    if kind == "template":
        answer = ask(user, brand, "whatsappOwnerSendTemplate", contactId=contact_id, linkId=link,
                     idempotencyKey=request_id, metaTemplateId=meta, variables=values, previewSha256=sha256)
    else:
        answer = ask(user, brand, "whatsappOwnerSendText", contactId=contact_id, linkId=link,
                     idempotencyKey=request_id, body=body)
    if "unavailable" in answer:
        return settle({"unconfirmed": True}, "unconfirmed")
    if "ok" not in answer:
        return settle(answer, "refused")
    held = answer["ok"]
    if held.get("sendId") != request_id or held.get("state") not in STATES:
        return settle({"unconfirmed": True}, "unconfirmed")
    # Relaunch72 holds this send. "queued" here only repeats its word; the screen reads the status from Relaunch72.
    return settle({"sent": request_id, "state": held["state"]}, "replayed" if held.get("replayed") is True else "queued")

