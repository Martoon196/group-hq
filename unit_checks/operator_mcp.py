"""The MCP server at /mcp without a database: handshake, tool listing, a read tool over fixed rows, and the bearer gate."""
import json
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("SECRET_KEY", "unit-test-only")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import config  # noqa: E402
from app.models import CampaignLead, CampaignPlan, User  # noqa: E402
from app.routers import operator_mcp as mcp  # noqa: E402
from app.routers import operator_oauth as oauth  # noqa: E402
from app.services import operator_oauth_store as oauth_store  # noqa: E402
from app.services import operator_tools as tools  # noqa: E402

STATIC = "static-operator-token-synthetic-0123456789abcdef"
SECRET = "operator-oauth-secret-synthetic-0123456789abcdef"
FOUNDER = SimpleNamespace(id=1, email="founder@example.test", is_active=True, must_change_password=False)
LEADS = [SimpleNamespace(id=7, brand="pp", source_system="relaunch72", display_name="Test Lead", source="form", lifecycle="lead", stage="New",
                         external_id="59e2f6cf-c35e-4a23-991f-012d29fbecba", next_task_at=None, last_activity_at=None,
                         opportunity_status="open", value_minor=250000, synced_at=datetime(2026, 11, 9, 10, 0))]
PLANS = [SimpleNamespace(id=3, lead_id=7, state="active", hold_reason="", started_at=datetime(2026, 11, 9, 11, 0), held_at=None,
                         blueprint_key="pp_first_conversation", blueprint_version=1, actions=[], activities=[])]


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):           # filter, filter_by, order_by, limit, options, join: all chainable
        return lambda *a, **k: self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def count(self):
        return len(self.rows)


class FakeSession:
    ROWS = {User: [FOUNDER], CampaignLead: LEADS, CampaignPlan: PLANS}
    committed = rolled_back = False

    def query(self, model):
        return FakeQuery(self.ROWS.get(model, []))

    def get(self, model, key):
        return next((r for r in self.ROWS.get(model, []) if r.id == key), None)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_app():
    app = FastAPI()
    app.include_router(oauth.router)
    app.include_router(mcp.router)
    return app


class OperatorMcpTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(config, "GHQ_OPERATOR_TOKEN", STATIC), patch.object(config, "GHQ_OPERATOR_OAUTH_SECRET", SECRET),
                        patch.object(config, "GHQ_OPERATOR_USER_EMAIL", "founder@example.test"),
                        patch.object(config, "GHQ_OPERATOR_WRITES", ""), patch.object(config, "GHQ_OPERATOR_MESSAGING", ""),
                        patch.object(config, "BASE_URL", "https://hq.example.test"),
                        patch.object(mcp, "SessionLocal", FakeSession)]
        for p in self.patches:
            p.start()
        self.client = TestClient(make_app())

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def rpc(self, method, params=None, *, bearer=STATIC, id_=1):
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        message = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            message["params"] = params
        return self.client.post("/mcp", json=message, headers=headers)

    def test_no_bearer_gets_401_with_the_resource_metadata_pointer(self):
        response = self.rpc("initialize", bearer=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "unauthorised"})
        self.assertEqual(response.headers["www-authenticate"],
                         'Bearer realm="Group HQ", resource_metadata="https://hq.example.test/.well-known/oauth-protected-resource"')
        self.assertEqual(self.rpc("initialize", bearer=STATIC[:-1] + "X").status_code, 401)
        self.assertEqual(self.client.get("/mcp").status_code, 405)

    def test_initialize_ping_and_notifications(self):
        response = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertEqual(result["serverInfo"], {"name": "group-hq", "version": "1"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.rpc("ping").json(), {"jsonrpc": "2.0", "id": 1, "result": {}})
        note = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                headers={"Authorization": f"Bearer {STATIC}"})
        self.assertEqual((note.status_code, note.content), (202, b""))
        self.assertEqual(self.rpc("resources/list").json()["error"]["code"], -32601)
        bad = self.client.post("/mcp", content=b"{nope", headers={"Authorization": f"Bearer {STATIC}"})
        self.assertEqual(bad.json()["error"]["code"], -32700)
        self.assertEqual(self.rpc("tools/call", {"name": 5}).json()["error"]["code"], -32602)
        self.assertEqual(self.rpc("tools/call", {"name": "no_such_tool"}).json()["error"]["code"], -32602)

    def test_tools_list_hides_write_tools_until_writes_are_enabled(self):
        names = [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]
        self.assertEqual(names, [t.name for t in tools.READ_TOOLS])
        self.assertNotIn("crm_start_plan", names)
        for tool in self.rpc("tools/list").json()["result"]["tools"]:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertTrue(tool["description"])
        refused = self.rpc("tools/call", {"name": "crm_hold_plan", "arguments": {"plan_id": 3, "reason": "x"}}).json()["result"]
        self.assertTrue(refused["isError"])
        self.assertIn("writes are disabled", refused["content"][0]["text"])
        with patch.object(config, "GHQ_OPERATOR_WRITES", "enabled"):
            names = [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]
        self.assertEqual(names, [t.name for t in tools.READ_TOOLS + tools.WRITE_TOOLS])

    def test_messaging_tools_need_writes_and_messaging_both_enabled(self):
        def names():
            return [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]

        def reply():
            return self.rpc("tools/call", {"name": "inbox_reply", "arguments": {"thread_ticket": "t", "message": "m"}}).json()["result"]
        messaging = [t.name for t in tools.MESSAGING_TOOLS]
        self.assertEqual(messaging, ["whatsapp_keyword_rule_save", "inbox_reply"])
        off = "sending messages is switched off on this deployment (GHQ_OPERATOR_MESSAGING)"
        with patch.object(config, "GHQ_OPERATOR_MESSAGING", "enabled"):        # messaging alone: still read only
            self.assertFalse(config.operator_messaging_enabled())
            self.assertEqual(names(), [t.name for t in tools.READ_TOOLS])
            self.assertEqual((reply()["isError"], reply()["content"][0]["text"]), (True, off))
        with patch.object(config, "GHQ_OPERATOR_WRITES", "enabled"):           # writes alone: no messaging tool
            self.assertEqual(names(), [t.name for t in tools.READ_TOOLS + tools.WRITE_TOOLS])
            self.assertEqual((reply()["isError"], reply()["content"][0]["text"]), (True, off))
            with patch.object(config, "GHQ_OPERATOR_MESSAGING", "enabled"):
                self.assertTrue(config.operator_messaging_enabled())
                self.assertEqual(names(), [t.name for t in tools.READ_TOOLS + tools.WRITE_TOOLS + tools.MESSAGING_TOOLS])
                listed = {t["name"]: t for t in self.rpc("tools/list").json()["result"]["tools"]}
                for name in messaging:
                    self.assertEqual(listed[name]["annotations"], {"readOnlyHint": False, "openWorldHint": True})
                self.assertNotIn("annotations", listed["crm_start_plan"])
        self.assertFalse(any(t.messaging for t in tools.READ_TOOLS + tools.WRITE_TOOLS))

    def test_read_tool_over_fixed_rows_with_the_static_token(self):
        response = self.rpc("tools/call", {"name": "crm_leads", "arguments": {"limit": 10}})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["result"]
        self.assertFalse(result["isError"])
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["count"], 1)
        lead = data["leads"][0]
        self.assertEqual((lead["id"], lead["name"], lead["stage"], lead["synced_at"]), (7, "Test Lead", "New", "2026-11-09T10:00:00"))
        self.assertEqual(lead["plan"], {"id": 3, "state": "active", "hold_reason": None, "started_at": "2026-11-09T11:00:00"})
        self.assertNotIn(STATIC, response.text)
        bad = self.rpc("tools/call", {"name": "crm_leads", "arguments": {"limit": 0}}).json()["result"]
        self.assertTrue(bad["isError"])
        self.assertIn("limit", bad["content"][0]["text"])
        missing = self.rpc("tools/call", {"name": "crm_lead", "arguments": {"lead_id": 99}}).json()["result"]
        self.assertTrue(missing["isError"])
        self.assertEqual(missing["content"][0]["text"], "Campaign lead 99 was not found")

    def test_an_oauth_access_token_acts_as_its_subject_and_a_batch_is_answered_in_order(self):
        access, expired = "a" * 43, "e" * 43
        live = patch.object(oauth_store, "resolve", lambda db, token: FOUNDER if token == access else None)
        live.start()
        self.addCleanup(live.stop)
        batch = [{"jsonrpc": "2.0", "id": "a", "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": "b", "method": "tools/call", "params": {"name": "crm_lead", "arguments": {"lead_id": 7}}}]
        response = self.client.post("/mcp", json=batch, headers={"Authorization": f"Bearer {access}"})
        self.assertEqual(response.status_code, 200, response.text)
        replies = response.json()
        self.assertEqual([r["id"] for r in replies], ["a", "b"])
        detail = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(detail["name"], "Test Lead")
        self.assertEqual(detail["plan"]["id"], 3)
        self.assertEqual(self.rpc("ping", bearer=expired).status_code, 401)


if __name__ == "__main__":
    unittest.main()
