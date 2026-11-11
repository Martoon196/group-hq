"""The operator API's Inbox and WhatsApp tools (app.services.operator_inbox), over POST /mcp with the static bearer.

Three classes of tool: read tools are always offered, write tools only while writes are enabled, messaging tools (the
only ones that cause a message to reach a real person) only while writes AND messaging are enabled. For every tool: a
happy path that checks the answer and the database, and a refusal that comes back as the screen's own sentence with
isError and changes nothing. A messaging tool called while messaging is off sends nothing: no provider call, no
connector job and no receipt row. Every provider here is a stand-in (the fixtures of tests/test_inbox.py,
tests/test_whatsapp_keywords.py, tests/test_personal_whatsapp.py and tests/test_r72_whatsapp_inbox.py); nothing real
is called."""
from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import config
from app.models import AppSetting, AuditLog, InboxCrmLink, InboxReply, KeywordDelivery, User, utcnow
from app.routers import inbox as inbox_router
from app.routers import operator_mcp
from app.services import inbox, inbox_crm, inbox_organisation, operator_inbox, operator_tools
from app.services import whatsapp_direct as direct
from app.services import whatsapp_keywords as keywords
from app.services.inbox_providers import InboxError
from tests import test_inbox as inbox_tests
from tests import test_personal_whatsapp as wa_tests
from tests import test_r72_whatsapp_inbox as r72_tests
from tests import test_whatsapp_keywords as kw
from tests.conftest import ADMIN_EMAIL, ADMIN_HASH, session
from tests.test_people import flash_of

TOKEN = "static-operator-token-synthetic-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
THREAD = "same-provider-thread-id"
READ = [t.name for t in operator_inbox.READ_TOOLS]
WRITE = [t.name for t in operator_inbox.WRITE_TOOLS]
MESSAGING = [t.name for t in operator_inbox.MESSAGING_TOOLS]
OFF = "sending messages is switched off on this deployment (GHQ_OPERATOR_MESSAGING)"
REFRESH = "Refresh the inbox before continuing."

connected = inbox_tests.connected      # two business accounts on a stand-in provider; yields the list of sends it was asked for
wa = wa_tests.wa                       # the owner's personal WhatsApp listener, signed as its adapter signs
canonical = r72_tests.canonical        # the official MHP WhatsApp number behind a stand-in Relaunch72 bridge


@pytest.fixture
def mcp(monkeypatch):
    """Writes enabled, messaging off: what a deployment offers after GHQ_OPERATOR_WRITES alone is switched on."""
    monkeypatch.setattr(config, "GHQ_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GHQ_OPERATOR_USER_EMAIL", ADMIN_EMAIL)
    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "enabled")
    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "")
    app = FastAPI()
    app.include_router(operator_mcp.router)
    return TestClient(app)


@pytest.fixture
def messaging(monkeypatch, mcp):
    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "enabled")
    return mcp


@pytest.fixture
def listener(monkeypatch, client):
    """The keyword listener's adapter secret and one reported group."""
    monkeypatch.setattr(config, "WHATSAPP_KEYWORD_ADAPTER_SECRET", kw.SECRET)
    kw.inventory(client)
    return client


def rpc(c: TestClient, method: str, params: dict | None = None) -> dict:
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params is not None else {})},
               headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def call(c: TestClient, tool: str, /, **arguments) -> tuple[bool, str]:
    result = rpc(c, "tools/call", {"name": tool, "arguments": arguments})
    return result["isError"], result["content"][0]["text"]


def audit_count() -> int:
    with session() as db:
        return db.query(AuditLog).count()


def last_audit() -> tuple[str, str, str]:
    with session() as db:
        row = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
        return (row.action, row.entity, row.actor) if row else ("", "", "")


def read(c: TestClient, tool: str, /, **arguments) -> dict:
    """A read tool answers, and leaves no audit row: it changed nothing."""
    before = audit_count()
    is_error, text = call(c, tool, **arguments)
    assert not is_error, text
    assert audit_count() == before
    return json.loads(text)


def ok(c: TestClient, tool: str, /, **arguments) -> dict:
    is_error, text = call(c, tool, **arguments)
    assert not is_error, text
    data = json.loads(text)
    assert data["ok"] is True and isinstance(data["message"], str)
    assert last_audit() == ("operator.tool", f"tool:{tool}", ADMIN_EMAIL)
    return data


def refused(c: TestClient, tool: str, expected: str, /, **arguments) -> str:
    """The tool refuses with the screen's sentence and no audit row (so nothing was written) appears."""
    before = audit_count()
    is_error, text = call(c, tool, **arguments)
    assert is_error, text
    assert expected in text, text
    assert audit_count() == before, "a refusal writes nothing"
    return text


def count(model, *where) -> int:
    with session() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def actions(entity_prefix: str) -> list[str]:
    with session() as db:
        return [r.action for r in db.query(AuditLog).filter(AuditLog.entity.like(entity_prefix + "%")).order_by(AuditLog.id)]


def conversations(c: TestClient, **arguments) -> list[dict]:
    return read(c, "inbox_conversations", **arguments)["conversations"]


def ticket_for(c: TestClient, account: str = "kp-fb") -> str:
    [conversation] = conversations(c, accounts=[account])
    return conversation["thread_ticket"]


def admin_id() -> int:
    with session() as db:
        return db.scalars(select(User).where(User.email == ADMIN_EMAIL)).one().id


def saved_rules() -> dict:
    with session() as db:
        return keywords.rules(db)


def land_rule(**changes) -> dict:
    return {"revision": 0, "name": "Land masterclass", "brand": "mhp", "keywords": ["Land"],
            "message": "Hi {name}, here is the masterclass: https://example.test/land", "group_ids": [kw.GROUP], **changes}


def typed(client, event_id: str = "message-1", text: str = "LAND!") -> dict:
    """Someone types in the listened group; the listener reports it as its adapter does."""
    response = kw.signed(client, "POST", kw.BASE + "/events", kw.event(event_id, text=text))
    assert response.status_code == 200, response.text
    return response.json()


def personal_whatsapp(db, wa, *, reply: bool = True) -> str:
    """Register the owner's linked device, switch collection (and replies) on and take in one private message."""
    user, device = wa
    assert device("status").status_code == 200
    direct.configure(db, user, True, reply)
    assert device("status").status_code == 200
    assert device("event", **wa_tests.event()).json()["stored"]
    return wa_tests.PEER


# ---- the catalogue and the three classes ---------------------------------------------------------------------------
def test_tools_list_offers_read_always_write_with_writes_and_messaging_only_with_both(mcp, monkeypatch):
    def offered() -> dict:
        return {t["name"]: t for t in rpc(mcp, "tools/list")["tools"]}

    names = offered()                                                       # writes on, messaging off
    assert set(READ) | set(WRITE) <= set(names) and not set(MESSAGING) & set(names)
    assert not config.operator_messaging_enabled()
    for name in READ + WRITE:
        schema = names[name]["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False and names[name]["description"]
        assert set(schema.get("required", [])) <= set(schema["properties"]) and "annotations" not in names[name]

    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "enabled")        # both on
    names = offered()
    assert set(READ) | set(WRITE) | set(MESSAGING) <= set(names) and config.operator_messaging_enabled()
    assert list(names) == [t.name for t in operator_tools.READ_TOOLS + operator_tools.WRITE_TOOLS + operator_tools.MESSAGING_TOOLS]
    for name in MESSAGING:
        assert names[name]["annotations"] == {"readOnlyHint": False, "openWorldHint": True}
        assert "REAL MESSAGE TO A REAL PERSON" in names[name]["description"]

    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "")                  # messaging "enabled" alone is not enough
    names = offered()
    assert set(READ) <= set(names) and not (set(WRITE) | set(MESSAGING)) & set(names)
    assert list(names) == [t.name for t in operator_tools.READ_TOOLS] and not config.operator_messaging_enabled()
    is_error, text = call(mcp, "inbox_reply", thread_ticket="x", message="x")
    assert is_error and text == OFF
    is_error, text = call(mcp, "inbox_organise", thread_ticket="x", conversation_type="business")
    assert is_error and "writes are disabled" in text

    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "")               # both off: read only
    assert list(offered()) == [t.name for t in operator_tools.READ_TOOLS]


def test_the_classes_are_what_they_say_and_the_descriptions_carry_the_warnings():
    assert READ == ["inbox_accounts", "inbox_conversations", "inbox_messages", "whatsapp_keyword_rules"]
    assert WRITE == ["inbox_link_crm", "inbox_organise", "whatsapp_keyword_rule_save_paused", "whatsapp_keyword_catchup_cancel"]
    assert MESSAGING == ["whatsapp_keyword_rule_save", "whatsapp_keyword_catchup", "inbox_reply"]
    for tool in operator_inbox.READ_TOOLS:
        assert not tool.write and not tool.messaging and operator_tools.get(tool.name) is tool
    for tool in operator_inbox.WRITE_TOOLS:
        assert tool.write and not tool.messaging and tool in operator_tools.WRITE_TOOLS
    for tool in operator_inbox.MESSAGING_TOOLS:
        assert tool.write and tool.messaging and tool not in operator_tools.WRITE_TOOLS
    assert "signed ticket" in operator_tools.get("inbox_conversations").description
    save = operator_tools.get("whatsapp_keyword_rule_save")
    assert "ONLY when the owner has said in this conversation" in save.description
    assert "enabled" not in operator_tools.get("whatsapp_keyword_rule_save_paused").input_schema["properties"]
    assert "requested_reply" not in operator_tools.get("whatsapp_keyword_rule_save_paused").input_schema["properties"]
    catchup = operator_tools.get("whatsapp_keyword_catchup")
    assert "ONLY when the owner has said in this conversation" in catchup.description and "up to 300 private messages" in catchup.description
    assert catchup.input_schema["required"] == ["rule_id"] and catchup.input_schema["properties"]["hours"]["maximum"] == 168
    assert "sends nothing" in operator_tools.get("whatsapp_keyword_catchup_cancel").description
    assert "never earlier ones" not in save.description and "whatsapp_keyword_catchup" in save.description
    reply = operator_tools.get("inbox_reply").description
    assert "ONE reply in ONE Inbox conversation" in reply and "never for unsolicited, bulk" in reply and "DO NOT send it again" in reply
    assert operator_tools.available(writes_enabled=True, messaging_enabled=False) == list(operator_tools.READ_TOOLS + operator_tools.WRITE_TOOLS)
    assert operator_tools.available(writes_enabled=False, messaging_enabled=True) == list(operator_tools.READ_TOOLS)


# ---- inbox_accounts ---------------------------------------------------------------------------------------------------
def test_inbox_accounts_lists_what_this_admin_sees_and_keeps_a_personal_account_with_its_owner(mcp, connected, wa, db, monkeypatch):
    personal_whatsapp(db, wa)
    data = read(mcp, "inbox_accounts")
    rows = {a["key"]: a for a in data["accounts"]}
    assert set(rows) == {"kp-fb", "mhp-ig", direct.ACCOUNT_KEY} and data["count"] == 3
    assert rows["kp-fb"] == {"key": "kp-fb", "label": "Key Properties", "brand": "kp", "brand_name": "Key Properties UK",
                             "channel": "facebook", "channel_name": "Facebook", "provider": "zernio", "personal": False,
                             "configured": True, "can_reply": True}
    own = rows[direct.ACCOUNT_KEY]
    assert (own["channel"], own["provider"], own["personal"], own["can_reply"], own["connector_online"]) == ("whatsapp", "local", True, True, True)
    text = call(mcp, "inbox_accounts")[1]
    assert "test-inbox-token" not in text and wa_tests.SECRET not in text and "token" not in text

    with session() as s:                                    # a second admin never sees the owner's personal account
        s.add(User(email="second@example.test", password_hash=ADMIN_HASH, is_active=True))
        s.commit()
    monkeypatch.setattr(config, "GHQ_OPERATOR_USER_EMAIL", "second@example.test")
    assert {a["key"] for a in read(mcp, "inbox_accounts")["accounts"]} == {"kp-fb", "mhp-ig"}
    refused(mcp, "inbox_conversations", "Unknown inbox filter", accounts=[direct.ACCOUNT_KEY])
    assert all(c["account_key"] != direct.ACCOUNT_KEY for c in conversations(mcp))
    assert connected == []


def test_inbox_accounts_refuses_a_broken_account_configuration_with_the_screens_sentence(mcp, monkeypatch):
    assert read(mcp, "inbox_accounts") == {"count": 0, "accounts": []}
    monkeypatch.setattr(config, "GHQ_INBOX_ACCOUNTS", "{not json")
    refused(mcp, "inbox_accounts", inbox_router.SETUP_ERROR)
    refused(mcp, "inbox_conversations", inbox_router.SETUP_ERROR)


# ---- inbox_conversations ----------------------------------------------------------------------------------------------
def test_inbox_conversations_reads_the_screens_list_with_its_filters_and_a_working_ticket(mcp, connected, db, monkeypatch):
    data = read(mcp, "inbox_conversations")
    assert (data["count"], data["matching"], data["truncated"]) == (2, 2, False)
    assert [(a["account_key"], a["error"]) for a in data["accounts_read"]] == [("kp-fb", None), ("mhp-ig", None)]
    row = next(c for c in data["conversations"] if c["account_key"] == "kp-fb")
    assert (row["name"], row["participant_id"], row["preview"], row["last_message_at"], row["unread"]) == (
        "<script>Contact</script>", "native-person", "Durham enquiry", "2026-11-06T12:00:00Z", 1)
    assert (row["channel"], row["chat"], row["phone"], row["category"], row["category_label"], row["conversation_type"]) == (
        "facebook", None, None, "unscanned", "Awaiting scan", "unclassified")
    account, bound = inbox.resolve(row["thread_ticket"], admin_id(), ADMIN_EMAIL, db)   # the ticket the screen itself issues
    assert (account.key, bound["thread"], bound["participant"]) == ("kp-fb", THREAD, "native-person")

    assert [c["account_key"] for c in conversations(mcp, accounts=["kp-fb"])] == ["kp-fb"]
    assert [c["account_key"] for c in conversations(mcp, accounts="mhp-ig")] == ["mhp-ig"]
    assert [c["account_key"] for c in conversations(mcp, channel="instagram")] == ["mhp-ig"]
    assert [c["account_key"] for c in conversations(mcp, brand="kp")] == ["kp-fb"]
    assert conversations(mcp, channel="whatsapp") == [] and conversations(mcp, query="nobody") == []
    assert len(conversations(mcp, query="CONTACT")) == 2
    one = read(mcp, "inbox_conversations", limit=1)
    assert (one["count"], one["matching"], one["truncated"], len(one["conversations"])) == (1, 2, True, 1)

    with session() as s:                                    # the owner's own filing shows, and filters as on the screen
        user = s.get(User, admin_id())
        inbox_organisation.save(s, user, next(a for a in inbox.accounts(s) if a.key == "kp-fb"), THREAD, "business", ["kp", "pp"], [])
    [filed] = conversations(mcp, conversation_type="business")
    assert (filed["account_key"], filed["conversation_type"], filed["businesses"], filed["category"]) == ("kp-fb", "business", ["kp", "pp"], "business")
    assert [c["account_key"] for c in conversations(mcp, brand="pp")] == ["kp-fb"]
    assert [c["account_key"] for c in conversations(mcp, label="unscanned")] == ["mhp-ig"]

    refused(mcp, "inbox_conversations", "Unknown inbox filter", accounts=["unknown"])
    refused(mcp, "inbox_conversations", "Unknown inbox filter", channel="email")
    refused(mcp, "inbox_conversations", "Choose a valid business.", brand="nope")
    refused(mcp, "inbox_conversations", "Choose an inbox triage filter.", label="nope")
    refused(mcp, "inbox_conversations", "Choose a conversation type.", conversation_type="nope")
    refused(mcp, "inbox_conversations", "limit must be a whole number from 1 to 100", limit=0)

    original = inbox.client                                 # one account failing leaves the other readable, as on the screen

    class Fails:
        def conversations(self):
            raise InboxError("permission_needed")
    monkeypatch.setattr(inbox, "client", lambda a: Fails() if a.key == "mhp-ig" else original(a))
    partial = read(mcp, "inbox_conversations")
    assert [c["account_key"] for c in partial["conversations"]] == ["kp-fb"]
    assert partial["accounts_read"][1] == {"account_key": "mhp-ig", "label": "Martin Howard Property",
                                           "error": "This connection needs inbox permission.", "more_in_original_inbox": False}
    assert connected == []


def test_inbox_conversations_bounds_the_list_and_the_preview(mcp, connected, monkeypatch):
    many = [{"id": f"thread-{n:03d}", "name": f"Person {n}", "participant_id": f"person-{n}", "preview": "p" * 900,
             "updated_at": f"2026-11-06T12:{n % 60:02d}:00Z", "unread": 0} for n in range(130)]
    monkeypatch.setattr(inbox.client, "conversations", lambda self: (many, True))
    data = read(mcp, "inbox_conversations", accounts=["kp-fb"], limit=5000)
    assert (data["count"], data["matching"], data["truncated"], len(data["conversations"])) == (100, 130, True, 100)
    assert data["accounts_read"] == [{"account_key": "kp-fb", "label": "Key Properties", "error": None, "more_in_original_inbox": True}]
    assert all(len(c["preview"]) == 300 and c["preview_truncated"] for c in data["conversations"])


# ---- inbox_messages ---------------------------------------------------------------------------------------------------
def test_inbox_messages_shows_the_conversation_as_the_screen_does(mcp, connected, monkeypatch):
    ticket = ticket_for(mcp)
    data = read(mcp, "inbox_messages", thread_ticket=ticket)
    assert (data["name"], data["participant_id"], data["chat"], data["notice"], data["reply_possible"]) == (
        "<script>Contact</script>", "native-person", None, None, True)
    assert data["account"]["key"] == "kp-fb" and data["account"]["can_reply"] is True
    assert data["messages"] == [{"id": "m1", "direction": "incoming", "sender": "Test contact", "at": "2026-11-06T12:00:00Z",
                                 "text": "<script>Message</script>", "text_truncated": False}]
    assert (data["count"], data["truncated"], data["earlier_cursor"], data["earlier_messages_not_loaded"]) == (1, False, None, False)
    assert data["reply_receipts"] == [] and data["crm_links"] == [] and data["property_deals"] == [] and data["triage"] is None
    assert data["organisation"] == {"conversation_type": "unclassified", "businesses": [], "copy_messages_to": [], "note": ""}

    seen = []

    def long_page(self, thread, cursor=None):
        seen.append(cursor)
        rows = [{"id": f"m{n}", "body": "x" * 4500 if n == 0 else "short", "direction": "outgoing" if n % 2 else "incoming",
                 "name": "", "occurred_at": "2026-11-06T12:00:00Z"} for n in range(130)]
        return rows, "older-page"
    monkeypatch.setattr(inbox.client, "messages", long_page)
    page = read(mcp, "inbox_messages", thread_ticket=ticket, cursor="page-two")
    assert seen == ["page-two"] and (page["count"], page["truncated"], page["earlier_cursor"]) == (100, True, "older-page")
    assert [m["id"] for m in page["messages"]][:2] == ["m30", "m31"] and len(page["messages"]) == 100
    assert (page["messages"][0]["sender"], page["messages"][1]["sender"]) == ("<script>Contact</script>", "You")
    monkeypatch.setattr(inbox.client, "messages", lambda self, thread, cursor=None: (
        [{"id": "big", "body": "x" * 4500, "direction": "incoming", "name": "A", "occurred_at": ""}], None))
    [big] = read(mcp, "inbox_messages", thread_ticket=ticket)["messages"]
    assert (len(big["text"]), big["text_truncated"], big["at"]) == (4000, True, None)
    assert connected == []


def test_inbox_messages_refuses_a_bad_ticket_and_an_unreachable_channel(mcp, connected, db, monkeypatch):
    ticket = ticket_for(mcp)
    refused(mcp, "inbox_messages", REFRESH, thread_ticket="tampered")
    refused(mcp, "inbox_messages", REFRESH, thread_ticket=ticket[:-3] + "abc")
    refused(mcp, "inbox_messages", "thread_ticket is required")
    account = next(a for a in inbox.accounts() if a.key == "kp-fb")
    foreign = inbox.ticket(account, THREAD, admin_id() + 1, "Test contact")      # issued to another admin
    refused(mcp, "inbox_messages", REFRESH, thread_ticket=foreign)

    def limited(self, thread, cursor=None):
        raise InboxError("rate_limited")
    monkeypatch.setattr(inbox.client, "messages", limited)
    refused(mcp, "inbox_messages", "The channel is rate limited. Please wait before refreshing.", thread_ticket=ticket)
    monkeypatch.setenv("GHQ_INBOX_TEST", "rotated-test-token")                   # a rotated credential ends the old ticket
    refused(mcp, "inbox_messages", REFRESH, thread_ticket=ticket)


def test_personal_whatsapp_is_read_one_to_one_with_the_number_and_the_last_synced_messages(mcp, wa, db):
    peer = personal_whatsapp(db, wa)
    [row] = conversations(mcp, channel="whatsapp")
    assert (row["account_key"], row["name"], row["participant_id"], row["phone"], row["chat"], row["preview"]) == (
        direct.ACCOUNT_KEY, "Fixture", peer, "+447700900120", "direct", "Fixture enquiry")
    data = read(mcp, "inbox_messages", thread_ticket=row["thread_ticket"])
    assert (data["phone"], data["chat"], data["notice"], data["reply_possible"]) == ("+447700900120", "direct", None, True)
    [message] = data["messages"]
    assert (message["id"], message["direction"], message["sender"], message["text"]) == ("baileys:m-one", "incoming", "Fixture", "Fixture enquiry")

    with session() as s:                                    # the owner's computer goes quiet: still readable, with the screen's notice
        setting = s.get(AppSetting, direct.KEY)
        setting.value = {**setting.value, "seen": direct.iso(utcnow() - timedelta(minutes=10))}
        s.commit()
    offline = read(mcp, "inbox_messages", thread_ticket=row["thread_ticket"])
    assert offline["notice"] == "Your personal connector is offline or catching up. These are the last synced messages."
    assert len(offline["messages"]) == 1 and offline["reply_possible"] is False and offline["account"]["connector_online"] is False
    user, device = wa
    assert device("event", **wa_tests.event(peer="123456789@g.us")).status_code == 400     # a group chat is never taken in
    assert len(conversations(mcp, channel="whatsapp")) == 1


def test_the_official_whatsapp_number_is_read_through_the_bridge_as_the_acting_admin(mcp, canonical):
    account_id, thread, person, calls = canonical
    [row] = conversations(mcp, channel="whatsapp", brand="mhp")
    assert (row["account_key"], row["name"], row["participant_id"], row["chat"], row["preview"]) == (
        "mhp-whatsapp", "Durham owner", person, "direct", "Received message")
    data = read(mcp, "inbox_messages", thread_ticket=row["thread_ticket"])
    assert [m["text"] for m in data["messages"]] == ["Received from the MHP number"]
    assert data["mhp_crm_contact_id"] == person and data["reply_possible"] is False and data["account"]["can_reply"] is False
    assert {(email, action) for email, action, _ in calls} == {(ADMIN_EMAIL, "whatsappThreads"), (ADMIN_EMAIL, "whatsappMessages")}


# ---- whatsapp_keyword_rules -------------------------------------------------------------------------------------------
def test_whatsapp_keyword_rules_shows_rules_groups_listener_and_enquiries(mcp, listener, admin_client, monkeypatch):
    empty = read(mcp, "whatsapp_keyword_rules")
    assert (empty["revision"], empty["rules"], empty["deliveries"]) == (0, [], [])
    assert empty["groups"] == [{"id": kw.GROUP, "name": "Income test group"}] and empty["groups_total"] == 1
    assert empty["listener"]["adapter_configured"] is True and empty["listener"]["id"] == keywords.LISTENER
    assert empty["listener"]["groups_reported_at"] and empty["listener"]["online"] is False

    assert kw.create(admin_client).status_code == 303                       # the owner's own enabled rule, saved on the screen
    assert typed(listener, text="INCOME 🙌")["outcome"] == "queued"
    assert typed(listener, "message-2", "just chatting about income")["outcome"] == "ignored"
    data = read(mcp, "whatsapp_keyword_rules")
    [rule] = data["rules"]
    assert data["revision"] == 1 and (rule["name"], rule["brand"], rule["enabled"], rule["keywords"], rule["match_mode"]) == (
        "Income action plan", "mhp", True, ["Income"], "exact")
    assert rule["groups"] == [{"id": kw.GROUP, "name": "Income test group"}] and rule["message"] == "Hi {name}, https://example.test/plan"
    [job] = data["deliveries"]                                              # only the matching message was ever recorded
    assert (job["state"], job["sender_name"], job["sender_phone"], job["brand"], job["rule_id"], job["rule_name"], job["group_name"], job["reply"]) == (
        "pending", "Emma Test", "+447700900123", "mhp", rule["id"], "Income action plan", "Income test group", "Hi Emma, https://example.test/plan")
    assert "not stored" in data["deliveries_note"]
    text = call(mcp, "whatsapp_keyword_rules")[1]
    assert kw.SECRET not in text and "secret" not in text.lower()

    assert read(mcp, "whatsapp_keyword_rules", group_query="INCOME")["groups_matching"] == 1
    none = read(mcp, "whatsapp_keyword_rules", group_query="nothing like it")
    assert (none["groups"], none["groups_matching"], none["groups_total"]) == ([], 0, 1)
    monkeypatch.setattr(config, "WHATSAPP_KEYWORD_ADAPTER_SECRET", "")
    assert read(mcp, "whatsapp_keyword_rules")["listener"]["adapter_configured"] is False
    refused(mcp, "whatsapp_keyword_rules", "group_query must be text", group_query=5)


def test_whatsapp_keyword_rules_bounds_the_group_list(mcp, monkeypatch, client):
    monkeypatch.setattr(config, "WHATSAPP_KEYWORD_ADAPTER_SECRET", kw.SECRET)
    groups = [{"id": f"{100000 + n}@g.us", "name": f"Group {n}"} for n in range(140)]
    assert kw.signed(client, "POST", kw.BASE + "/groups", {"listenerId": keywords.LISTENER, "groups": groups}).status_code == 200
    data = read(mcp, "whatsapp_keyword_rules")
    assert (len(data["groups"]), data["groups_matching"], data["groups_total"], data["groups_truncated"]) == (100, 140, 140, True)
    narrowed = read(mcp, "whatsapp_keyword_rules", group_query="group 13")
    assert [g["name"] for g in narrowed["groups"]] == ["Group 13"] + [f"Group {n}" for n in range(130, 140)]
    assert narrowed["groups_truncated"] is False


# ---- inbox_link_crm ---------------------------------------------------------------------------------------------------
@pytest.fixture
def crm(monkeypatch):
    """A stand-in for the Relaunch72 CRM command: records what it was asked and confirms a contact."""
    calls = []
    contact_id, workspace_id = str(uuid.uuid4()), str(uuid.uuid4())

    def command(actor, brand, action, **payload):
        calls.append((actor.email, brand, action, payload))
        return {"brand": brand, "contactId": payload.get("contactId", contact_id), "workspaceId": workspace_id}
    monkeypatch.setattr(inbox_crm, "command", command)
    monkeypatch.setattr(config, "R72_GROUP_HQ_INBOX_SECRET", "fixture-credential-" * 3)
    return calls, contact_id, workspace_id


def test_inbox_link_crm_creates_the_lead_as_the_screens_button_does(mcp, connected, crm):
    calls, contact_id, workspace_id = crm
    ticket = ticket_for(mcp)
    data = ok(mcp, "inbox_link_crm", thread_ticket=ticket, brand="kp", copy_messages=True)
    assert data == {"ok": True, "message": "Conversation linked to the selected business CRM.", "account_key": "kp-fb", "brand": "kp",
                    "contact_id": contact_id, "workspace_id": workspace_id, "copy_messages": True}
    assert calls == [(ADMIN_EMAIL, "kp", "create", {"source": {"channel": "facebook", "accountId": "kp-native-account",
                                                               "threadId": THREAD, "participantId": "native-person"},
                                                    "displayName": "<script>Contact</script>"})]
    with session() as db:
        [link] = db.scalars(select(InboxCrmLink)).all()
        assert (link.account_key, link.brand, link.contact_id, link.workspace_id, link.participant_id, link.user_id) == (
            "kp-fb", "kp", contact_id, workspace_id, "native-person", admin_id())
    assert actions("inbox.conv.v1.") == ["inbox.conversation.organise"]      # the screen's own audit row, then the operator's
    shown = read(mcp, "inbox_messages", thread_ticket=ticket)
    assert shown["crm_links"] == [{"brand": "kp", "brand_name": "Key Properties UK", "contact_id": contact_id,
                                   "workspace_id": workspace_id, "last_error": None}]
    assert shown["organisation"] == {"conversation_type": "business", "businesses": ["kp"], "copy_messages_to": ["kp"], "note": ""}

    existing = str(uuid.uuid4())                                            # the same person in another business, an existing contact
    again = ok(mcp, "inbox_link_crm", thread_ticket=ticket, brand="mhp", contact_id=existing, display_name="Ignored")
    assert (again["contact_id"], again["copy_messages"]) == (existing, False)
    assert calls[-1][1:3] == ("mhp", "link") and calls[-1][3]["contactId"] == existing and "displayName" not in calls[-1][3]
    assert count(InboxCrmLink) == 2
    with session() as db:
        account = next(a for a in inbox.accounts(db) if a.key == "kp-fb")
        assert inbox_organisation.choices(db, admin_id(), account, THREAD)["sync_brands"] == ["kp"]
    assert connected == [], "linking to a CRM sends no message"


def test_inbox_link_crm_refusals_are_the_screens_and_link_nothing(mcp, connected, crm, monkeypatch):
    calls, _, _ = crm
    ticket = ticket_for(mcp)
    refused(mcp, "inbox_link_crm", "Choose a business for this CRM contact.", thread_ticket=ticket, brand="nope")
    refused(mcp, "inbox_link_crm", "Choose an existing CRM contact.", thread_ticket=ticket, brand="kp", contact_id="not-a-uuid")
    refused(mcp, "inbox_link_crm", REFRESH, thread_ticket="tampered", brand="kp")
    refused(mcp, "inbox_link_crm", "brand is required", thread_ticket=ticket)
    refused(mcp, "inbox_link_crm", "copy_messages must be true or false", thread_ticket=ticket, brand="kp", copy_messages="yes")
    assert calls == []

    def unavailable(actor, brand, action, **payload):
        raise inbox_crm.CrmUnavailable("Your CRM access for this business is unavailable.")
    monkeypatch.setattr(inbox_crm, "command", unavailable)
    refused(mcp, "inbox_link_crm", "Your CRM access for this business is unavailable.", thread_ticket=ticket, brand="kp")
    assert count(InboxCrmLink) == 0
    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "")
    is_error, text = call(mcp, "inbox_link_crm", thread_ticket=ticket, brand="kp")
    assert is_error and "writes are disabled" in text and count(InboxCrmLink) == 0


def test_the_crm_route_still_flashes_as_before(admin_client, connected, crm, db, monkeypatch):
    account = next(a for a in inbox.accounts() if a.key == "kp-fb")
    ticket = inbox.ticket(account, THREAD, admin_id(), "Buyer", "native-person")
    r = admin_client.post("/inbox/crm", data={"thread_ticket": ticket, "brand": "kp", "display_name": "Buyer"})
    assert r.status_code == 303 and r.headers["location"].startswith("/inbox?account=kp-fb&thread=")
    assert flash_of(r.headers["location"])["ok"] == "Conversation linked to the selected business CRM."
    assert crm[0][-1][2] == "create" and crm[0][-1][3]["displayName"] == "Buyer"

    def unavailable(actor, brand, action, **payload):
        raise inbox_crm.CrmUnavailable("CRM is temporarily unavailable. Your conversation is saved; try the CRM action again.")
    monkeypatch.setattr(inbox_crm, "command", unavailable)
    r = admin_client.post("/inbox/crm", data={"thread_ticket": ticket, "brand": "mhp", "display_name": "Buyer"})
    assert r.status_code == 303 and flash_of(r.headers["location"])["err"].startswith("CRM is temporarily unavailable.")
    r = admin_client.post("/inbox/organise", data={"thread_ticket": ticket, "kind": "business", "brands": ["kp"], "sync_brands": ["mhp"]})
    assert r.status_code == 400
    r = admin_client.post("/inbox/organise", data={"thread_ticket": ticket, "kind": "business", "brands": ["kp"], "sync_brands": ["kp"]})
    assert flash_of(r.headers["location"])["ok"] == "Conversation choices saved. Your reply channel has not changed."


# ---- inbox_organise ---------------------------------------------------------------------------------------------------
def test_inbox_organise_saves_the_conversation_choices_and_keeps_what_is_left_out(mcp, connected, crm):
    ticket = ticket_for(mcp)
    data = ok(mcp, "inbox_organise", thread_ticket=ticket, conversation_type="business", businesses=["mhp", "kp"], note=" Asked for the masterclass ")
    assert data == {"ok": True, "message": "Conversation choices saved. Your reply channel has not changed.", "account_key": "kp-fb",
                    "conversation_type": "business", "businesses": ["kp", "mhp"], "copy_messages_to": [], "note": "Asked for the masterclass"}
    assert actions("inbox.conv.v1.") == ["inbox.conversation.organise"]
    kept = ok(mcp, "inbox_organise", thread_ticket=ticket, conversation_type="property")
    assert (kept["conversation_type"], kept["businesses"], kept["note"]) == ("property", ["kp", "mhp"], "Asked for the masterclass")
    [row] = conversations(mcp, accounts=["kp-fb"])
    assert (row["conversation_type"], row["category"], row["businesses"]) == ("property", "property", ["kp", "mhp"])

    ok(mcp, "inbox_link_crm", thread_ticket=ticket, brand="kp")                # copying needs the link first, then it is allowed
    copied = ok(mcp, "inbox_organise", thread_ticket=ticket, conversation_type="business", copy_messages_to=["kp"])
    assert copied["copy_messages_to"] == ["kp"]
    cleared = ok(mcp, "inbox_organise", thread_ticket=ticket, conversation_type="personal", businesses=[], copy_messages_to=[], note="")
    assert (cleared["businesses"], cleared["copy_messages_to"], cleared["note"]) == ([], [], "")
    assert connected == []


def test_inbox_organise_refusals_are_the_screens_and_save_nothing(mcp, connected):
    ticket = ticket_for(mcp)
    refused(mcp, "inbox_organise", "Link a CRM contact before choosing message copying.", thread_ticket=ticket,
            conversation_type="business", businesses=["kp"], copy_messages_to=["kp"])
    refused(mcp, "inbox_organise", "Choose a conversation type and valid businesses.", thread_ticket=ticket, conversation_type="nope")
    refused(mcp, "inbox_organise", "Choose a conversation type and valid businesses.", thread_ticket=ticket,
            conversation_type="business", businesses=["not-a-business"])
    refused(mcp, "inbox_organise", "Keep the private note within 2,000 characters.", thread_ticket=ticket,
            conversation_type="business", note="n" * 2001)
    refused(mcp, "inbox_organise", "conversation_type is required", thread_ticket=ticket)
    refused(mcp, "inbox_organise", REFRESH, thread_ticket="tampered", conversation_type="business")
    assert count(AppSetting, AppSetting.key.like("inbox.conv.v1.%")) == 0
    assert conversations(mcp, accounts=["kp-fb"])[0]["conversation_type"] == "unclassified"


# ---- whatsapp_keyword_rule_save_paused --------------------------------------------------------------------------------
def test_keyword_rule_save_paused_drafts_a_rule_that_answers_nobody(mcp, listener):
    data = ok(mcp, "whatsapp_keyword_rule_save_paused", **land_rule(enabled=True, requested_reply=True))   # neither is honoured here
    assert data["message"] == "Rule saved. The listener will use it for new requests; existing deliveries are not repeated."
    assert (data["revision"], data["enabled"], data["rule"]["enabled"]) == (1, False, False)
    assert data["rule"]["groups"] == [{"id": kw.GROUP, "name": "Income test group"}] and data["rule"]["keywords"] == ["Land"]
    [rule] = saved_rules()["rules"]
    assert rule == {"id": data["rule_id"], "brand": "mhp", "name": "Land masterclass", "keywords": ["Land"], "matchMode": "exact",
                    "message": "Hi {name}, here is the masterclass: https://example.test/land", "groupIds": [kw.GROUP], "enabled": False}
    assert actions("whatsapp-keyword:") == ["whatsapp.keyword.save"]
    assert typed(listener) == {"outcome": "ignored", "deliveryId": None} and count(KeywordDelivery) == 0

    edited = ok(mcp, "whatsapp_keyword_rule_save_paused", revision=1, rule_id=data["rule_id"], keywords=["Land", "land deal"], match_mode="word")
    assert (edited["revision"], edited["rule_id"], edited["rule"]["keywords"], edited["rule"]["match_mode"]) == (2, data["rule_id"], ["Land", "land deal"], "word")
    assert edited["rule"]["name"] == "Land masterclass" and edited["rule"]["message"].startswith("Hi {name}") and len(saved_rules()["rules"]) == 1
    assert read(mcp, "whatsapp_keyword_rules")["rules"][0]["enabled"] is False


def test_keyword_rule_save_paused_refusals_are_the_editors_and_save_nothing(mcp, listener):
    refused(mcp, "whatsapp_keyword_rule_save_paused", "These rules changed. Refresh before saving.", **land_rule(revision=3))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Choose at least one connected group and a valid matching option.", **land_rule(group_ids=["999999@g.us"]))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Choose at least one connected group and a valid matching option.", **land_rule(group_ids=[]))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Choose at least one connected group and a valid matching option.", **land_rule(match_mode="fuzzy"))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Add 1–20 keywords, one per line, up to 80 characters each.", **land_rule(keywords=[]))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Choose a business, a rule name and a reply of up to 4,000 characters.", **land_rule(message="m" * 4001))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Choose a business, a rule name and a reply of up to 4,000 characters.", **land_rule(brand="piuk"))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Rule not found", **land_rule(rule_id=str(uuid.uuid4())))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "Invalid rule", **land_rule(rule_id="not-a-uuid"))
    refused(mcp, "whatsapp_keyword_rule_save_paused", "revision must be the whole number", **{k: v for k, v in land_rule().items() if k != "revision"})
    refused(mcp, "whatsapp_keyword_rule_save_paused", "keywords must be a list of short texts", **land_rule(keywords=[1, 2]))
    assert saved_rules() == {"revision": 0, "rules": []}


# ---- whatsapp_keyword_rule_save (messaging) ---------------------------------------------------------------------------
def test_keyword_rule_save_is_refused_while_messaging_is_off_and_nothing_is_saved_or_sent(mcp, listener):
    before = audit_count()
    is_error, text = call(mcp, "whatsapp_keyword_rule_save", **land_rule(enabled=True, requested_reply=True))
    assert is_error and text == OFF
    assert saved_rules() == {"revision": 0, "rules": []} and audit_count() == before
    assert typed(listener)["outcome"] == "ignored" and count(KeywordDelivery) == 0
    assert kw.signed(listener, "GET", kw.BASE + "/pending").json() == {"deliveryIds": []}


def test_keyword_rule_save_enables_a_rule_only_with_the_owners_confirmation(messaging, listener):
    refused(messaging, "whatsapp_keyword_rule_save", "Confirm the group invitation promises this private reply before enabling it.",
            **land_rule(enabled=True))
    refused(messaging, "whatsapp_keyword_rule_save", "requested_reply must be true or false", **land_rule(enabled=True, requested_reply="yes"))
    assert saved_rules()["rules"] == []
    draft = ok(messaging, "whatsapp_keyword_rule_save", **land_rule())            # enabled left out: a new rule is paused
    assert draft["enabled"] is False and typed(listener, "message-0")["outcome"] == "ignored"

    live = ok(messaging, "whatsapp_keyword_rule_save", revision=1, rule_id=draft["rule_id"], enabled=True, requested_reply=True)
    assert (live["revision"], live["enabled"], live["rule"]["name"]) == (2, True, "Land masterclass") and "once" in live["note"]
    assert actions("whatsapp-keyword:") == ["whatsapp.keyword.save", "whatsapp.keyword.save"]
    queued = typed(listener)                                                   # a member types the keyword: one private reply is queued
    assert queued["outcome"] == "queued"
    with session() as db:
        job = db.get(KeywordDelivery, queued["deliveryId"])
        assert (job.state, job.reply, job.sender_phone) == ("pending", "Hi Emma, here is the masterclass: https://example.test/land", "447700900123")
    assert typed(listener, "message-2")["outcome"] == "already_contacted" and count(KeywordDelivery) == 1

    refused(messaging, "whatsapp_keyword_rule_save", "An enabled rule already matches that keyword in one of these groups.",
            **land_rule(revision=2, name="Second", enabled=True, requested_reply=True))
    refused(messaging, "whatsapp_keyword_rule_save", "Confirm the group invitation promises this private reply before enabling it.",
            revision=2, rule_id=draft["rule_id"], message="Hi {name}, a new link")    # still enabled, so the confirmation is asked again
    refused(messaging, "whatsapp_keyword_rule_save", "These rules changed. Refresh before saving.", **land_rule(enabled=True, requested_reply=True))
    assert saved_rules()["revision"] == 2

    paused = ok(messaging, "whatsapp_keyword_rule_save", revision=2, rule_id=draft["rule_id"], enabled=False)
    assert paused["enabled"] is False and paused["rule"]["message"].endswith("/land")
    begin = kw.signed(listener, "POST", kw.BASE + "/deliveries/" + queued["deliveryId"] + "/begin", {})
    assert begin.json() == {"send": False, "state": "held"}, "pausing holds the reply that had not gone yet"


def test_the_paused_save_pauses_an_enabled_rule(messaging, listener):
    live = ok(messaging, "whatsapp_keyword_rule_save", **land_rule(enabled=True, requested_reply=True))
    assert live["enabled"] is True
    held = ok(messaging, "whatsapp_keyword_rule_save_paused", revision=1, rule_id=live["rule_id"], message="Hi {name}, updated")
    assert held["enabled"] is False and saved_rules()["rules"][0]["enabled"] is False
    assert typed(listener)["outcome"] == "ignored"


# ---- whatsapp_keyword_catchup (messaging) and whatsapp_keyword_catchup_cancel (write) ---------------------------------
def earlier(client, catchup_id: str, event_id: str = "earlier-1", hours_ago: float = 3, **sender) -> dict:
    """The listener submits a group message it read back from the phone for an open catch-up."""
    response = kw.signed(client, "POST", kw.BASE + "/catchup-events", {**kw.past(event_id, hours_ago, text="land", **sender), "catchupId": catchup_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_keyword_catchup_is_refused_while_messaging_is_off_and_nothing_starts(mcp, listener, admin_client):
    rule = ok(mcp, "whatsapp_keyword_rule_save_paused", **land_rule())["rule_id"]
    before = audit_count()
    is_error, text = call(mcp, "whatsapp_keyword_catchup", rule_id=rule, hours=48, requested_reply=True)
    assert is_error and text == OFF and audit_count() == before
    with session() as db:
        assert keywords.catchups(db) == []
    assert read(mcp, "whatsapp_keyword_rules")["catchups"] == []
    assert kw.signed(listener, "GET", kw.BASE + "/rules").json()["catchups"] == [] and count(KeywordDelivery) == 0


def test_keyword_catchup_answers_earlier_messages_through_the_editors_own_request(messaging, listener):
    live = ok(messaging, "whatsapp_keyword_rule_save", **land_rule(enabled=True, requested_reply=True))
    rule = live["rule_id"]
    data = ok(messaging, "whatsapp_keyword_catchup", rule_id=rule, requested_reply=True)        # hours left out: 48
    assert data["message"].startswith("Catch-up requested.") and "Nothing has been sent yet" in data["note"]
    assert (data["rule_id"], data["rule_name"], data["state"], data["hours"], data["group_ids"], data["listener_online"]) == (
        rule, "Land masterclass", "open", 48, [kw.GROUP], False)
    assert actions("whatsapp-keyword:") == ["whatsapp.keyword.save", "whatsapp.keyword.catchup"]
    [offered] = kw.signed(listener, "GET", kw.BASE + "/rules").json()["catchups"]
    assert (offered["id"], offered["ruleId"], offered["since"], offered["until"]) == (data["catchup_id"], rule, data["since"], data["until"])

    queued = earlier(listener, data["catchup_id"])                                              # the listener found one earlier request
    assert queued["outcome"] == "queued" and earlier(listener, data["catchup_id"], "earlier-2", 5)["outcome"] == "already_contacted"
    with session() as db:
        job = db.get(KeywordDelivery, queued["deliveryId"])
        assert (job.state, job.reply) == ("pending", "Hi Emma, here is the masterclass: https://example.test/land")
    shown = read(messaging, "whatsapp_keyword_rules")
    [row] = shown["catchups"]
    assert (row["id"], row["rule_id"], row["rule_name"], row["state"], row["hours"], row["requested_by"]) == (
        data["catchup_id"], rule, "Land masterclass", "open", 48, ADMIN_EMAIL)
    assert row["counts"]["queued"] == 1 and row["counts"]["already_contacted"] == 1 and row["replies"] == {"pending": 1}
    assert row["groups"] == [{"id": kw.GROUP, "name": "Income test group", "status": "pending", "reached_since": False,
                              "messages_read": 0, "matched": 0, "without_phone_number": 0, "reported_at": None}]
    assert "deliveryIds" not in json.dumps(shown) and "300" in shown["catchups_note"] and "found by a catch-up" in shown["deliveries_note"]

    refused(messaging, "whatsapp_keyword_catchup", "A catch-up is already running. Wait for it to finish, or cancel it first.",
            rule_id=rule, requested_reply=True)
    assert count(KeywordDelivery) == 1


def test_keyword_catchup_refusals_are_the_editors_and_start_nothing(messaging, listener):
    draft = ok(messaging, "whatsapp_keyword_rule_save", **land_rule())
    refused(messaging, "whatsapp_keyword_catchup", "Enable this rule before catching up on earlier messages.",
            rule_id=draft["rule_id"], requested_reply=True)
    live = ok(messaging, "whatsapp_keyword_rule_save", revision=1, rule_id=draft["rule_id"], enabled=True, requested_reply=True)
    rule = live["rule_id"]
    refused(messaging, "whatsapp_keyword_catchup", "Confirm these group members were told this keyword requests this private reply before catching up.",
            rule_id=rule, hours=24)
    refused(messaging, "whatsapp_keyword_catchup", "requested_reply must be true or false", rule_id=rule, requested_reply="yes")
    refused(messaging, "whatsapp_keyword_catchup", "Choose how far back to look: from 1 hour to 7 days (168 hours).", rule_id=rule, hours=0, requested_reply=True)
    refused(messaging, "whatsapp_keyword_catchup", "Choose how far back to look: from 1 hour to 7 days (168 hours).", rule_id=rule, hours=169, requested_reply=True)
    refused(messaging, "whatsapp_keyword_catchup", "hours must be a whole number from 1 to 168", rule_id=rule, hours="48", requested_reply=True)
    refused(messaging, "whatsapp_keyword_catchup", "hours must be a whole number from 1 to 168", rule_id=rule, hours=True, requested_reply=True)
    refused(messaging, "whatsapp_keyword_catchup", "Rule not found", rule_id=str(uuid.uuid4()), requested_reply=True)
    refused(messaging, "whatsapp_keyword_catchup", "rule_id is required", requested_reply=True)
    with session() as db:
        assert keywords.catchups(db) == []
    assert kw.signed(listener, "GET", kw.BASE + "/rules").json()["catchups"] == []


def test_keyword_catchup_cancel_is_a_write_tool_that_stops_it_and_holds_unsent_replies(messaging, listener, monkeypatch):
    rule = ok(messaging, "whatsapp_keyword_rule_save", **land_rule(enabled=True, requested_reply=True))["rule_id"]
    started = ok(messaging, "whatsapp_keyword_catchup", rule_id=rule, hours=6, requested_reply=True)
    queued = earlier(listener, started["catchup_id"])
    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "")                   # cancelling needs writes only: it sends nothing
    refused(messaging, "whatsapp_keyword_catchup_cancel", "Catch-up not found", catchup_id=str(uuid.uuid4()))
    refused(messaging, "whatsapp_keyword_catchup_cancel", "catchup_id is required")
    data = ok(messaging, "whatsapp_keyword_catchup_cancel", catchup_id=started["catchup_id"])
    assert (data["state"], data["replies_held"], data["rule_id"]) == ("cancelled", 1, rule) and "1 reply not yet sent will not go out" in data["message"]
    assert actions("whatsapp-keyword:") == ["whatsapp.keyword.save", "whatsapp.keyword.catchup", "whatsapp.keyword.catchup.cancel"]
    begin = kw.signed(listener, "POST", kw.BASE + "/deliveries/" + queued["deliveryId"] + "/begin", {})
    assert begin.json() == {"send": False, "state": "held"}
    assert kw.signed(listener, "GET", kw.BASE + "/rules").json()["catchups"] == []
    [row] = read(messaging, "whatsapp_keyword_rules")["catchups"]
    assert (row["state"], row["reason"], row["replies"]) == ("cancelled", "owner", {"held": 1})
    refused(messaging, "whatsapp_keyword_catchup_cancel", "That catch-up is no longer running.", catchup_id=started["catchup_id"])
    assert saved_rules()["rules"][0]["enabled"] is True                         # the rule keeps answering new messages


# ---- inbox_reply (messaging) ------------------------------------------------------------------------------------------
def test_inbox_reply_while_messaging_is_off_sends_nothing(mcp, connected):
    ticket = ticket_for(mcp)
    before = audit_count()
    is_error, text = call(mcp, "inbox_reply", thread_ticket=ticket, message="Here is the masterclass link", request_id=str(uuid.uuid4()))
    assert is_error and text == OFF
    assert connected == [], "no provider call"
    assert count(InboxReply) == 0 and audit_count() == before
    assert read(mcp, "inbox_messages", thread_ticket=ticket)["reply_receipts"] == []


def test_inbox_reply_sends_one_reply_through_the_screens_send_and_a_replay_sends_nothing_more(messaging, connected):
    ticket = ticket_for(messaging)
    request_id = str(uuid.uuid4())
    is_error, text = call(messaging, "inbox_reply", thread_ticket=ticket, message="Here is the masterclass link", request_id=request_id)
    assert not is_error, text
    assert json.loads(text) == {"ok": True, "message": "The channel accepted your reply.", "state": "accepted", "reason": None,
                                "request_id": request_id, "account_key": "kp-fb", "conversation": "<script>Contact</script>",
                                "do_not_retry": False, "note": None}
    assert connected == [("kp-fb", THREAD, "Here is the masterclass link", "ghq-" + request_id)]
    with session() as db:
        receipt = db.get(InboxReply, request_id)
        assert (receipt.state, receipt.account_key, receipt.user_id, receipt.provider_message_id) == ("accepted", "kp-fb", admin_id(), "provider-message")
    assert actions("inbox-reply:") == ["inbox.reply.begin", "inbox.reply.accepted"]
    assert last_audit() == ("operator.tool", "tool:inbox_reply", ADMIN_EMAIL)

    again = json.loads(call(messaging, "inbox_reply", thread_ticket=ticket, message="Here is the masterclass link", request_id=request_id)[1])
    assert again["state"] == "accepted" and len(connected) == 1 and count(InboxReply) == 1      # the same request never sends twice
    refused(messaging, "inbox_reply", "This reply request was already used with different content or an account.",
            thread_ticket=ticket, message="Changed text", request_id=request_id)
    assert len(connected) == 1

    generated = json.loads(call(messaging, "inbox_reply", thread_ticket=ticket, message="A second, separate reply")[1])
    assert str(uuid.UUID(generated["request_id"])) == generated["request_id"] != request_id and generated["ok"] is True
    assert len(connected) == 2 and connected[1][3] == "ghq-" + generated["request_id"] and count(InboxReply) == 2
    shown = read(messaging, "inbox_messages", thread_ticket=ticket)["reply_receipts"]
    assert {r["request_id"] for r in shown} == {request_id, generated["request_id"]} and {r["state"] for r in shown} == {"accepted"}


def test_inbox_reply_refusals_are_the_screens_and_send_nothing(messaging, connected, canonical):
    ticket = ticket_for(messaging)
    refused(messaging, "inbox_reply", "Write a reply of up to 10,000 bytes.", thread_ticket=ticket, message="   ")
    refused(messaging, "inbox_reply", "Write a reply of up to 10,000 bytes.", thread_ticket=ticket, message="x" * 10_001)
    refused(messaging, "inbox_reply", "Invalid reply request", thread_ticket=ticket, message="Hello", request_id="not-a-uuid")
    refused(messaging, "inbox_reply", REFRESH, thread_ticket="tampered", message="Hello")
    refused(messaging, "inbox_reply", "message must be text", thread_ticket=ticket)
    refused(messaging, "inbox_reply", "thread_ticket is required", message="Hello")
    official = ticket_for(messaging, "mhp-whatsapp")                           # the official number is read only on this deployment
    refused(messaging, "inbox_reply", "Replies are not configured for this account.", thread_ticket=official, message="Hello")
    assert connected == [] and count(InboxReply) == 0


def test_inbox_reply_reports_an_unknown_outcome_and_a_refusal_without_ever_retrying(messaging, connected, monkeypatch):
    ticket = ticket_for(messaging)

    class Provider:
        def __init__(self, code):
            self.code = code

        def send(self, *args):
            connected.append(args)
            raise InboxError(self.code)
    monkeypatch.setattr(inbox, "client", lambda account: Provider("outcome_unknown"))
    request_id = str(uuid.uuid4())
    is_error, text = call(messaging, "inbox_reply", thread_ticket=ticket, message="Did this arrive?", request_id=request_id)
    data = json.loads(text)
    assert not is_error and (data["ok"], data["state"], data["do_not_retry"]) == (False, "outcome_unknown", True)
    assert data["message"] == "The reply's outcome is unknown. Check the original conversation before sending anything again."
    assert "must not be sent again" in data["note"] and len(connected) == 1
    assert actions("inbox-reply:") == ["inbox.reply.begin", "inbox.reply.outcome_unknown"]
    assert last_audit() == ("operator.tool", "tool:inbox_reply", ADMIN_EMAIL)
    replay = json.loads(call(messaging, "inbox_reply", thread_ticket=ticket, message="Did this arrive?", request_id=request_id)[1])
    assert replay["state"] == "outcome_unknown" and len(connected) == 1, "an unknown outcome is never retried"

    monkeypatch.setattr(inbox, "client", lambda account: Provider("provider_rejected"))
    data = json.loads(call(messaging, "inbox_reply", thread_ticket=ticket, message="Refused by the channel")[1])
    assert (data["ok"], data["state"], data["reason"], data["do_not_retry"]) == (False, "rejected", "provider_rejected", False)
    assert data["message"] == "The channel refused this request. Check the conversation in its original app."


def test_inbox_reply_on_personal_whatsapp_queues_for_the_owners_computer_and_refuses_when_it_is_off(messaging, wa, db, monkeypatch):
    peer = personal_whatsapp(db, wa)
    user, device = wa
    [row] = conversations(messaging, channel="whatsapp")
    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "")
    is_error, text = call(messaging, "inbox_reply", thread_ticket=row["thread_ticket"], message="Here is the link")
    assert is_error and text == OFF and count(InboxReply) == 0
    assert device("pending").json()["reply_ids"] == [], "nothing was queued for the connector"

    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "enabled")
    data = json.loads(call(messaging, "inbox_reply", thread_ticket=row["thread_ticket"], message="Here is the link")[1])
    assert (data["ok"], data["state"], data["message"]) == (True, "queued", "Your reply is queued for your personal connector.")
    assert "owner's computer" in data["note"]
    assert device("pending").json()["reply_ids"] == [data["request_id"]]      # the listener on his computer collects it
    job = device("replies/" + data["request_id"] + "/begin").json()
    assert job["dispatch"] and (job["thread_id"], job["channel"]) == (peer, "whatsapp")

    with session() as s:                                    # his computer is off: the screen's refusal, and nothing queued
        setting = s.get(AppSetting, direct.KEY)
        setting.value = {**setting.value, "seen": direct.iso(utcnow() - timedelta(minutes=10))}
        s.commit()
    refused(messaging, "inbox_reply", "Your personal connector is offline or catching up. Open it before replying.",
            thread_ticket=row["thread_ticket"], message="A later reply")
    assert count(InboxReply) == 1

    assert device("status").status_code == 200              # back on, but the person has typed STOP: no reply may be queued
    assert device("event", **wa_tests.event(id="baileys:m-stop", body="STOP")).json()["stored"]
    refused(messaging, "inbox_reply", "Check the WhatsApp recipient and keep the reply within 4,000 characters.",
            thread_ticket=row["thread_ticket"], message="After STOP")
    assert count(InboxReply) == 1


def test_overview_and_settings_say_whether_messaging_is_on(mcp, monkeypatch):
    assert read(mcp, "group_hq_overview")["messaging_enabled"] is False
    assert read(mcp, "settings_get")["messaging_enabled"] is False
    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "enabled")
    assert read(mcp, "group_hq_overview")["messaging_enabled"] is True
    assert (read(mcp, "settings_get")["writes_enabled"], read(mcp, "settings_get")["messaging_enabled"]) == (True, True)
