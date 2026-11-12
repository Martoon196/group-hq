"""Send a WhatsApp: the owner's one-to-one screen for a CRM contact (app.routers.whatsapp_send).

Relaunch72 is a stand-in here: an httpx.MockTransport that answers the signed owner commands as its contract
(docs/mhp-whatsapp-owner-one-to-one.md in Relaunch72) says it does, checks the signature of every request and keeps
its own queue. So these tests prove what Growth HQ asks, in what order, and that the screen only ever repeats what
Relaunch72 reported. No test reaches a network.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import config
from app.main import app
from app.models import AuditLog, CampaignLead, InboxReply, LapsDeal, User
from app.services import inbox_crm, whatsapp_send

from .conftest import session

PAGE = "/crm/whatsapp/send"
SECRET = "fixture-inbox-credential-" * 2
WORKSPACE = str(uuid.uuid4())
MASKED = "+44 ••• 0126"
HELLO = {"metaTemplateId": "900100200300", "name": "hello_first_name", "language": "en_GB", "category": "MARKETING",
         "status": "APPROVED", "parameterFormat": "NAMED", "parameterNames": ["first_name"],
         "bodyText": "Hi {{first_name}}, Martin here.", "previewText": "Hi {{first_name}}, Martin here.\nReply STOP to opt out.",
         "sendable": True, "unsupportedReason": None}
PLAIN_ONE = {"metaTemplateId": "900100200301", "name": "viewing_reminder", "language": "en_GB", "category": "UTILITY",
             "status": "APPROVED", "parameterFormat": "NONE", "parameterNames": [], "bodyText": "Your viewing is tomorrow.",
             "previewText": "Your viewing is tomorrow.", "sendable": True, "unsupportedReason": None}
PICTURE = {"metaTemplateId": "900100200302", "name": "needs_a_picture", "language": "en_GB", "category": "MARKETING",
           "status": "APPROVED", "parameterFormat": "NONE", "parameterNames": [], "bodyText": "Look at this.",
           "previewText": "Look at this.", "sendable": False, "unsupportedReason": "header_needs_media"}
MESSAGES = {"daily_cap_reached": "Today’s limit for one-to-one WhatsApp templates has been reached. Try again tomorrow."}


class Relaunch72:
    """The owner one-to-one commands, as far as this screen uses them."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.enabled, self.manual_replies, self.connected = True, True, True
        self.numbers: list[dict] = []
        self.candidates: list[dict] = []
        self.templates = [HELLO, PLAIN_ONE, PICTURE]
        self.sends: dict[str, dict] = {}
        self.permissions: list[dict] = []
        self.account_hold = None
        self.remaining = 20
        self.refuse: dict[str, dict] = {}       # action -> a refusal to answer with
        self.broken: set[str] = set()           # actions that get no confirmed answer
        self.name = "Dana Buyer"

    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]

    def number(self, **changes) -> dict:
        row = {"linkId": str(uuid.uuid4()), "number": MASKED, "numberSource": "crm_point", "verification": "unverified",
               "permission": "none", "blocked": None, "available": "true", "window": {"open": False},
               "gate": {"ok": False, "reason": "number_unverified", "message": "Not confirmed.", "hold": None}}
        row.update(changes)
        self.numbers.append(row)
        return row

    def ready_number(self, **changes) -> dict:
        return self.number(verification="owner_attested", permission="granted", gate={"ok": True, "reason": None, "hold": None},
                           **changes)

    @staticmethod
    def render(template: dict, variables) -> str:
        text = template["previewText"]
        for index, name in enumerate(template["parameterNames"]):
            text = text.replace("{{" + name + "}}", variables[name] if isinstance(variables, dict) else variables[index])
        return text

    def handler(self, request: httpx.Request) -> httpx.Response:
        stamp, body = request.headers["X-GHQ-Timestamp"], request.content
        expected = hmac.new(SECRET.encode(), f"{stamp}\nPOST\n/api/group-hq/inbox/crm\n".encode() + body, hashlib.sha256).hexdigest()
        assert hmac.compare_digest(expected, request.headers["X-GHQ-Signature"]), "unsigned request reached Relaunch72"
        data = json.loads(body)
        action, brand, contact = data["action"], data["brand"], data.get("contactId")
        assert str(uuid.UUID(data["requestId"])) == data["requestId"] and data["actorEmail"]
        self.calls.append((action, data))
        if action in self.broken:
            raise httpx.ConnectError("no answer")
        if action == "contact":
            return httpx.Response(200, json={"brand": brand, "contactId": contact, "workspaceId": WORKSPACE,
                                             "contact": {"name": self.name, "lifecycle": "lead"}, "activities": []})
        assert action.startswith("whatsappOwner")
        if not self.enabled:
            return httpx.Response(409, json={"error": "owner_send_unavailable"})
        result = self.refuse[action] if action in self.refuse else getattr(self, action)(data)
        if isinstance(result, httpx.Response):
            return result
        if result.get("state") == "refused" and "message" not in result and result["reason"] in MESSAGES:
            result["message"] = MESSAGES[result["reason"]]
        return httpx.Response(200, json={"brand": brand, "workspaceId": WORKSPACE, "sender": "Martin Howard Property",
                                         "result": result})

    def whatsappOwnerState(self, data):
        return {"state": "ok", "sender": {"connected": self.connected, "name": "Martin Howard Property"},
                "dailyCap": {"limit": 20, "used": 20 - self.remaining, "remaining": self.remaining},
                "accountHold": self.account_hold, "numbers": self.numbers, "candidates": self.candidates,
                "sends": sorted(self.sends.values(), key=lambda s: s["createdAt"], reverse=True)}

    def whatsappOwnerTemplates(self, data):
        return {"connected": True, "bindingId": str(uuid.uuid4()), "templates": self.templates}

    def whatsappOwnerPreview(self, data):
        template = next((t for t in self.templates if t["metaTemplateId"] == data["metaTemplateId"]), None)
        if template is None:
            return {"state": "refused", "reason": "template_not_approved"}
        text = self.render(template, data["variables"])
        return {"state": "ok", "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "metaTemplateId": template["metaTemplateId"]}

    def whatsappOwnerLink(self, data):
        assert ("pointId" in data) != ("number" in data)
        if "number" in data and not data["number"].startswith("+"):
            return {"state": "refused", "reason": "number_not_international"}
        row = self.number(numberSource="owner_entered" if "number" in data else "crm_point")
        return {"state": "linked", "linkId": row["linkId"], "number": MASKED, "created": True, "mirrorCreated": True}

    def whatsappOwnerPermission(self, data):
        row = next(n for n in self.numbers if n["linkId"] == data["linkId"])
        if row["verification"] == "unverified" and data["attestNumber"] is not True:
            return {"state": "refused", "reason": "number_unverified"}
        if not 8 <= len(data["note"]) <= 500:
            return {"state": "refused", "reason": "evidence_note_invalid"}
        self.permissions.append(data)
        row.update(verification="owner_attested", permission="granted", gate={"ok": True, "reason": None, "hold": None})
        return {"state": "granted", "consentEventId": str(uuid.uuid4()), "disposition": "applied", "verification": "owner_attested"}

    def _queue(self, data, kind, text, same):
        key = data["idempotencyKey"]
        prior = self.sends.get(key)
        if prior is not None:
            if not same(prior):
                return httpx.Response(409, json={"error": "request_conflict"})
            return {"state": prior["state"], "sendId": key, "replayed": True, "providerMessageId": prior["providerMessageId"]}
        row = next(n for n in self.numbers if n["linkId"] == data["linkId"])
        if kind == "template" and row["gate"]["ok"] is not True:
            return {"state": "refused", "reason": row["gate"]["reason"]}
        self.sends[key] = {"sendId": key, "kind": kind, "state": "queued", "stateReason": None, "text": text,
                           "providerMessageId": None, "errorCode": None, "number": MASKED, "receipts": [],
                           "createdAt": f"2026-11-12T10:{len(self.sends):02d}:00+00:00", "originContactId": data["contactId"],
                           "request": data}
        return {"state": "queued", "sendId": key, "replayed": False}

    def whatsappOwnerSendTemplate(self, data):
        template = next(t for t in self.templates if t["metaTemplateId"] == data["metaTemplateId"])
        text = self.render(template, data["variables"])
        if hashlib.sha256(text.encode()).hexdigest() != data["previewSha256"]:
            return {"state": "refused", "reason": "preview_changed"}
        return self._queue(data, "template", text, lambda prior: prior["request"]["previewSha256"] == data["previewSha256"]
                           and prior["request"]["variables"] == data["variables"] and prior["request"]["linkId"] == data["linkId"])

    def whatsappOwnerSendText(self, data):
        if not self.manual_replies:
            return httpx.Response(409, json={"error": "reply_unavailable"})
        return self._queue(data, "text", data["body"], lambda prior: prior["request"].get("body") == data["body"])

    def whatsappOwnerStatus(self, data):
        send = self.sends.get(data["sendId"])
        if send is None:
            return {"state": "not_found"}
        return {k: v for k, v in send.items() if k != "request"} | {"originWorkspaceId": WORKSPACE}


@pytest.fixture
def r72(monkeypatch):
    """Relaunch72's boundary behind the real signing client: every request Growth HQ makes lands in fake.calls."""
    fake = Relaunch72()
    real = httpx.Client
    monkeypatch.setattr(config, "R72_GROUP_HQ_INBOX_SECRET", SECRET)
    monkeypatch.setattr(inbox_crm.httpx, "Client",
                        lambda **options: real(**{**options, "transport": httpx.MockTransport(fake.handler)}))
    return fake


@pytest.fixture
def lead():
    """A Property Predator CRM contact Growth HQ already knows by name."""
    with session() as db:
        row = CampaignLead(brand="pp", source_system="relaunch72", external_id=str(uuid.uuid4()), display_name="Dana Buyer",
                           lifecycle="lead")
        db.add(row)
        db.commit()
        return {"id": row.id, "contact": row.external_id}


def target(lead, **more) -> dict:
    return {"brand": "pp", "contact": lead["contact"], **more}


def fields(html: str, action: str) -> dict:
    """The hidden and filled fields of the form that posts to action, as a browser would send them."""
    form = re.search(rf'<form method="post" action="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S)
    assert form, f"no form posts to {action}"
    data: dict = {}
    for tag in re.findall(r"<input[^>]*>", form.group(1)):
        name, value, kind = (re.search(rf'{key}="([^"]*)"', tag) for key in ("name", "value", "type"))
        if name and (kind is None or kind.group(1) == "hidden"):
            data.setdefault(name.group(1), []).append(unescape(value.group(1)) if value else "")
    return {k: v if len(v) > 1 else v[0] for k, v in data.items()}


def audit_rows() -> list[AuditLog]:
    with session() as db:
        return list(db.scalars(select(AuditLog).where(AuditLog.action.like("whatsapp.send.%")).order_by(AuditLog.id)))


def checked(admin_client, r72, lead, link, **message) -> tuple[str, dict]:
    """Step 3 for an approved message with one blank: the page and its Send form."""
    page = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", template=HELLO["metaTemplateId"],
                             layout="NAMED", name="first_name", blank=message.get("blank", "Dana")))
    assert page.status_code == 200
    return page.text, fields(page.text, PAGE)


# -- who may open it -----------------------------------------------------------------------------------------------

def test_the_screen_is_for_a_signed_in_admin_only(client, r72, lead):
    anonymous = TestClient(app, follow_redirects=False)
    assert anonymous.get(PAGE, params=target(lead)).headers["location"].startswith("/login")
    for path in ("", "/check", "/number"):
        response = anonymous.post(PAGE + path, data=target(lead, link=str(uuid.uuid4()), kind="text", body="Hello",
                                                           request_id=str(uuid.uuid4())))
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
    assert r72.calls == [] and audit_rows() == []


def test_a_post_from_another_site_is_blocked_before_anything_is_asked(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    for path in ("", "/check", "/number"):
        response = admin_client.post(PAGE + path, headers={"Origin": "https://elsewhere.example"},
                                     data=target(lead, link=link, kind="text", body="Hello", request_id=str(uuid.uuid4()),
                                                 number="+447700900126", confirm="yes"))
        assert response.status_code == 403
    assert r72.calls == [] and audit_rows() == []
    same_site = admin_client.post(PAGE + "/number", headers={"Origin": "http://testserver"},
                                  data=target(lead, number="+447700900126", confirm="yes"))
    assert same_site.status_code == 303 and r72.actions() == ["whatsappOwnerLink"]


def test_an_unknown_business_or_contact_is_not_found(admin_client, r72, lead):
    assert admin_client.get(PAGE, params={"brand": "hnh", "contact": lead["contact"]}).status_code == 404
    assert admin_client.get(PAGE, params={"brand": "pp", "contact": "not-a-contact"}).status_code == 404
    assert admin_client.post(PAGE, data={"brand": "pp", "contact": "1 OR 1=1"}).status_code == 404
    assert r72.calls == []


# -- the switch is off (production today) --------------------------------------------------------------------------

def test_switched_off_says_so_and_offers_nothing_to_press(admin_client, r72, lead):
    r72.enabled = False
    page = admin_client.get(PAGE, params=target(lead))
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "WhatsApp sending isn't switched on yet" in unescape(page.text)
    assert "nothing has been sent" in page.text and "Dana Buyer" in page.text
    main = page.text.split('<main class="app-main">')[1]
    assert "<form" not in main and "<button" not in main and "<input" not in main
    assert "Step 1" not in main and "couldn't reach" not in unescape(main)
    assert r72.actions() == ["whatsappOwnerState"]           # one question, and no second for a name it already has


def test_switched_off_when_send_is_pressed_sends_nothing(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    r72.enabled = False
    response = admin_client.post(PAGE, data=form)
    assert response.status_code == 200 and "WhatsApp sending isn't switched on yet" in unescape(response.text)
    assert "Try again" not in response.text and r72.sends == {}
    assert [row.action for row in audit_rows()] == ["whatsapp.send.begin", "whatsapp.send.refused"]
    assert audit_rows()[1].after["reason"] == "owner_send_unavailable"


# -- step 1: who -----------------------------------------------------------------------------------------------------

def test_no_number_asks_for_one_with_the_confirmation(admin_client, r72, lead):
    page = admin_client.get(PAGE, params=target(lead))
    text = unescape(page.text)
    assert "There is no WhatsApp number for Dana Buyer yet" in text and "Their WhatsApp number" in text
    assert "I have checked this is their WhatsApp number" in text and "Use this number" in text
    assert "Step 2" not in text and r72.actions() == ["whatsappOwnerState"]

    unticked = admin_client.post(PAGE + "/number", data=target(lead, number="+44 7700 900126"))
    assert unticked.status_code == 303 and "Tick+the+box" in unticked.headers["location"]
    empty = admin_client.post(PAGE + "/number", data=target(lead, confirm="yes"))
    assert "country+code" in empty.headers["location"]
    assert r72.actions() == ["whatsappOwnerState"]           # neither reached Relaunch72

    local = admin_client.post(PAGE + "/number", data=target(lead, number="07700 900126", confirm="yes"))
    assert "country+code" in local.headers["location"] and r72.numbers == []
    linked = admin_client.post(PAGE + "/number", data=target(lead, number="+44 7700 900126", confirm="yes"))
    assert linked.status_code == 303
    action, sent = r72.calls[-1]
    assert action == "whatsappOwnerLink" and sent["number"] == "+44 7700 900126" and sent["contactId"] == lead["contact"]
    assert "pointId" not in sent
    query = parse_qs(urlsplit(linked.headers["location"]).query)
    assert query["link"] == [r72.numbers[0]["linkId"]] and "900126" not in linked.headers["location"]
    with session() as db:
        user = db.scalars(select(User)).first()
        assert whatsapp_send.was_checked(user, query["link"][0], query["checked"][0])
        assert not whatsapp_send.was_checked(user, str(uuid.uuid4()), query["checked"][0])
    rows = audit_rows()
    assert [row.action for row in rows] == ["whatsapp.send.number"] * 2
    assert rows[-1].after == {"from": "typed", "linked": True, "reason": None} and "7700" not in json.dumps(rows[-1].after)


def test_a_number_on_the_crm_record_can_be_picked(admin_client, r72, lead):
    point = str(uuid.uuid4())
    r72.candidates = [{"pointId": point, "kind": "phone", "number": MASKED, "usable": True, "linked": False},
                      {"pointId": str(uuid.uuid4()), "kind": "phone", "number": None, "usable": False, "linked": False}]
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert f'name="point" value="{point}" checked' in page and MASKED in page
    assert "Or type a different number" in page and "couldn't be read" in page
    admin_client.post(PAGE + "/number", data=target(lead, point=point, confirm="yes"))
    action, sent = r72.calls[-1]
    assert action == "whatsappOwnerLink" and sent["pointId"] == point and "number" not in sent


@pytest.mark.parametrize("reason, sentence", [
    ("opted_out", "This person has opted out of WhatsApp. Nothing can be sent."),
    ("whatsapp_stop", "This person replied STOP on WhatsApp. Nothing can be sent."),
    ("suppressed", "This number is on the do-not-message list. Nothing can be sent."),
    ("permission_withdrawn", "Permission to WhatsApp this person was withdrawn. Nothing can be sent."),
])
def test_opted_out_stops_at_step_one(admin_client, r72, lead, reason, sentence):
    r72.number(blocked=reason, window={"open": True}, gate={"ok": False, "reason": reason, "hold": None})
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert sentence in page and MASKED in page
    assert "Step 2" not in page and "Check this message" not in page and "Send this message" not in page
    assert r72.actions() == ["whatsappOwnerState"]


def test_a_pause_is_shown_with_its_reason_and_when_it_ends(admin_client, r72, lead):
    r72.ready_number()
    r72.account_hold = {"reason": "meta_131048", "errorCode": 131048, "until": "2026-11-13T09:30:00+00:00"}
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert "WhatsApp has paused sending from the Martin Howard Property number for now." in page
    assert "too many messages were blocked or reported" in page and "You can try again after 13 Nov 2026, 09:30." in page
    assert "Step 2" not in page and r72.actions() == ["whatsappOwnerState"]

    r72.account_hold, r72.numbers = None, []
    r72.number(gate={"ok": False, "reason": "recipient_paused",
                     "hold": {"scope": "recipient", "errorCode": 131056, "until": "2026-11-12T12:00:00+00:00"}})
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert "WhatsApp has asked us not to message this person for now." in page and "12 Nov 2026, 12:00" in page
    assert "Step 2" not in page


def test_a_reason_this_screen_does_not_know_stops_with_relaunch72s_own_words(admin_client, r72, lead):
    r72.number(gate={"ok": False, "reason": "brand_new_reason", "message": "A new rule says not today.", "hold": None})
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert "A new rule says not today." in page and "Step 2" not in page

    r72.connected = False
    assert "The Martin Howard Property WhatsApp number isn't connected right now." in unescape(
        admin_client.get(PAGE, params=target(lead)).text)


def test_the_name_is_asked_for_only_when_growth_hq_does_not_hold_it(admin_client, r72):
    contact = str(uuid.uuid4())
    r72.name = "Kiran Seller"
    r72.ready_number(window={"open": True})
    page = admin_client.get(PAGE, params={"brand": "kp", "contact": contact})
    assert "Kiran Seller" in page.text and "Key Properties UK" in page.text
    assert r72.actions() == ["whatsappOwnerState", "contact"]
    assert f"/inbox/crm/contact?brand=kp&amp;contact={contact}" in page.text


# -- step 2: what ----------------------------------------------------------------------------------------------------

def test_an_open_window_offers_a_typed_reply_without_listing_approved_messages(admin_client, r72, lead):
    link = r72.number(window={"open": True, "closesAt": "2026-11-13T08:15:00+00:00"})["linkId"]
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert "so you can type a reply" in page and "You have until 13 Nov 2026, 08:15." in page
    assert 'name="body"' in page and "Check this message" in page and "Use an approved message instead" in page
    assert r72.actions() == ["whatsappOwnerState"]

    step3 = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="text", body="See you at 3pm.\nMartin"))
    text = unescape(step3.text)
    assert "This is exactly what Dana Buyer will receive." in text and "See you at 3pm.\nMartin" in text
    assert "How do you know" not in text            # a reply to their own message needs no reason
    form = fields(step3.text, PAGE)
    sent = admin_client.post(PAGE, data=form)
    assert sent.status_code == 303
    action, data = r72.calls[-1]
    assert action == "whatsappOwnerSendText" and data["body"] == "See you at 3pm.\nMartin"
    assert data["idempotencyKey"] == form["request_id"] and data["linkId"] == link
    assert "whatsappOwnerPermission" not in r72.actions() and "whatsappOwnerPreview" not in r72.actions()


def test_typed_replies_switched_off_points_to_approved_messages(admin_client, r72, lead):
    link = r72.number(window={"open": True})["linkId"]
    r72.manual_replies = False
    step3 = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="text", body="Hello"))
    response = admin_client.post(PAGE, data=fields(step3.text, PAGE))
    text = unescape(response.text)
    assert "Typed replies aren't switched on yet. Choose an approved message instead." in text
    assert "Choose an approved message</a>" in text and "mode=approved" in response.text and r72.sends == {}


def test_a_closed_window_lists_approved_messages_and_their_blanks(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert "choose one of your approved messages" in page and 'name="body"' not in page
    assert "Hello first name" in page and "Hi {{first_name}}, Martin here.\nReply STOP to opt out." in page
    assert "Viewing reminder" in page and "Your viewing is tomorrow." in page
    assert "Needs a picture" not in page             # an approved message that cannot be sent from here is not offered
    assert "You can send 20 more approved messages today." in page
    assert r72.actions() == ["whatsappOwnerState", "whatsappOwnerTemplates"]
    for jargon in ("900100200300\"", "template", "wamid", "lawful", "legitimate", "authority", "Meta"):
        assert jargon not in page.split('<main class="app-main">')[1].replace("message=900100200300", "")

    chosen = unescape(admin_client.get(PAGE, params=target(lead, link=link, message=HELLO["metaTemplateId"])).text)
    assert 'name="blank"' in chosen and "First name" in chosen and "Fill in the gaps" in chosen
    assert fields(chosen, PAGE + "/check") == target(lead, link=link, kind="template", checked="", blank="",
                                                     template=HELLO["metaTemplateId"], layout="NAMED", name="first_name")
    plain_one = unescape(admin_client.get(PAGE, params=target(lead, link=link, message=PLAIN_ONE["metaTemplateId"])).text)
    assert "This message goes exactly as written." in plain_one and 'name="blank"' not in plain_one


def test_an_empty_blank_is_caught_before_relaunch72_is_asked_to_preview(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    page = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NAMED",
                             template=HELLO["metaTemplateId"], name="first_name", blank="   "))
    assert page.status_code == 200 and "Fill in every blank." in page.text and "Send this message" not in page.text
    untouched = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NAMED",
                                  template=HELLO["metaTemplateId"], name="first_name", blank=""))
    assert untouched.status_code == 200 and "Fill in every blank." in untouched.text
    assert "whatsappOwnerPreview" not in r72.actions()
    # What was typed comes back in the blank, so a long message is not lost to one mistake.
    r72.refuse["whatsappOwnerPreview"] = {"state": "refused", "reason": "variables_invalid"}
    refused = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NAMED",
                                template=HELLO["metaTemplateId"], name="first_name", blank="Dana     B"))
    assert "WhatsApp doesn't allow new lines or long gaps inside a blank." in unescape(refused.text)
    assert 'value="Dana     B"' in refused.text and "Send this message" not in refused.text


def test_the_daily_limit_is_shown_in_plain_words(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    r72.refuse["whatsappOwnerSendTemplate"] = {"state": "refused", "reason": "daily_cap_reached", "limit": 20}
    response = unescape(admin_client.post(PAGE, data=form).text)
    assert "That message was not sent" in response
    assert "You've reached today's limit of 20 approved messages. Try again tomorrow." in response
    assert "templates" not in response and "Try again</button>" not in response and "Check the message again" not in response
    assert r72.sends == {}

    r72.refuse["whatsappOwnerSendTemplate"] = {"state": "refused", "reason": "recipient_recently_messaged"}
    _, form = checked(admin_client, r72, lead, link)
    response = unescape(admin_client.post(PAGE, data=form).text)
    assert ("This person was sent an approved message in the last 24 hours. Wait for their reply, or try again tomorrow."
            in response)
    assert [row.after["reason"] for row in audit_rows() if row.action == "whatsapp.send.refused"] == [
        "daily_cap_reached", "recipient_recently_messaged"]

    r72.remaining = 0                                # and once the day is used up, the list is not even asked for
    r72.calls.clear()
    page = unescape(admin_client.get(PAGE, params=target(lead)).text)
    assert "You've reached today's limit of 20 approved messages. Try again tomorrow." in page
    assert "Use this message" not in page and r72.actions() == ["whatsappOwnerState"]


# -- step 3: check, then send ----------------------------------------------------------------------------------------

def test_the_check_step_shows_relaunch72s_exact_preview_and_sends_nothing(admin_client, r72, lead):
    link = r72.number()["linkId"]
    page, form = checked(admin_client, r72, lead, link)
    text = unescape(page)
    assert "Hi Dana, Martin here.\nReply STOP to opt out." in text
    assert f"Dana Buyer · {MASKED}" in text and "The Martin Howard Property WhatsApp number" in text
    assert "Send this message" in text and 'data-submit-once="Sending…"' in text
    assert form["sha"] == hashlib.sha256("Hi Dana, Martin here.\nReply STOP to opt out.".encode()).hexdigest()
    assert str(uuid.UUID(form["request_id"])) == form["request_id"]
    assert re.findall(r'name="reason" value="([a-z_]+)"', page) == ["existing_client", "business_introduction"]
    assert "They are an existing or recent client" in text and "This is a one-off business introduction" in text
    assert "I have checked this is their WhatsApp number" in text and "How do you know Dana Buyer?" in text
    action, data = r72.calls[-1]
    assert action == "whatsappOwnerPreview"
    assert data["metaTemplateId"] == HELLO["metaTemplateId"] and data["variables"] == {"first_name": "Dana"}
    assert r72.actions() == ["whatsappOwnerState", "whatsappOwnerPreview"]
    assert r72.sends == {} and r72.permissions == [] and audit_rows() == []

    again, second = checked(admin_client, r72, lead, link)
    assert second["request_id"] != form["request_id"]          # a new request id each time the button is rendered


def test_a_preview_that_does_not_match_its_own_digest_is_not_shown(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    r72.refuse["whatsappOwnerPreview"] = {"state": "ok", "text": "Something else", "sha256": "0" * 64}
    page = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NAMED",
                             template=HELLO["metaTemplateId"], name="first_name", blank="Dana"))
    assert "Send this message" not in page.text and "Something else" not in page.text
    assert "We couldn't reach the CRM just now" in unescape(page.text)


def test_send_records_how_the_owner_knows_them_then_queues_once(admin_client, r72, lead):
    link = r72.number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    form |= {"reason": "existing_client", "note": "  Bought a flat through us in March  ", "confirm": "yes"}
    r72.calls.clear()
    response = admin_client.post(PAGE, data=form)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["sent"] == [form["request_id"]] and query["contact"] == [lead["contact"]]
    (first, permission), (second, queued) = r72.calls
    assert first == "whatsappOwnerPermission" and second == "whatsappOwnerSendTemplate"
    assert {k: permission[k] for k in ("contactId", "linkId", "decision", "lawfulBasis", "evidenceKind", "note", "attestNumber")} == {
        "contactId": lead["contact"], "linkId": link, "decision": "granted", "lawfulBasis": "legitimate_interests",
        "evidenceKind": "existing_client", "note": "Bought a flat through us in March", "attestNumber": True}
    assert str(uuid.UUID(permission["commandId"])) == permission["commandId"] != form["request_id"]
    assert {k: queued[k] for k in ("contactId", "linkId", "idempotencyKey", "metaTemplateId", "variables", "previewSha256")} == {
        "contactId": lead["contact"], "linkId": link, "idempotencyKey": form["request_id"],
        "metaTemplateId": HELLO["metaTemplateId"], "variables": {"first_name": "Dana"}, "previewSha256": form["sha"]}
    assert list(r72.sends) == [form["request_id"]]

    begin, done = audit_rows()
    assert (begin.action, done.action) == ("whatsapp.send.begin", "whatsapp.send.queued")
    assert begin.entity == done.entity == "whatsapp-send:" + form["request_id"] and begin.actor == "founder@example.test"
    assert begin.after == {"brand": "pp", "contact": lead["contact"], "kind": "approved message",
                           "message_sha256": form["sha"], "how_known": "existing_client"}
    stored = json.dumps([begin.after, done.after])
    for private in ("Dana", "Martin here", "0126", "Bought a flat"):
        assert private not in stored


def test_the_status_line_only_repeats_what_relaunch72_reports(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    after = admin_client.post(PAGE, data=form).headers["location"]
    send = r72.sends[form["request_id"]]
    r72.calls.clear()

    def line() -> str:
        return unescape(admin_client.get(after).text).split('<main class="app-main">')[1]

    page = line()
    assert "<h2>Queued</h2>" in page and "<h2>Sent</h2>" not in page and "Check again" in page
    assert "Hi Dana, Martin here." in page and "Send this message" not in page and "Step 2" not in page
    assert r72.actions() == ["whatsappOwnerState", "whatsappOwnerStatus"] and r72.calls[1][1]["sendId"] == form["request_id"]
    send["state"] = "calling"
    assert "<h2>Sending now</h2>" in line() and "<h2>Sent</h2>" not in line()
    send.update(state="accepted", providerMessageId="wamid.TESTONLY")
    page = line()
    assert "<h2>Sent</h2>" in page and "It has not reached their phone yet." in page and "wamid" not in page
    send["state"] = "delivered"
    assert "<h2>Delivered</h2>" in line()
    send["state"] = "read"
    page = line()
    assert "<h2>Read</h2>" in page and "Check again" not in page
    send.update(state="failed", errorCode=131026)
    page = line()
    assert "<h2>Not sent</h2>" in page and "This number may not be on WhatsApp." in page
    send.update(state="expired", errorCode=None, stateReason="authority_expired")
    assert "It wasn't picked up in time, so nothing was sent." in line()
    # Growth HQ kept nothing of its own that says sent: only its audit trail of the attempt.
    with session() as db:
        assert db.scalars(select(InboxReply)).all() == []
    assert [row.action for row in audit_rows()] == ["whatsapp.send.begin", "whatsapp.send.queued"]


@pytest.mark.parametrize("state", ["outcome_unknown", "a_state_from_the_future"])
def test_an_unknown_outcome_says_do_not_resend_and_never_sent(admin_client, r72, lead, state):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    after = admin_client.post(PAGE, data=form).headers["location"]
    r72.sends[form["request_id"]]["state"] = state
    page = unescape(admin_client.get(after).text).split('<main class="app-main">')[1]
    assert "<h2>Not confirmed by WhatsApp — do not resend yet</h2>" in page
    assert "Check the conversation on the phone before sending anything else." in page
    assert "<h2>Sent</h2>" not in page and ">Sent<" not in page and "Delivered" not in page
    assert ">Not confirmed by WhatsApp — do not resend yet</span>" in page            # and the same in the history


def test_pressing_send_twice_sends_once(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    first = admin_client.post(PAGE, data=form)
    second = admin_client.post(PAGE, data=form)
    assert first.status_code == second.status_code == 303 and first.headers["location"] == second.headers["location"]
    assert list(r72.sends) == [form["request_id"]] and r72.actions().count("whatsappOwnerSendTemplate") == 2
    assert [row.action for row in audit_rows()] == ["whatsapp.send.begin", "whatsapp.send.queued",
                                                    "whatsapp.send.begin", "whatsapp.send.replayed"]

    # The same request id with a different message is refused, and still nothing more is sent.
    other, _ = checked(admin_client, r72, lead, link, blank="Someone Else")
    changed = fields(other, PAGE) | {"request_id": form["request_id"]}
    refused = admin_client.post(PAGE, data=changed)
    assert refused.status_code == 200
    assert "That send was already used for a different message. Check the message again." in unescape(refused.text)
    assert list(r72.sends) == [form["request_id"]] and r72.sends[form["request_id"]]["text"].startswith("Hi Dana")
    assert audit_rows()[-1].action == "whatsapp.send.refused" and audit_rows()[-1].after["reason"] == "request_conflict"


def test_a_form_this_screen_did_not_render_is_refused_before_anything_is_asked(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    r72.calls.clear()
    for change in ({"request_id": "not-a-request"}, {"request_id": form["request_id"].upper()}, {"sha": "abc"},
                   {"kind": "broadcast"}, {"link": "x"}, {"layout": "POSITIONAL", "name": "9 9"}, {"blank": ""},
                   {"reason": "asked_to_be_contacted"}, {"reason": "written_consent"}):
        assert admin_client.post(PAGE, data=form | change).status_code == 400
    assert r72.calls == [] and audit_rows() == []


def test_the_reason_and_the_number_check_are_needed_before_a_first_message(admin_client, r72, lead):
    link = r72.number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    r72.calls.clear()
    short = admin_client.post(PAGE, data=form | {"reason": "business_introduction", "note": "hi", "confirm": "yes"})
    assert "Add a short note (8 to 500 characters, on one line)" in unescape(short.text)
    unticked = admin_client.post(PAGE, data=form | {"reason": "business_introduction", "note": "Introduced by her solicitor"})
    assert "Tick the box to say you have checked this is their WhatsApp number." in unescape(unticked.text)
    assert "Check the message again" in unticked.text and r72.calls == [] and audit_rows() == []

    # Left out altogether, Relaunch72 is the one that says no; the screen then asks again rather than sending.
    bare = {k: v for k, v in form.items() if k != "needs_tick"}
    refused = admin_client.post(PAGE, data=bare)
    assert "Tick the box to say you have checked this is their WhatsApp number." in unescape(refused.text)
    assert r72.sends == {} and r72.permissions == []
    recheck = admin_client.post(PAGE + "/check", data=fields(refused.text, PAGE + "/check"))
    assert "How do you know Dana Buyer?" in unescape(recheck.text)


def test_a_number_confirmed_when_it_was_added_is_not_asked_about_twice(admin_client, r72, lead):
    linked = admin_client.post(PAGE + "/number", data=target(lead, number="+447700900126", confirm="yes"))
    query = parse_qs(urlsplit(linked.headers["location"]).query)
    link, token = query["link"][0], query["checked"][0]
    page = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NONE", checked=token,
                                                          template=PLAIN_ONE["metaTemplateId"]))
    text = unescape(page.text)
    assert "You have confirmed this is their WhatsApp number." in text and 'name="confirm"' not in text
    form = fields(page.text, PAGE) | {"reason": "business_introduction", "note": "Introduced by her solicitor"}
    assert admin_client.post(PAGE, data=form).status_code == 303
    assert r72.permissions[0]["attestNumber"] is True and r72.permissions[0]["evidenceKind"] == "business_introduction"
    # Someone else's proof, or a made-up one, confirms nothing.
    r72.numbers[0].update(verification="unverified", gate={"ok": False, "reason": "number_unverified", "hold": None})
    forged = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NONE", checked="made-up",
                                                            template=PLAIN_ONE["metaTemplateId"]))
    assert 'name="confirm"' in forged.text and "You have confirmed" not in forged.text


# -- when Relaunch72 does not answer ---------------------------------------------------------------------------------

def test_crm_unavailable_on_opening_the_page(admin_client, r72, lead):
    r72.broken.add("whatsappOwnerState")
    page = admin_client.get(PAGE, params=target(lead))
    text = unescape(page.text)
    assert page.status_code == 200 and "We couldn't reach the CRM just now, so nothing was sent. Try again in a minute." in text
    main = page.text.split('<main class="app-main">')[1]
    assert "<form" not in main and "Step 1" not in main and "switched on" not in main

    r72.broken.clear()
    r72.refuse["whatsappOwnerState"] = httpx.Response(500, json={"error": "temporarily_unavailable"})
    assert "We couldn't reach the CRM just now" in unescape(admin_client.get(PAGE, params=target(lead)).text)
    r72.refuse["whatsappOwnerState"] = httpx.Response(403, json={"error": "forbidden_scope"})
    assert "Your account isn't set up to send WhatsApp messages for this business." in unescape(
        admin_client.get(PAGE, params=target(lead)).text)
    r72.refuse["whatsappOwnerState"] = {"state": "refused", "reason": "contact_not_found"}
    assert "This contact could not be found. It may have been archived." in unescape(
        admin_client.get(PAGE, params=target(lead)).text)


def test_crm_unavailable_while_sending_can_be_retried_without_sending_twice(admin_client, r72, lead):
    link = r72.ready_number()["linkId"]
    _, form = checked(admin_client, r72, lead, link)
    r72.broken.add("whatsappOwnerSendTemplate")
    response = admin_client.post(PAGE, data=form)
    text = unescape(response.text)
    assert response.status_code == 200 and "We couldn't confirm that" in text
    assert "the same message will not be sent twice" in text and "<h2>Sent</h2>" not in text and "Queued" not in text
    retry = fields(response.text, PAGE)
    assert retry["request_id"] == form["request_id"] and retry["sha"] == form["sha"] and retry["blank"] == "Dana"
    assert [row.action for row in audit_rows()] == ["whatsapp.send.begin", "whatsapp.send.unconfirmed"]

    r72.broken.clear()
    assert admin_client.post(PAGE, data=retry).status_code == 303
    assert admin_client.post(PAGE, data=retry).status_code == 303
    assert list(r72.sends) == [form["request_id"]]

    r72.refuse["whatsappOwnerSendTemplate"] = httpx.Response(502, text="bad gateway")
    _, form = checked(admin_client, r72, lead, link)
    assert "We couldn't confirm that" in unescape(admin_client.post(PAGE, data=form).text)
    r72.refuse["whatsappOwnerSendTemplate"] = {"state": "queued", "sendId": str(uuid.uuid4()), "replayed": False}
    assert "We couldn't confirm that" in unescape(admin_client.post(PAGE, data=form).text)      # not the send we asked for


# -- hostile text ----------------------------------------------------------------------------------------------------

def test_names_and_message_text_are_escaped_everywhere(admin_client, r72, lead):
    hostile = '<script>alert("x")</script>'
    with session() as db:
        db.get(CampaignLead, lead["id"]).display_name = f"Dana {hostile}"
        db.commit()
    r72.templates = [dict(HELLO, name="hello<b>", previewText="Hi {{first_name}} <img src=x onerror=alert(1)>",
                          bodyText="Hi {{first_name}} <img src=x onerror=alert(1)>")]
    row = r72.ready_number(number='+44 <i>•••</i> 0126')
    link = row["linkId"]
    listing = admin_client.get(PAGE, params=target(lead)).text
    page = admin_client.post(PAGE + "/check", data=target(lead, link=link, kind="template", layout="NAMED",
                             template=HELLO["metaTemplateId"], name="first_name", blank='"><svg onload=alert(2)>'))
    form = fields(page.text, PAGE)
    assert form["blank"] == '"><svg onload=alert(2)>'
    after = admin_client.get(admin_client.post(PAGE, data=form).headers["location"]).text
    r72.broken.add("whatsappOwnerSendTemplate")
    problem = admin_client.post(PAGE, data=form | {"request_id": str(uuid.uuid4())}).text
    r72.enabled = False
    off = admin_client.get(PAGE, params=target(lead)).text
    for html in (listing, page.text, after, problem, off):
        assert "<script>alert" not in html and "<img src=x" not in html and "<svg" not in html and "<i>" not in html
        assert "<b>" not in html and "&lt;script&gt;alert" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in listing and "&lt;svg onload=alert(2)&gt;" in page.text
    assert "&lt;svg onload=alert(2)&gt;" in after             # the history, as Relaunch72 holds it


# -- the ways in -----------------------------------------------------------------------------------------------------

def test_the_ways_in_all_open_the_same_page(admin_client, r72, lead, monkeypatch):
    href = f'href="{PAGE}?brand=pp&amp;contact={lead["contact"]}"'
    assert href in admin_client.get(f"/crm/leads/{lead['id']}").text
    with session() as db:
        followed = LapsDeal(name="Dana Buyer", brand="pp", stage="lead", origin=f"crm:{lead['id']}")
        by_hand = LapsDeal(name="Walk-in", brand="pp", stage="lead")
        db.add_all([followed, by_hand])
        db.commit()
        followed_id, by_hand_id = followed.id, by_hand.id
    assert href in admin_client.get(f"/laps/deals/{followed_id}").text
    assert "Send a WhatsApp" not in admin_client.get(f"/laps/deals/{by_hand_id}").text
    contact = admin_client.get("/inbox/crm/contact", params={"brand": "pp", "contact": lead["contact"]})
    assert href in contact.text and "Send a WhatsApp" in contact.text
    assert r72.actions() == ["contact"]              # the buttons are links: none of these pages asked about WhatsApp
    assert whatsapp_send.BRANDS.keys() == {"pp", "mhp", "kp", "piuk"}


def test_an_inbox_conversation_linked_to_a_crm_contact_offers_the_page(admin_client, db, monkeypatch):
    from app.services import inbox
    account_id, thread, person, message = (str(uuid.uuid4()) for _ in range(4))
    monkeypatch.setattr(config, "GHQ_MHP_WHATSAPP_INBOX_ID", account_id)
    monkeypatch.setattr(config, "R72_GROUP_HQ_INBOX_SECRET", SECRET)
    asked = []

    def read(actor, brand, action, **data):
        asked.append(action)
        common = dict(brand=brand, accountId=account_id)
        if action == "whatsappThreads":
            return dict(common, hasMore=False, threads=[dict(id=thread, participantId=person, name="Durham owner",
                                                             preview="Received", updatedAt="2026-11-07T11:00:00Z")])
        return dict(common, threadId=thread, nextCursor=None, messages=[dict(
            id=message, participantId=person, name="Durham owner", body="Hello", occurredAt="2026-11-07T11:00:00Z")])

    monkeypatch.setattr(inbox_crm, "command", read)
    user = db.scalars(select(User)).first()
    _, _, threads = inbox.snapshot(user.id, ["mhp-whatsapp"], "mhp", "whatsapp", db=db, user_email=user.email)
    page = admin_client.get("/inbox", params={"account": "mhp-whatsapp", "thread": threads[0]["ticket"]})
    assert f'href="{PAGE}?brand=mhp&amp;contact={person}">Send a WhatsApp</a>' in page.text
    assert not any(action.startswith("whatsappOwner") for action in asked)
    # A conversation linked to another business's contact gets the same link; a business with no CRM gets none.
    from app.templating import templates
    link = templates.env.from_string("{% for l in links %}{{ whatsapp_send_url(l.brand, l.contact_id) }}|{% endfor %}")
    assert link.render(links=[{"brand": "kp", "contact_id": person}, {"brand": "hnh", "contact_id": person},
                              {"brand": "kp", "contact_id": "x"}]) == f"{PAGE}?brand=kp&amp;contact={person}|||"
    source = (config.APP_DIR / "templates" / "inbox.html").read_text()
    assert source.count("whatsapp_send_url(link.brand, link.contact_id)") == 2


# -- the client ------------------------------------------------------------------------------------------------------

def test_the_client_tells_a_definite_no_from_no_answer(db, r72):
    user = db.scalars(select(User)).first()
    contact = str(uuid.uuid4())
    assert inbox_crm.command(user, "pp", "whatsappOwnerState", contactId=contact)["state"] == "ok"
    r72.enabled = False
    with pytest.raises(inbox_crm.OwnerWhatsAppRefused) as refusal:
        inbox_crm.command(user, "pp", "whatsappOwnerState", contactId=contact)
    assert refusal.value.code == "owner_send_unavailable" and isinstance(refusal.value, inbox_crm.CrmUnavailable)
    r72.enabled = True
    for answer in (httpx.Response(500, json={"error": "temporarily_unavailable"}), httpx.Response(200, json=[]),
                   httpx.Response(200, json={"brand": "kp", "workspaceId": WORKSPACE, "result": {}}),
                   httpx.Response(200, json={"brand": "pp", "workspaceId": WORKSPACE, "result": "queued"}),
                   httpx.Response(429, json={"error": "slow_down"})):
        r72.refuse["whatsappOwnerState"] = answer
        with pytest.raises(inbox_crm.CrmUnavailable) as failure:
            inbox_crm.command(user, "pp", "whatsappOwnerState", contactId=contact)
        assert not isinstance(failure.value, inbox_crm.OwnerWhatsAppRefused)
    r72.refuse["whatsappOwnerState"] = httpx.Response(409, text="<html>conflict</html>")
    with pytest.raises(inbox_crm.OwnerWhatsAppRefused) as refusal:
        inbox_crm.command(user, "pp", "whatsappOwnerState", contactId=contact)
    assert refusal.value.code == "http_409"


def test_plain_words_never_use_the_engines_vocabulary():
    for sentence in [*whatsapp_send.PLAIN.values(), *whatsapp_send.STATES.values(), *whatsapp_send.STATE_HELP.values(),
                     *whatsapp_send.FAILURES.values(), *whatsapp_send.REASONS.values(), whatsapp_send.UNCONFIRMED,
                     whatsapp_send.UNAVAILABLE, whatsapp_send.CONFIRM_NUMBER]:
        lowered = sentence.lower()
        for word in ("template", "wamid", "authority", "lawful", "legitimate", "meta ", "idempot", "sha256", "relaunch"):
            assert word not in lowered, sentence
    assert whatsapp_send.describe({"state": "queued"})["label"] == "Queued"
    assert {whatsapp_send.describe({"state": s})["label"] for s in ("accepted", "sent")} == {"Sent"}
    assert whatsapp_send.describe({"state": None})["label"] == whatsapp_send.NOT_CONFIRMED
