import json
import uuid

import pytest
from sqlalchemy import select

from app import config
from app.models import AppSetting, InboxReply, User
from app.routers import video_comments as routes
from app.services import inbox, video_comments as service
from app.services.inbox_providers import InboxError
from app.services.linkedin_comments_provider import reply_target
from app.services.video_comments_provider import PROFILES


@pytest.fixture
def video(monkeypatch, db):
    user = db.scalars(select(User)).first()
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", user.email)
    monkeypatch.setenv("GHQ_INBOX_VIDEO_TEST", "test-only-inbox-credential")
    monkeypatch.setenv("GHQ_INBOX_ACCOUNTS", json.dumps([{"key": brand + "-ig", "brand": brand,
        "channel": "instagram", "provider": "zernio", "label": brand, "account_id": brand + "-account",
        "profile_id": profile, "credential_env": "GHQ_INBOX_VIDEO_TEST"} for brand, profile in PROFILES.items()]))
    state = {"user": user, "sends": [], "connects": [], "unsupported": False, "empty": False, "unknown": False}

    class Setup:
        def __init__(self, base):
            self.base = base
        def inventory(self):
            return [] if state["empty"] else [{"id": "1" * 24, "name": "<script>Public channel</script>",
                "username": "owned-channel", "connected": True}]
        def connect_url(self, redirect, reconnect=None):
            state["connects"].append((self.base.brand, self.base.channel, redirect, reconnect))
            return "https://zernio.com/connect/welcome?ticket=test-only-request"

    class Comments:
        def __init__(self, account):
            self.account = account
        def posts(self, cursor=None):
            if state["unsupported"]:
                raise InboxError("business_connection_needed")
            return [{"id": "video-1", "body": "Public video", "occurred_at": "2026-11-09"}], None
        def comments(self, post, cursor=None):
            return [{"id": "comment-1", "body": "<script>Question</script>", "name": "Public viewer",
                "occurred_at": "2026-11-09", "can_reply": True, "depth": 0, "more_replies": False}], None
        def send(self, value, body, request_id):
            state["sends"].append((self.account.key, value, body, request_id))
            if state["unknown"]:
                raise InboxError("outcome_unknown")
            return "accepted-public-reply"

    for module in (service, routes):
        monkeypatch.setattr(module, "VideoSetup", Setup)
        monkeypatch.setattr(module, "VideoComments", Comments)
    monkeypatch.setattr(inbox, "client", Comments)
    return state


def enable(video, db, brand="mhp", channel="youtube", replies=True):
    user = video["user"]
    base = service.scope(brand, channel, user)
    value = service.ticket(base, user, "enable", id="1" * 24)
    return service.enable(db, user, value, True, replies)


def reply_form(video, account, **changes):
    return {"value": service.ticket(account, video["user"], "reply", post="video-1", comment="comment-1"),
        "request_id": str(uuid.uuid4()), "message": "A deliberate public reply", "audience": "public_" + account.channel,
        "acknowledge": "true", **changes}


def test_owner_setup_works_with_no_channel_and_starts_real_flow(client, admin_client, video):
    assert client.get(service.PAGE).status_code == 303
    video["empty"] = True
    page = admin_client.get(service.PAGE)
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "No YouTube account is connected" in page.text and "Connect YouTube for Martin Howard Property" in page.text
    callback = admin_client.get(service.PAGE, params={"error": "no_youtube_channel", "error_message": "private-provider-detail"})
    assert "Brand Account that owns" in callback.text and "private-provider-detail" not in callback.text
    response = admin_client.post(service.PAGE + "/connect", data={"brand": "mhp", "channel": "youtube"})
    assert response.status_code == 303 and response.headers["location"].startswith("https://zernio.com/connect/welcome")
    assert video["connects"][0][0:2] == ("mhp", "youtube") and video["sends"] == []


def test_owner_enable_requires_explicit_choice_and_verified_comment_capability(admin_client, video, db):
    base = service.scope("piuk", "tiktok", video["user"])
    value = service.ticket(base, video["user"], "enable", id="1" * 24)
    assert admin_client.post(service.PAGE + "/enable", data={"value": value}).status_code == 400
    video["unsupported"] = True
    response = admin_client.post(service.PAGE + "/enable", data={"value": value, "acknowledge": "true"})
    assert response.status_code == 303 and "posting-only" in response.headers["location"]
    assert db.get(AppSetting, service.registry_key(video["user"])) is None
    video["unsupported"] = False
    current = enable(video, db, "piuk", "tiktok")
    saved = db.get(AppSetting, service.registry_key(video["user"]))
    assert current.brand == "piuk" and current.profile_id == PROFILES["piuk"]
    assert "test-only-inbox-credential" not in json.dumps(saved.value)


def test_public_read_reply_is_escaped_blank_and_journaled_once(admin_client, video, db):
    current = enable(video, db)
    post = service.ticket(current, video["user"], "post", post="video-1")
    page = admin_client.get(service.PAGE, params={"post": post})
    assert page.status_code == 200 and "&lt;script&gt;Question&lt;/script&gt;" in page.text
    assert 'name="audience" value="public_youtube"' in page.text and "Post public reply" in page.text
    assert "></textarea>" in page.text and "Private draft" not in page.text
    data = reply_form(video, current)
    for _ in range(2):
        assert admin_client.post(service.PAGE + "/reply", data=data).status_code == 303
    assert len(video["sends"]) == 1
    receipt = db.get(InboxReply, data["request_id"])
    assert receipt.state == "accepted" and receipt.body is None and receipt.thread_id is None
    assert receipt.provider_message_id == "accepted-public-reply"
    response = admin_client.post(service.PAGE + "/reply", data={**data, "message": "Changed content"})
    assert "already+used" in response.headers["location"] and len(video["sends"]) == 1


def test_private_tickets_wrong_audience_wrong_brand_and_rotation_cannot_publish(admin_client, video, db, monkeypatch):
    current = enable(video, db)
    data = reply_form(video, current)
    assert admin_client.post(service.PAGE + "/reply", data={**data, "audience": "private"}).status_code == 400
    assert admin_client.post(service.PAGE + "/reply", data={**data, "acknowledge": "false"}).status_code == 400
    private = inbox.ticket(current, "private-thread", video["user"].id)
    assert admin_client.post(service.PAGE + "/reply", data={**data, "value": private}).status_code == 409
    # A LinkedIn public target cannot be pasted into a video reply composer either.
    assert admin_client.post(service.PAGE + "/reply", data={**data, "value": reply_target("video-1", "comment-1")}).status_code == 409
    post = service.ticket(current, video["user"], "post", post="video-1")
    assert admin_client.get(service.PAGE, params={"brand": "pp", "post": post}).status_code == 409
    monkeypatch.setenv("GHQ_INBOX_VIDEO_TEST", "rotated-test-credential")
    assert admin_client.post(service.PAGE + "/reply", data=data).status_code == 409
    assert video["sends"] == []


def test_public_unknown_receipt_never_retries(admin_client, video, db):
    current = enable(video, db)
    video["unknown"] = True
    data = reply_form(video, current)
    for _ in range(2):
        response = admin_client.post(service.PAGE + "/reply", data=data)
        assert response.status_code == 303 and "unknown" in response.headers["location"]
    assert len(video["sends"]) == 1 and db.get(InboxReply, data["request_id"]).state == "outcome_unknown"


def test_owner_origin_and_observed_inventory_are_required(admin_client, video, db, monkeypatch):
    base = service.scope("mhp", "youtube", video["user"])
    value = service.ticket(base, video["user"], "enable", id="2" * 24)
    response = admin_client.post(service.PAGE + "/enable", data={"value": value, "acknowledge": "true"})
    assert response.status_code == 303 and "reconnect" in response.headers["location"]
    assert admin_client.post(service.PAGE + "/connect", data={"brand": "mhp", "channel": "youtube"},
        headers={"Origin": "https://foreign.example.test"}).status_code == 403
    monkeypatch.setattr(config, "GHQ_PERSONAL_INBOX_OWNER_EMAIL", "other@example.test")
    assert admin_client.get(service.PAGE).status_code == 403
    assert db.get(AppSetting, service.registry_key(video["user"])) is None
