"""No database or live provider IO; public comments reuse the proven parser."""
import json
import unittest
from dataclasses import replace

import httpx

from app.services.inbox_providers import Account, InboxError, client
from app.services.linkedin_comments_provider import reply_target
from app.services.video_comments_provider import PROFILES, VideoCommentAccount, VideoComments, VideoSetup, target

ACCOUNT = VideoCommentAccount(key="mhp-youtube-public", brand="mhp", channel="youtube", label="MHP channel",
    provider="zernio", account_id="1" * 24, profile_id=PROFILES["mhp"], token="test-only", owner_email="owner@example.test", can_reply=True)
TIKTOK = replace(ACCOUNT, key="piuk-tiktok-public", brand="piuk", channel="tiktok", profile_id=PROFILES["piuk"])
REQUEST = "ghq-00000000-0000-4000-8000-000000000001"


class VideoProviderTests(unittest.TestCase):
    def adapter(self, body, account=ACCOUNT, seen=None):
        def response(request):
            if seen is not None:
                seen.append(request)
            return httpx.Response(200, json=body)
        return VideoComments(account, httpx.MockTransport(response))

    def test_both_platforms_use_existing_account_bound_public_parser(self):
        for a in (ACCOUNT, TIKTOK):
            with self.subTest(channel=a.channel):
                seen = []
                adapter = self.adapter({"data": [{"id": "video-1", "accountId": a.account_id,
                    "platform": a.channel, "content": "Public video"}], "meta": {"accountsQueried": 1}}, a, seen)
                posts, cursor = adapter.posts()
                self.assertEqual(posts[0]["id"], "video-1")
                self.assertIsNone(cursor)
                self.assertEqual(seen[0].url.params["platform"], a.channel)
                self.assertEqual(seen[0].url.params["profileId"], a.profile_id)
                self.assertIsInstance(client(a), VideoComments)

    def test_tiktok_developer_skip_is_not_an_empty_ready_inbox(self):
        body = {"data": [], "meta": {"accountsQueried": 0, "accountsSkipped": [
            {"accountId": TIKTOK.account_id, "platform": "tiktok"}]}}
        with self.assertRaisesRegex(InboxError, "business_connection_needed"):
            self.adapter(body, TIKTOK).posts()
        with self.assertRaisesRegex(InboxError, "provider_unavailable"):
            self.adapter({"data": [], "meta": {"accountsQueried": 0}}).posts()

    def test_wrong_brand_profile_private_account_or_platform_are_refused(self):
        for a in (replace(ACCOUNT, profile_id=PROFILES["pp"]), replace(ACCOUNT, channel="facebook"),
                  Account(**ACCOUNT.__dict__)):
            with self.assertRaises(InboxError):
                VideoComments(a)

    def test_video_comment_metadata_and_nested_targets_remain_bound(self):
        body = {"comments": [{"id": "comment-1", "platform": "youtube", "message": "Question",
            "canReply": True, "from": {"name": "Viewer"}, "replies": []}],
            "meta": {"accountId": ACCOUNT.account_id, "platform": "youtube", "postId": "video-1"}}
        comments, _ = self.adapter(body).comments("video-1")
        self.assertEqual(comments[0]["name"], "Viewer")
        body["meta"]["platform"] = "tiktok"
        with self.assertRaisesRegex(InboxError, "unbound_target"):
            self.adapter(body).comments("video-1")

    def test_public_reply_exact_endpoint_no_private_or_cross_platform_target(self):
        seen = []
        adapter = self.adapter({"success": True, "data": {"isReply": True, "commentId": "reply-1"}}, seen=seen)
        self.assertEqual(adapter.send(target(ACCOUNT, "video-1", "comment-1"), "Public reply", REQUEST), "reply-1")
        self.assertEqual(seen[0].url.path, "/api/v1/inbox/comments/video-1")
        self.assertEqual(json.loads(seen[0].content), {"accountId": ACCOUNT.account_id, "commentId": "comment-1", "message": "Public reply"})
        self.assertEqual(seen[0].headers["Idempotency-Key"], REQUEST)
        for private in ("private-thread", reply_target("video-1", "comment-1"), target(TIKTOK, "video-1", "comment-1")):
            with self.assertRaisesRegex(InboxError, "unbound_target"):
                adapter.send(private, "Private draft", REQUEST)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(ACCOUNT.identity, Account(**ACCOUNT.__dict__).identity)

    def test_ambiguous_reply_ack_is_unknown(self):
        with self.assertRaisesRegex(InboxError, "outcome_unknown"):
            self.adapter({"success": True}).send(target(ACCOUNT, "video-1", "comment-1"), "Public", REQUEST)

    def test_inventory_requires_real_profile_platform_and_drops_secrets(self):
        row = {"_id": ACCOUNT.account_id, "profileId": {"_id": ACCOUNT.profile_id}, "platform": "youtube",
               "username": "mhp", "isActive": True, "enabled": True, "accessToken": "must-not-leave-parser"}
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"accounts": [row]}))
        result = VideoSetup(ACCOUNT, transport).inventory()
        self.assertTrue(result[0]["connected"])
        self.assertNotIn("must-not-leave-parser", repr(result))
        row["profileId"] = PROFILES["pp"]
        with self.assertRaisesRegex(InboxError, "unbound_target"):
            VideoSetup(ACCOUNT, transport).inventory()

    def test_owner_connect_url_is_real_provider_response_and_bound_reconnect(self):
        seen = []
        def answer(request):
            seen.append(request)
            if request.url.path == "/api/v1/accounts":
                return httpx.Response(200, json={"accounts": [{"_id": TIKTOK.account_id,
                    "platform": "tiktok", "profileId": TIKTOK.profile_id, "username": "ukpropertyinvestment"}]})
            return httpx.Response(200, json={"authUrl": "https://zernio.com/connect/welcome?ticket=test-request"})
        url = VideoSetup(TIKTOK, httpx.MockTransport(answer)).connect_url("https://group.example.test/inbox/video-comments", TIKTOK.account_id)
        self.assertTrue(url.startswith("https://zernio.com/connect/welcome"))
        params = seen[-1].url.params
        self.assertEqual(params["profileId"], PROFILES["piuk"])
        self.assertEqual(params["reconnectAccountId"], TIKTOK.account_id)
        self.assertEqual(params["expectedUsername"], "ukpropertyinvestment")
        self.assertEqual(params["welcome"], "true")

    def test_connect_does_not_redirect_owner_to_an_untrusted_host(self):
        for url in ("https://evil.test/auth", "http://zernio.com/auth", "https://zernio.com.evil.test/auth"):
            setup = VideoSetup(ACCOUNT, httpx.MockTransport(lambda r: httpx.Response(200, json={"authUrl": url})))
            with self.subTest(url=url), self.assertRaises(InboxError):
                setup.connect_url("https://group.example.test/inbox/video-comments")


if __name__ == "__main__":
    unittest.main()
