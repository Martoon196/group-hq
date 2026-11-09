from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import config
from app.models import AppSetting, User
from app.services import inbox_claude, inbox_scanner, inbox_triage


def msg(text, direction="incoming"):
    return {"body": text, "direction": direction, "id": "msg"}


@pytest.mark.parametrize("text,kind", [
    ("Would you like us to handle your marketing? We help businesses like yours generate leads.", "pitch"),
    ("How are the kids, fancy a pint at the pub tonight?", "personal"),
    ("I'm looking for bridging finance on a development project", "business"),
    ("OK", "unclassified"),
])
def test_local_triage(text, kind):
    assert inbox_triage.analyse([msg(text)])["kind"] == kind


def test_conflicting_directions_and_multiple_brands():
    assert inbox_triage.analyse([msg("We offer website development and lead generation", "outgoing")])["kind"] == "unclassified"
    result = inbox_triage.analyse([msg("I need a letting agent and bridging finance for the next project")])
    assert result["kind"] == "business" and set(result["brands"]) == {"hnh", "piuk"}
    assert inbox_triage.analyse([msg("I need a refund on my course")])["needs_owner"]


def test_claude_limits_and_handoff(monkeypatch):
    monkeypatch.setenv("GHQ_INBOX_ANTHROPIC_API_KEY", "fixture")
    import json, httpx
    sent = []
    def provider(url, **kw):
        sent.append(kw)
        data = {"kind":"business","brands":["kp"],"intent":"Complaint","reason":"Customer wants a refund",
                "needs_owner":False,"draft":"We will refund you"}
        return httpx.Response(200, request=httpx.Request("POST", url), json={"stop_reason":"end_turn","content":[{"type":"text","text":json.dumps(data)}]})
    res = inbox_claude.analyse_with_claude([msg("password: sample-secret")]*15 + [msg("I want a refund")], "kp", provider)
    assert res["needs_owner"] and not res["draft"]
    payload = json.loads(sent[0]["json"]["messages"][0]["content"])
    assert "sample-secret" not in str(payload)
    assert len(payload["messages"]) == 12 and all(set(row) == {"text","direction"} for row in payload["messages"])
    assert "tools" not in sent[0]["json"]


def test_scope_and_manual_override():
    a = SimpleNamespace(binding="one")
    b = SimpleNamespace(binding="two")
    assert len({inbox_scanner.result_key(1,a,"x"), inbox_scanner.result_key(2,a,"x"),
                inbox_scanner.result_key(1,b,"x"), inbox_scanner.result_key(1,a,"y")}) == 4
    assert inbox_scanner.effective_kind({"kind":"business"}, {"kind":"personal"}) == "personal"
    assert inbox_scanner.message_stamp([msg("first")]) != inbox_scanner.message_stamp([msg("later")])


def test_scan_skips_unchanged_without_projecting(monkeypatch, db):
    user = db.scalar(select(User))
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", user.email)
    account = SimpleNamespace(binding="x", brand="kp", provider="local", owner_email=user.email)
    t = {"id":"1","updated_at":"now","preview":"Hi","account":account}
    monkeypatch.setattr(inbox_scanner.inbox,"snapshot", lambda *args, **kw: ([], [], [t]))
    monkeypatch.setattr(inbox_scanner.inbox,"local_thread", lambda *args: SimpleNamespace(messages=[msg("Are you coming for dinner tonight?")]))
    monkeypatch.delenv("GHQ_INBOX_ANTHROPIC_API_KEY", raising=False)
    inbox_scanner.save_value(db,inbox_scanner.setting_key(user.id),{"enabled":True},user.email)
    db.commit()
    assert inbox_scanner.scan_cycle(db,user) == 1
    assert inbox_scanner.scan_cycle(db,user) == 0
    result = db.get(AppSetting,inbox_scanner.result_key(user.id,account,"1")).value
    assert result["kind"] == "personal" and result["source"] == "Local classifier"
