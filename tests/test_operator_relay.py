"""Relaunch72's operator tools carried by the Group HQ connector (app.services.relaunch72_operator), over POST /mcp.

Relaunch72 is a stand-in here: an httpx.MockTransport that answers as its operator endpoint does, so the request this
server really sends (address, bearer, the admin it names) is checked, and nothing real is called. Covered: the bearer
both servers must derive identically; the switch; what tools/list adds and for whom; the write gate; a call passed
through, with the audit row a write leaves; and each way Relaunch72 can fail to answer, where the sentence must say
truthfully whether anything could have changed."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.models import AuditLog
from app.routers import operator_mcp
from app.services import operator_tools
from app.services import relaunch72_operator as relay
from tests.conftest import ADMIN_EMAIL, session

TOKEN = "static-operator-token-synthetic-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SHARED = "group-hq-shared-secret-synthetic-0123456789abcdef"
# Pinned in Relaunch72 too (orchestrator/test/operator.test.ts): both servers must derive exactly this from SHARED.
DERIVED = "vSvqRI-KLf62_AAErvHo3X0_isVsT-aXT9jMwmRUNxs"
OBJECT = {"type": "object", "properties": {"brand": {"type": "string", "enum": ["pp", "mhp"]}}, "required": ["brand"]}
REMOTE = [
    {"name": "workspace_overview", "description": "The brand workspace.", "inputSchema": OBJECT},
    {"name": "webinars_list", "description": "All webinars on the brand workspace with registration counts.", "inputSchema": OBJECT},
    {"name": "webinar_get", "description": "One webinar in full.", "inputSchema": OBJECT},
    {"name": "webinar_create", "description": "Create a webinar (draft).", "inputSchema": OBJECT},
    {"name": "crm_create_lead", "description": "Create a CRM lead.", "inputSchema": OBJECT},
    {"name": "crm_leads", "description": "A name Group HQ already uses.", "inputSchema": OBJECT},
    {"name": "something_new", "description": "A tool Relaunch72 added later.", "inputSchema": OBJECT},
]
REMOTE_READS = ["workspace_overview", "webinars_list", "webinar_get"]
REMOTE_WRITES = ["webinar_create", "crm_create_lead", "something_new"]


class Relaunch72:
    """What the stand-in was asked, and how it answers."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.tools = REMOTE
        self.status = 200
        self.fail: Exception | None = None
        self.result = {"content": [{"type": "text", "text": json.dumps({"ok": True, "id": "w-1"})}], "isError": False}
        self.raw: bytes | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "unauthorised"})
        if self.raw is not None:
            return httpx.Response(200, content=self.raw, headers={"content-type": "application/json"})
        message = json.loads(request.content)
        result = {"tools": self.tools} if message["method"] == "tools/list" else self.result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def calls(self, method: str) -> list[dict]:
        return [m for m in (json.loads(r.content) for r in self.requests) if m["method"] == method]


@pytest.fixture
def r72(monkeypatch):
    """The relay switched on against a stand-in Relaunch72; Group HQ's writes off."""
    remote = Relaunch72()
    monkeypatch.setattr(config, "GHQ_OPERATOR_TOKEN", TOKEN)
    monkeypatch.setattr(config, "GHQ_OPERATOR_USER_EMAIL", ADMIN_EMAIL)
    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "")
    monkeypatch.setattr(config, "GHQ_OPERATOR_MESSAGING", "")
    monkeypatch.setattr(config, "GHQ_OPERATOR_RELAUNCH72", "enabled")
    monkeypatch.setattr(config, "R72_SSO_SECRET", SHARED)
    monkeypatch.setattr(config, "R72_PP_HOST", "hq.propertypredator.com")
    monkeypatch.setattr(relay, "_client", lambda timeout: httpx.Client(transport=httpx.MockTransport(remote.handler),
                                                                      timeout=timeout, follow_redirects=False))
    relay.forget()
    yield remote
    relay.forget()


@pytest.fixture
def mcp(r72):
    app = FastAPI()
    app.include_router(operator_mcp.router)
    return TestClient(app)


@pytest.fixture
def writes(monkeypatch, mcp):
    monkeypatch.setattr(config, "GHQ_OPERATOR_WRITES", "enabled")
    return mcp


def rpc(c: TestClient, method: str, params: dict | None = None) -> dict:
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 7, "method": method, **({"params": params} if params is not None else {})},
               headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def names(c: TestClient) -> list[str]:
    return [t["name"] for t in rpc(c, "tools/list")["result"]["tools"]]


def call(c: TestClient, tool: str, /, **arguments) -> tuple[bool, str]:
    result = rpc(c, "tools/call", {"name": tool, "arguments": arguments})["result"]
    return result["isError"], result["content"][0]["text"]


def audits() -> list[tuple[str, str]]:
    with session() as db:
        return [(row.action, row.entity) for row in db.query(AuditLog).order_by(AuditLog.id).all()]


# ---- the bearer and the switch ----------------------------------------------------------------------------------------
def test_the_bearer_is_the_value_relaunch72_derives():
    assert relay.bearer(SHARED) == DERIVED
    assert relay.bearer("  " + SHARED + "\n") == DERIVED, "Relaunch72 trims its copy of the secret, so this side does too"
    assert relay.bearer(SHARED) != SHARED and "=" not in relay.bearer(SHARED)


def test_the_relay_needs_its_switch_and_the_handoff_secret(monkeypatch, r72, mcp):
    assert relay.enabled()
    for switch, secret in (("", SHARED), ("disabled", SHARED), ("enabled", ""), ("enabled", "too-short")):
        monkeypatch.setattr(config, "GHQ_OPERATOR_RELAUNCH72", switch)
        monkeypatch.setattr(config, "R72_SSO_SECRET", secret)
        assert not relay.enabled()
        assert names(mcp) == [t.name for t in operator_tools.READ_TOOLS]
        assert rpc(mcp, "tools/call", {"name": "webinar_get", "arguments": {}})["error"]["message"] == "unknown tool: webinar_get"
    assert r72.requests == [], "while the relay is off Relaunch72 is never contacted"


# ---- tools/list -------------------------------------------------------------------------------------------------------
def test_the_request_names_the_admin_and_carries_the_derived_bearer(r72, mcp):
    names(mcp)
    [request] = r72.requests
    assert str(request.url) == "https://hq.propertypredator.com/api/operator/mcp" and request.method == "POST"
    assert request.headers["authorization"] == f"Bearer {DERIVED}"
    assert request.headers["x-group-hq-admin"] == ADMIN_EMAIL
    assert json.loads(request.content) == {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def test_read_only_deployment_lists_only_relaunch72_tools_known_to_change_nothing(r72, mcp):
    listed = names(mcp)
    own = [t.name for t in operator_tools.READ_TOOLS if t.name != "webinars_list"]
    assert listed == own + REMOTE_READS
    assert listed.count("webinars_list") == 1 and listed.count("crm_leads") == 1
    assert not set(REMOTE_WRITES) & set(listed), "an unknown Relaunch72 tool counts as a write"


def test_with_writes_on_every_relaunch72_tool_is_listed_and_labelled(r72, writes):
    tools = {t["name"]: t for t in rpc(writes, "tools/list")["result"]["tools"]}
    assert set(REMOTE_READS + REMOTE_WRITES) <= set(tools)
    assert tools["webinar_create"]["description"] == relay.PREFIX + "Create a webinar (draft)."
    assert tools["webinar_create"]["inputSchema"] == OBJECT
    assert tools["webinars_list"]["description"].startswith(relay.PREFIX), "Relaunch72's fuller webinars_list replaces Group HQ's"
    assert not tools["crm_leads"]["description"].startswith(relay.PREFIX), "any other clash keeps Group HQ's own tool"


def test_the_tool_list_is_remembered_between_calls(r72, mcp):
    names(mcp), names(mcp), call(mcp, "webinar_get", brand="pp")
    assert len(r72.calls("tools/list")) == 1


@pytest.mark.parametrize("status", [401, 403])
def test_an_admin_relaunch72_does_not_serve_is_offered_nothing(r72, mcp, status):
    r72.status = status
    assert names(mcp) == [t.name for t in operator_tools.READ_TOOLS]
    assert rpc(mcp, "tools/call", {"name": "webinar_get", "arguments": {}})["error"]["message"] == "unknown tool: webinar_get"
    is_error, text = call(mcp, "webinars_list")
    assert not is_error and "brands" in json.loads(text), "Group HQ's own webinars_list still answers"
    assert len(r72.requests) == 1, "the refusal is remembered"


@pytest.mark.parametrize("broken", [
    [{"name": "Bad-Name", "description": "x", "inputSchema": OBJECT}],
    [{"name": "webinar_get", "description": "x", "inputSchema": {"type": "string"}}],
    [{"name": "webinar_get", "description": 5, "inputSchema": OBJECT}],
    [{"name": "webinar_get", "description": "x", "inputSchema": OBJECT}] * 2,
    "not a list",
])
def test_a_tool_list_that_is_not_what_mcp_returns_adds_nothing(r72, mcp, broken):
    r72.tools = broken
    assert names(mcp) == [t.name for t in operator_tools.READ_TOOLS]


def test_while_relaunch72_is_down_group_hq_still_lists_and_says_so_on_a_call(r72, mcp):
    r72.fail = httpx.ConnectError("no route")
    assert names(mcp) == [t.name for t in operator_tools.READ_TOOLS]
    assert call(mcp, "webinar_get", brand="pp") == (True, relay.UNAVAILABLE)
    assert len(r72.requests) == 1, "the outage is remembered briefly, so tools/list is not slowed every time"


# ---- tools/call -------------------------------------------------------------------------------------------------------
def test_a_read_tool_is_passed_through_and_leaves_no_audit_row(r72, mcp):
    r72.result = {"content": [{"type": "text", "text": json.dumps({"webinar": {"id": "w-9", "title": "Land"}})}], "isError": False}
    is_error, text = call(mcp, "webinar_get", brand="mhp", webinarId="w-9")
    assert not is_error and json.loads(text) == {"webinar": {"id": "w-9", "title": "Land"}}
    [sent] = r72.calls("tools/call")
    assert sent["params"] == {"name": "webinar_get", "arguments": {"brand": "mhp", "webinarId": "w-9"}}
    assert r72.requests[-1].headers["x-group-hq-admin"] == ADMIN_EMAIL
    assert audits() == []


def test_webinars_list_goes_to_relaunch72_for_the_admin_it_serves(r72, mcp):
    r72.result = {"content": [{"type": "text", "text": json.dumps({"webinars": []})}], "isError": False}
    assert call(mcp, "webinars_list", brand="pp") == (False, json.dumps({"webinars": []}))
    assert r72.calls("tools/call")[0]["params"]["name"] == "webinars_list"


def test_a_write_needs_group_hqs_writes_and_nothing_is_sent_without_them(r72, mcp):
    names(mcp)
    is_error, text = call(mcp, "webinar_create", brand="mhp", title="Land masterclass")
    assert is_error and "writes are disabled" in text
    assert r72.calls("tools/call") == [] and audits() == []


def test_a_write_is_passed_through_once_and_audited_here(r72, writes):
    is_error, text = call(writes, "webinar_create", brand="mhp", title="Land masterclass")
    assert not is_error and json.loads(text) == {"ok": True, "id": "w-1"}
    assert len(r72.calls("tools/call")) == 1
    assert audits() == [("operator.tool", "tool:relaunch72/webinar_create")]


def test_relaunch72s_own_refusal_comes_back_as_its_sentence(r72, writes):
    r72.result = {"content": [{"type": "text", "text": "That webinar changed since you read it."}], "isError": True}
    assert call(writes, "webinar_create", brand="mhp") == (True, "That webinar changed since you read it.")
    assert audits() == [("operator.tool", "tool:relaunch72/webinar_create")], "it reached Relaunch72, so it is on the record"


def test_a_write_that_never_left_says_nothing_changed(r72, writes):
    names(writes)
    r72.fail = httpx.ConnectError("no route")
    assert call(writes, "webinar_create", brand="mhp") == (True, relay.UNAVAILABLE)
    assert audits() == []


@pytest.mark.parametrize("how", ["timeout", "status", "body", "shape"])
def test_a_write_with_no_readable_answer_says_the_outcome_is_unknown(r72, writes, how):
    names(writes)
    if how == "timeout":
        r72.fail = httpx.ReadTimeout("slow")
    elif how == "status":
        r72.status = 502
    elif how == "body":
        r72.raw = b"<html>bad gateway</html>"
    else:
        r72.result = {"content": [{"type": "image", "data": "..."}], "isError": False}
    assert call(writes, "crm_create_lead", brand="pp") == (True, relay.UNKNOWN)
    assert len(r72.calls("tools/call")) <= 1, "never retried"
    assert audits() == [("operator.tool", "tool:relaunch72/crm_create_lead")]


def test_a_call_relaunch72_refuses_changes_nothing_and_the_list_is_read_again(r72, writes):
    names(writes)
    r72.status = 401
    assert call(writes, "webinar_create", brand="mhp") == (True, relay.REFUSED)
    assert audits() == []
    assert "webinar_create" not in names(writes)


def test_an_oversized_answer_is_not_passed_on(monkeypatch, r72, mcp):
    names(mcp)
    monkeypatch.setattr(relay, "MAX_RESULT_BYTES", 200)
    r72.result = {"content": [{"type": "text", "text": "x" * 500}], "isError": False}
    assert call(mcp, "webinar_get", brand="pp") == (True, relay.UNKNOWN)


def test_a_redirect_is_never_followed(r72, mcp):
    r72.status = 302
    assert names(mcp) == [t.name for t in operator_tools.READ_TOOLS]
    assert len(r72.requests) == 1
