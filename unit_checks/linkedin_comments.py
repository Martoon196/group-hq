"""No database or provider IO: python -m unittest discover -s unit_checks -p linkedin_comments.py."""
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from itsdangerous import URLSafeTimedSerializer
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import config
from app.services import inbox_comments as service
from app.services.inbox_providers import Account, InboxError, client, load_accounts
from app.services.linkedin_comments_provider import (
    COMPANIES, LinkedInComments, PublicCommentAccount, load_public_accounts, reply_target, unpack_target,
)

OWNER = "owner@example.test"
ROW = {"key": "pp-linkedin-public", "brand": "pp", "channel": "linkedin", "provider": "zernio",
       "label": "Property Predator", "account_id": COMPANIES["pp"][0], "profile_id": COMPANIES["pp"][1],
       "owner_email": OWNER, "credential_env": "GHQ_INBOX_TEST", "can_reply": True}
ENV = {"GHQ_INBOX_TEST": "test-only-token", "GHQ_INBOX_PUBLIC_ACCOUNTS": json.dumps([ROW])}
ACCOUNT = load_public_accounts(json.dumps([ROW]), ENV)[0]
POST = "urn:li:share:123"
COMMENT = "urn:li:comment:456"
TARGET = reply_target(POST, COMMENT)
REQUEST = "ghq-00000000-0000-4000-8000-000000000001"


def comment(id=COMMENT, **extra):
    return {"id": id, "platform": "linkedin", "message": "Public question", "createdTime": "2026-11-09T14:00:00Z",
            "from": {"name": "Public author"}, "canReply": True, "replies": [], **extra}


def comments_body(**extra):
    return {"comments": [comment()], "meta": {"accountId": ACCOUNT.account_id,
            "platform": "linkedin", "postId": POST}, "pagination": {"hasMore": False}, **extra}


class PublicRegistryTests(unittest.TestCase):
    def test_explicit_companies_only_and_owner_scoped(self):
        self.assertIsInstance(ACCOUNT, PublicCommentAccount)
        self.assertTrue(ACCOUNT.configured)
        with patch.dict("os.environ", ENV):
            self.assertEqual(service.accounts(OWNER.upper()), (ACCOUNT,))
            self.assertEqual(service.accounts("another@example.test"), ())
        for change in ({"account_id": "6a95e83277555aae01639b98"}, {"profile_id": "another-profile"},
                       {"brand": "mhp"}, {"channel": "facebook"}, {"provider": "local"},
                       {"key": "private-linkedin"}, {"owner_email": ""}, {"credential_env": "ZERNIO_API_KEY"}):
            with self.subTest(change=change), self.assertRaises(InboxError):
                load_public_accounts(json.dumps([{**ROW, **change}]), ENV)

    def test_registry_never_uses_collector_credential_and_rejects_duplicates(self):
        a = load_public_accounts(json.dumps([ROW]), {"ZERNIO_API_KEY": "collector-only"})[0]
        self.assertFalse(a.configured)
        with self.assertRaises(InboxError):
            load_public_accounts(json.dumps([ROW, ROW]), ENV)
        with self.assertRaises(InboxError):
            load_accounts(json.dumps([ROW]), ENV)

    def test_reuse_requires_matching_private_profile_and_brand(self):
        company = {**ROW, "credential_env": "GHQ_INBOX_REUSE_PROFILE"}
        private = {**ROW, "channel": "facebook", "key": "pp-fb", "account_id": "private-id"}
        def configured(**overrides):
            env = {**ENV, "GHQ_INBOX_ACCOUNTS": json.dumps([{**private, **overrides}])}
            return load_public_accounts(json.dumps([company]), env)[0].configured
        self.assertTrue(configured())
        self.assertFalse(configured(profile_id="other-profile"))
        self.assertFalse(configured(owner_email="another@example.test"))
        self.assertFalse(configured(brand="mhp"))

    def test_public_binding_is_distinct_and_rotation_invalidates(self):
        private = Account(**ACCOUNT.__dict__)
        self.assertNotEqual(private.binding, ACCOUNT.binding)
        self.assertNotEqual(private.identity, ACCOUNT.identity)
        self.assertNotEqual(replace(ACCOUNT, token="rotated").binding, ACCOUNT.binding)
        self.assertNotIn(ACCOUNT.token, repr(ACCOUNT))

    def test_tickets_bound_to_owner_actor_account_and_surface(self):
        with patch.dict("os.environ", ENV):
            value = service.ticket(ACCOUNT, POST, 7, comment_id=COMMENT)
            self.assertEqual(service.resolve(value, 7, OWNER, for_reply=True)[1]["comment"], COMMENT)
            for actor, email in ((8, OWNER), (7, "other@example.test")):
                with self.assertRaises(HTTPException):
                    service.resolve(value, actor, email, for_reply=True)
            for bad in (value + "x", URLSafeTimedSerializer(config.SECRET_KEY, salt="ghq-inbox-thread-v1").dumps(
                    {"account": ACCOUNT.key, "binding": ACCOUNT.binding, "thread": TARGET, "user": 7})):
                with self.assertRaises(HTTPException):
                    service.resolve(bad, 7, OWNER, for_reply=True)
            with patch.dict("os.environ", {"GHQ_INBOX_TEST": "rotated"}), self.assertRaises(HTTPException):
                service.resolve(value, 7, OWNER, for_reply=True)

    def test_post_ticket_cannot_send_and_reply_ticket_cannot_select_post(self):
        with patch.dict("os.environ", ENV):
            post = service.ticket(ACCOUNT, POST, 7)
            reply = service.ticket(ACCOUNT, POST, 7, comment_id=COMMENT)
            for value, replying in ((post, True), (reply, False)):
                with self.assertRaises(HTTPException):
                    service.resolve(value, 7, OWNER, for_reply=replying)

    def test_public_audience_acknowledgement_required(self):
        service.validate_audience("public_linkedin", True)
        for audience, ack in (("private", True), ("", True), ("public_linkedin", False)):
            with self.assertRaises(HTTPException):
                service.validate_audience(audience, ack)


class LinkedInProviderTests(unittest.TestCase):
    def adapter(self, response, seen=None):
        def answer(request):
            if seen is not None:
                seen.append(request)
            return httpx.Response(200, json=response)
        return LinkedInComments(ACCOUNT, httpx.MockTransport(answer))

    def test_dispatch_is_separate_from_private_dm_provider(self):
        self.assertIsInstance(client(ACCOUNT), LinkedInComments)
        with self.assertRaises(InboxError):
            LinkedInComments(Account(**ACCOUNT.__dict__))

    def test_posts_exact_company_profile_platform_and_cursor(self):
        seen = []
        body = {"data": [{"id": POST, "accountId": ACCOUNT.account_id, "platform": "linkedin",
                          "content": "Public company post", "createdTime": "2026-11-09T14:00:00Z"}],
                "pagination": {"hasMore": True, "nextCursor": "next-page"}}
        posts, cursor = self.adapter(body, seen).posts("prior-page")
        self.assertEqual(posts[0]["id"], POST)
        self.assertEqual(cursor, "next-page")
        req = seen[0]
        self.assertEqual(req.url.host, "zernio.com")
        self.assertEqual(req.url.path, "/api/v1/inbox/comments")
        self.assertEqual(req.url.params["accountId"], ACCOUNT.account_id)
        self.assertEqual(req.url.params["profileId"], ACCOUNT.profile_id)
        self.assertEqual(req.url.params["platform"], "linkedin")
        self.assertEqual(req.url.params["cursor"], "prior-page")

    def test_posts_reject_cross_account_platform_ad_and_failed_account(self):
        valid = {"id": POST, "accountId": ACCOUNT.account_id, "platform": "linkedin"}
        for row in ({**valid, "accountId": "another"}, {**valid, "platform": "facebook"}, {**valid, "isAd": True}):
            with self.subTest(row=row), self.assertRaisesRegex(InboxError, "unbound_target"):
                self.adapter({"data": [row]}).posts()
        with self.assertRaisesRegex(InboxError, "provider_unavailable"):
            self.adapter({"data": [], "meta": {"accountsFailed": 1}}).posts()

    def test_comments_flatten_nested_replies_and_bind_post(self):
        seen = []
        body = comments_body(comments=[comment(replies=[comment("nested", canReply=False)])],
                             pagination={"hasMore": True, "cursor": "next-comments"})
        comments, cursor = self.adapter(body, seen).comments(POST)
        self.assertEqual([r["depth"] for r in comments], [0, 1])
        self.assertFalse(comments[1]["can_reply"])
        self.assertEqual(cursor, "next-comments")
        self.assertIn("urn%3Ali%3Ashare%3A123", seen[0].url.raw_path.decode())

    def test_comments_reject_wrong_metadata_and_nested_binding(self):
        for key, value in (("accountId", "another"), ("platform", "facebook"), ("postId", "another-post")):
            body = comments_body()
            body["meta"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(InboxError, "unbound_target"):
                self.adapter(body).comments(POST)
        with self.assertRaisesRegex(InboxError, "unbound_target"):
            self.adapter(comments_body(comments=[comment(replies=[comment("nested", accountId="another")])])).comments(POST)

    def test_empty_post_and_comment_pages_are_valid(self):
        self.assertEqual(self.adapter({"data": [], "pagination": {"hasMore": False}}).posts(), ([], None))
        self.assertEqual(self.adapter(comments_body(comments=[])).comments(POST), ([], None))

    def test_incomplete_pagination_or_permission_data_are_refused(self):
        for body in (comments_body(pagination={"hasMore": True}), comments_body(comments=[comment(canReply="true")])):
            with self.assertRaisesRegex(InboxError, "invalid_provider_response"):
                self.adapter(body).comments(POST)

    def test_public_reply_exact_comment_and_idempotency_no_dm_path(self):
        seen = []
        provider_id = self.adapter({"success": True, "data": {"isReply": True, "commentId": "new-comment"}}, seen).send(
            TARGET, "Deliberately public reply", REQUEST)
        self.assertEqual(provider_id, "new-comment")
        req = seen[0]
        self.assertEqual(req.method, "POST")
        self.assertNotIn("conversations", req.url.path)
        self.assertEqual(json.loads(req.content), {"accountId": ACCOUNT.account_id,
                        "commentId": COMMENT, "message": "Deliberately public reply"})
        self.assertEqual(req.headers["Idempotency-Key"], REQUEST)

    def test_private_thread_and_disabled_reply_never_call_provider(self):
        seen = []
        adapter = self.adapter({}, seen)
        for value in ("private-thread", "[]"):
            with self.assertRaises(InboxError):
                adapter.send(value, "Private body", REQUEST)
        disabled = LinkedInComments(replace(ACCOUNT, can_reply=False), adapter.transport)
        with self.assertRaisesRegex(InboxError, "reply_not_enabled"):
            disabled.send(TARGET, "Reply", REQUEST)
        self.assertEqual(seen, [])

    def test_bad_ack_server_failure_redirect_or_timeout_are_unknown(self):
        for body in ({"success": True, "data": {"isReply": False, "commentId": "x"}}, {"success": False}, {}):
            with self.assertRaisesRegex(InboxError, "outcome_unknown"):
                self.adapter(body).send(TARGET, "Public reply", REQUEST)
        for status in (302, 408, 500):
            adapter = LinkedInComments(ACCOUNT, httpx.MockTransport(lambda r: httpx.Response(status)))
            with self.subTest(status=status), self.assertRaisesRegex(InboxError, "outcome_unknown"):
                adapter.send(TARGET, "Public reply", REQUEST)
        def timeout(request):
            raise httpx.ReadTimeout("test", request=request)
        with self.assertRaisesRegex(InboxError, "outcome_unknown"):
            LinkedInComments(ACCOUNT, httpx.MockTransport(timeout)).send(TARGET, "Public reply", REQUEST)

    def test_target_is_canonical_public_tuple(self):
        self.assertEqual(unpack_target(TARGET), (POST, COMMENT))
        for value in (TARGET + "=", "not base64", "W10"):
            with self.assertRaises(InboxError):
                unpack_target(value)

    def test_template_public_audience_blank_composer_and_escaping(self):
        env = Environment(loader=FileSystemLoader(config.APP_DIR / "templates"), autoescape=select_autoescape())
        html = env.get_template("inbox_comments.html").render(s={}, user=None, accounts=[ACCOUNT], account=ACCOUNT,
            brands={"pp": "Property Predator"}, post_ticket="signed-post", post_id=POST,
            comments=[{**comment(), "name": "<script>not-executed</script>", "body": "<script>public</script>",
                       "reply_ticket": "signed-comment", "request_id": "request"}])
        self.assertIn("Post public reply", html)
        self.assertIn('name="audience" value="public_linkedin"', html)
        self.assertIn('name="acknowledge" value="true" required', html)
        self.assertIn("&lt;script&gt;public&lt;/script&gt;", html)
        self.assertNotIn("<script>public</script>", html)
        self.assertRegex(html, r'<textarea[^>]*></textarea>')


if __name__ == "__main__":
    unittest.main()
