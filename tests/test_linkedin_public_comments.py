"""Existing local-Postgres harness; provider IO is replaced, never sent."""
import json
import uuid

import pytest
from sqlalchemy import select

from app.main import app
from app.models import InboxReply, User
from app.routers import inbox_comments as routes
from app.services import inbox, inbox_comments as service
from app.services.inbox_providers import InboxError
from app.services.linkedin_comments_provider import COMPANIES, reply_target


@pytest.fixture
def public_connection(monkeypatch, db):
    if not any(getattr(r, "path", None) == service.PAGE for r in app.routes):
        app.include_router(routes.router)
    actor = db.scalars(select(User)).first()
    row = {"key": "pp-linkedin-public", "brand": "pp", "channel": "linkedin", "provider": "zernio",
        "label": "Property Predator", "account_id": COMPANIES["pp"][0], "profile_id": COMPANIES["pp"][1],
        "credential_env": "GHQ_INBOX_PUBLIC_TEST", "owner_email": actor.email, "can_reply": True}
    monkeypatch.setenv("GHQ_INBOX_PUBLIC_ACCOUNTS", json.dumps([row]))
    monkeypatch.setenv("GHQ_INBOX_PUBLIC_TEST", "test-comment-token")
    state = {"sends": [], "failure": None, "row": row, "actor": actor}

    class Fake:
        def __init__(self, account):
            self.account = account
        def posts(self, cursor=None):
            return [{"id": "post-1", "body": "Public company post", "occurred_at": "2026-11-09"}], None
        def comments(self, post, cursor=None):
            assert post == "post-1"
            return [{"id": "comment-1", "body": "<script>Public comment</script>", "name": "Public author",
                     "occurred_at": "2026-11-09", "depth": 0, "can_reply": True, "more_replies": False}], None
        def send(self, target, body, request_id):
            state["sends"].append((self.account.key, target, body, request_id))
            if state["failure"]:
                raise InboxError(state["failure"])
            return "accepted-public-comment"

    monkeypatch.setattr(routes, "client", Fake)
    monkeypatch.setattr(inbox, "client", Fake)
    return state


def form(state, **changes):
    actor = state["actor"]
    account = service.accounts(actor.email)[0]
    return {"target": service.ticket(account, "post-1", actor.id, comment_id="comment-1"),
            "request_id": str(uuid.uuid4()), "message": "Deliberate public reply",
            "audience": "public_linkedin", "acknowledge": "true", **changes}


def test_public_page_auth_company_and_public_only_composer(client, admin_client, public_connection):
    assert client.get(service.PAGE).status_code == 303
    actor = public_connection["actor"]
    account = service.accounts(actor.email)[0]
    value = service.ticket(account, "post-1", actor.id)
    page = admin_client.get(service.PAGE, params={"post": value})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Post public reply" in page.text and "&lt;script&gt;Public comment&lt;/script&gt;" in page.text
    assert 'name="audience" value="public_linkedin"' in page.text
    assert public_connection["sends"] == []


def test_public_reply_durable_journal_and_same_request_replay(admin_client, public_connection, db):
    data = form(public_connection)
    for _ in range(2):
        response = admin_client.post(service.PAGE + "/reply", data=data)
        assert response.status_code == 303
    assert len(public_connection["sends"]) == 1
    receipt = db.get(InboxReply, data["request_id"])
    assert receipt.state == "accepted" and receipt.provider_message_id == "accepted-public-comment"
    assert receipt.body is None and receipt.thread_id is None
    assert public_connection["sends"][0][1] == reply_target("post-1", "comment-1")
    response = admin_client.post(service.PAGE + "/reply", data={**data, "message": "Changed content"})
    assert response.status_code == 303 and "already+used" in response.headers["location"]
    assert len(public_connection["sends"]) == 1


def test_public_reply_unknown_is_not_retried(admin_client, public_connection, db):
    public_connection["failure"] = "outcome_unknown"
    data = form(public_connection)
    for _ in range(2):
        response = admin_client.post(service.PAGE + "/reply", data=data)
        assert response.status_code == 303 and "unknown" in response.headers["location"]
    assert len(public_connection["sends"]) == 1
    assert db.get(InboxReply, data["request_id"]).state == "outcome_unknown"


def test_public_requires_explicit_audience_ack_and_origin(admin_client, public_connection):
    for changes in ({"audience": "private"}, {"acknowledge": "false"}):
        assert admin_client.post(service.PAGE + "/reply", data=form(public_connection, **changes)).status_code == 400
    assert admin_client.post(service.PAGE + "/reply", data=form(public_connection),
        headers={"Origin": "https://foreign.example.test"}).status_code == 403
    assert public_connection["sends"] == []


def test_private_ticket_and_post_only_ticket_cannot_publish(admin_client, public_connection):
    actor = public_connection["actor"]
    account = service.accounts(actor.email)[0]
    for value in (inbox.ticket(account, "private-thread", actor.id), service.ticket(account, "post-1", actor.id)):
        assert admin_client.post(service.PAGE + "/reply", data=form(public_connection, target=value)).status_code == 409
    assert public_connection["sends"] == []


def test_public_owner_or_binding_change_revokes_old_composer(admin_client, public_connection, monkeypatch):
    data = form(public_connection)
    monkeypatch.setenv("GHQ_INBOX_PUBLIC_TEST", "rotated-test-token")
    assert admin_client.post(service.PAGE + "/reply", data=data).status_code == 409
    monkeypatch.setenv("GHQ_INBOX_PUBLIC_ACCOUNTS", json.dumps([{**public_connection["row"], "owner_email": "other@example.test"}]))
    assert admin_client.post(service.PAGE + "/reply", data=data).status_code == 409
    assert public_connection["sends"] == []
