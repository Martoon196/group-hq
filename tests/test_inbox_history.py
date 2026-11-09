from datetime import datetime, timezone
from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.jobs.inbox_history import account_page, message_page
from app.models import User
from app.services.inbox_archive import store_history
from app.services.inbox_providers import Account, InboxError
from app.services.video_comments_provider import PROFILES, VideoSetup, VideoComments, VideoCommentAccount
from tests.conftest import session


def make_message(i, year=2012):
    return {"id": str(i), "body": "Archived " + str(i), "direction": "incoming", "name": "Sender",
            "occurred_at": f"{year}-01-01T12:00:00Z", "revision": ""}


def test_personal_history_survives_restart_and_conflicting_replay(db, monkeypatch, admin_client):
    user = db.scalars(select(User)).first()
    account = Account("test", "group", "facebook", "Test Messenger", "local", "acct", token="test", owner_email=user.email)
    monkeypatch.setattr("app.services.inbox.accounts", lambda db: [account])
    for i in range(3):
        messages = [make_message(n) for n in range(100*i,100*i+100)]
        packet = {"thread": {"id": "thread", "participant_id": "peer", "name": "Archive test", "messages": messages,
                             "is_group": False, "is_merged": False},
                  "observed_at": datetime.now(timezone.utc).isoformat(), "oldest_id": messages[0]["id"],
                  "newest_id": messages[-1]["id"], "chunk_id": str(i)*64, "reached_start": i==2}
        with session() as fresh:
            assert store_history(fresh,user,account,packet)["stored"] == 100
    assert store_history(db,user,account,packet)["reached_start"]
    assert db.scalar(text("SELECT count(*) FROM inbox_history_messages")) == 300
    assert db.scalar(text("SELECT count(*) FROM inbox_replies")) == 0
    assert admin_client.get("/inbox/history?q=Archived+299").status_code == 200
    assert "300 messages" in admin_client.get("/inbox/history").text
    thread_id = db.scalar(text("SELECT thread_key FROM inbox_history_threads"))
    page = admin_client.get("/inbox/history",params={"account":account.key,"thread":thread_id,"page":6})
    assert "2012" in page.text and "Previous page" in page.text
    monkeypatch.setattr("app.services.inbox.accounts", lambda db: [replace(account,owner_email="other@example.test")])
    assert admin_client.get("/inbox/history",params={"account":account.key,"thread":thread_id}).status_code == 404
    packet["thread"]["messages"][0]["body"] = "Conflicting packet"
    with pytest.raises(HTTPException) as exc:
        store_history(db,user,account,packet)
    assert exc.value.status_code == 409


def test_official_cursor_survives_rate_limit_and_restart(db):
    user = db.scalars(select(User)).first()
    account = Account("ig","mhp","instagram","Instagram","zernio","acct",token="test")
    class Provider:
        retry = True
        def conversation_page(self, cursor):
            if cursor is None:
                return [{"id":str(i),"participant_id":str(i),"name":"A","updated_at":"2026"} for i in range(100)],True,"next"
            assert cursor == "next"
            if self.retry:
                self.retry = False
                raise InboxError("rate_limited")
            return [{"id":"100","participant_id":"100","name":"B","updated_at":"2026"}],False,None
        def messages(self, thread, cursor):
            return ([make_message(i) for i in range(100)],"more") if not cursor else ([make_message(101,2009)],None)
    api = Provider()
    for _ in range(3):
        with session() as fresh:
            fresh.execute(text("UPDATE inbox_history_accounts SET next_attempt=now()"))
            account_page(fresh,user,account,api)
    assert db.scalar(text("SELECT count(*) FROM inbox_history_threads")) == 101
    # Give a single thread both turns while preserving every other queued thread.
    db.execute(text("UPDATE inbox_history_threads SET last_attempt=now()+interval '1 hour' WHERE native_id<>'0'"))
    db.commit()
    assert message_page(db,user,account,api)
    with session() as fresh:
        fresh.execute(text("UPDATE inbox_history_threads SET last_attempt=NULL WHERE native_id='0'"))
        assert message_page(fresh,user,account,api)
    assert db.scalar(text("SELECT count(*) FROM inbox_history_messages")) == 101
    assert db.scalar(text("SELECT min(sent_at) FROM inbox_history_messages")).year == 2009
    assert db.scalar(text("SELECT reached_start FROM inbox_history_threads WHERE native_id='0'"))


def test_metadata_key_is_limited_to_setup(monkeypatch):
    monkeypatch.setenv("GHQ_INBOX_ZERNIO_ACCOUNTS_KEY","setup-only")
    base = VideoCommentAccount("key","pp","youtube","Account","zernio","1"*24,PROFILES["pp"],"comments-key")
    assert VideoSetup(base).account.token == "setup-only"
    assert VideoComments(base).account.token == "comments-key"
    with pytest.raises(InboxError):
        VideoSetup(replace(base,profile_id="other"))
