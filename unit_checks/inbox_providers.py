"""Run without a database: python -m unittest discover -s unit_checks -p inbox_providers.py."""
import json
import unittest
from dataclasses import replace

import httpx

from app.services.inbox_providers import Account, InboxError, Matrix, Zernio, load_accounts

REQUEST = "ghq-00000000-0000-4000-8000-000000000001"
ACCOUNT = Account("kp-facebook", "kp", "facebook", "Key Properties", "zernio", "page-account",
                  profile_id="key-properties", token="test-inbox-token", can_reply=True)
ROOM = {"id": "!room:test", "label": "Test contact", "bridge_bot": "@facebookbot:test",
        "bridge_state_key": "fi.mau.facebook://test/thread", "protocol_id": "facebook"}
MATRIX = Account("personal-facebook", "kp", "facebook", "Personal Messenger", "matrix", "personal",
                 token="test-matrix-token", origin="https://matrix.example.test", user_id="@martin:test",
                 rooms=(ROOM,), can_reply=True)


def state():
    return [{"type": "m.room.member", "state_key": MATRIX.user_id, "content": {"membership": "join"}},
            {"type": "m.bridge", "state_key": ROOM["bridge_state_key"], "sender": ROOM["bridge_bot"],
             "content": {"protocol": {"id": "facebook"}}}]


class InboxProviderTests(unittest.TestCase):
    def test_registry_no_paid_broker_or_collector_key(self):
        row = {"key": ACCOUNT.key, "brand": "kp", "channel": "facebook", "provider": "zernio", "label": ACCOUNT.label,
               "account_id": ACCOUNT.account_id, "profile_id": ACCOUNT.profile_id, "credential_env": "GHQ_INBOX_SOCIAL"}
        result = load_accounts(json.dumps([row]), {"ZERNIO_API_KEY": "collector-only"})
        self.assertFalse(result[0].configured)
        for bad in ({**row, "provider": "unipile"}, {**row, "credential_env": "ZERNIO_API_KEY"},
                    {**row, "channel": "youtube"}, {**row, "brand": "unknown"}):
            with self.subTest(bad=bad), self.assertRaises(InboxError):
                load_accounts(json.dumps([bad]), {})
        with self.assertRaises(InboxError):
            load_accounts(json.dumps([row, row]), {})

    def test_registry_self_hosted_origin_is_operator_https_only(self):
        row = {"key": MATRIX.key, "brand": "kp", "channel": "facebook", "provider": "matrix", "label": MATRIX.label,
               "account_id": "personal", "credential_env": "GHQ_INBOX_MATRIX", "user_id": MATRIX.user_id,
               "rooms": [ROOM], "origin": MATRIX.origin}
        self.assertTrue(load_accounts(json.dumps([row]), {"GHQ_INBOX_MATRIX": "test-token"})[0].configured)
        for origin in ("http://localhost:8008", "https://user:pass@host.test", "https://host.test/path", "https://host.test?token=x"):
            with self.subTest(origin=origin), self.assertRaises(InboxError):
                load_accounts(json.dumps([{**row, "origin": origin}]), {})

    def test_binding_changes_on_remap_rotation_or_send_access(self):
        for changed in (replace(ACCOUNT, brand="pp"), replace(ACCOUNT, token="rotated"),
                        replace(ACCOUNT, account_id="another-account"), replace(ACCOUNT, can_reply=False)):
            self.assertNotEqual(changed.binding, ACCOUNT.binding)
        self.assertNotIn(ACCOUNT.token, repr(ACCOUNT))

    def test_zernio_list_bound_account_and_no_fake_email(self):
        seen = []
        def answer(request):
            seen.append(request)
            return httpx.Response(200, json={"data": [{"id": "thread/one", "accountId": ACCOUNT.account_id,
                "platform": "facebook", "participantId": "native-person-id", "participantName": "Durham seller",
                "lastMessage": "Hello", "updatedTime": "2026-11-06T13:00:00Z"}], "pagination": {"hasMore": True}})
        threads, more = Zernio(ACCOUNT, httpx.MockTransport(answer)).conversations()
        self.assertTrue(more)
        self.assertEqual(threads[0]["participant_id"], "native-person-id")
        self.assertEqual(seen[0].url.host, "zernio.com")
        self.assertEqual(seen[0].url.params["accountId"], ACCOUNT.account_id)
        self.assertEqual(seen[0].url.params["profileId"], ACCOUNT.profile_id)
        self.assertEqual(seen[0].headers["Authorization"], "Bearer " + ACCOUNT.token)

    def test_cross_account_or_channel_response_is_refused(self):
        for row in ({"accountId": "another", "platform": "facebook"},
                    {"accountId": ACCOUNT.account_id, "platform": "instagram"}):
            with self.subTest(row=row), self.assertRaisesRegex(InboxError, "unbound_target"):
                Zernio(ACCOUNT, httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [row]}))).conversations()

    def test_thread_messages_are_bound_and_latest_page_displayed_in_order(self):
        def answer(request):
            self.assertIn("thread%2Fone", request.url.raw_path.decode())
            self.assertEqual(request.url.params["sortOrder"], "desc")
            return httpx.Response(200, json={"messages": [{"id": "m2", "conversationId": "thread/one",
                "accountId": ACCOUNT.account_id, "platform": "facebook", "direction": "incoming", "message": "New"},
                {"id": "m1", "conversationId": "thread/one", "accountId": ACCOUNT.account_id,
                 "platform": "facebook", "direction": "outgoing", "message": "Old"}], "pagination": {"nextCursor": "opaque"}})
        messages, cursor = Zernio(ACCOUNT, httpx.MockTransport(answer)).messages("thread/one")
        self.assertEqual([m["id"] for m in messages], ["m1", "m2"])
        self.assertEqual(cursor, "opaque")

    def test_send_uses_original_account_and_idempotency_key(self):
        def answer(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["Idempotency-Key"], REQUEST)
            self.assertEqual(json.loads(request.content), {"accountId": ACCOUNT.account_id, "message": "Requested test"})
            return httpx.Response(200, json={"data": {"messageId": "native-message"}})
        self.assertEqual(Zernio(ACCOUNT, httpx.MockTransport(answer)).send("thread/one", "Requested test", REQUEST), "native-message")

    def test_timeout_bad_ack_or_server_failure_never_claim_delivery(self):
        for status, content in ((200, b"not json"), (200, b'{}'), (503, b'{}')):
            with self.subTest(status=status, content=content), self.assertRaisesRegex(InboxError, "outcome_unknown"):
                Zernio(ACCOUNT, httpx.MockTransport(lambda r: httpx.Response(status, content=content))).send("thread", "Test", REQUEST)
        def timeout(request):
            raise httpx.ReadTimeout("private provider details")
        with self.assertRaisesRegex(InboxError, "^outcome_unknown$"):
            Zernio(ACCOUNT, httpx.MockTransport(timeout)).send("thread", "Test", REQUEST)

    def test_no_auto_redirect_or_following_credentials(self):
        seen = []
        def redirect(request):
            seen.append(request)
            return httpx.Response(302, headers={"Location": "https://evil.test"})
        with self.assertRaisesRegex(InboxError, "provider_rejected"):
            Zernio(ACCOUNT, httpx.MockTransport(redirect)).conversations()
        self.assertEqual(len(seen), 1)

    def test_no_send_when_disabled_or_invalid(self):
        def forbidden(request):
            self.fail("No provider request permitted")
        for a, body, key in ((replace(ACCOUNT, can_reply=False), "Test", REQUEST),
                             (ACCOUNT, "", REQUEST), (ACCOUNT, "a" * 10001, REQUEST), (ACCOUNT, "Test", "bad")):
            with self.subTest(body=body[:20]), self.assertRaises(InboxError):
                Zernio(a, httpx.MockTransport(forbidden)).send("thread", body, key)

    def test_matrix_original_room_and_owner_proof_with_transaction_id(self):
        seen = []
        def answer(request):
            seen.append(request)
            if request.url.path.endswith("/whoami"):
                return httpx.Response(200, json={"user_id": MATRIX.user_id})
            if request.url.path.endswith("/state"):
                return httpx.Response(200, json=state())
            self.assertEqual(request.method, "PUT")
            self.assertTrue(request.url.path.endswith("/send/m.room.message/" + REQUEST))
            self.assertEqual(json.loads(request.content), {"msgtype": "m.text", "body": "Requested reply"})
            return httpx.Response(200, json={"event_id": "$matrix-event"})
        self.assertEqual(Matrix(MATRIX, httpx.MockTransport(answer)).send(ROOM["id"], "Requested reply", REQUEST), "$matrix-event")
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(r.url.host == "matrix.example.test" for r in seen))

    def test_matrix_wrong_owner_or_bridge_or_encryption_blocks_send(self):
        for variant in ("owner", "bridge", "encryption", "membership", "protocol"):
            calls = []
            def answer(request):
                calls.append(request)
                self.assertEqual(request.method, "GET")
                if request.url.path.endswith("/whoami"):
                    return httpx.Response(200, json={"user_id": "@wrong:test" if variant == "owner" else MATRIX.user_id})
                events = state()
                if variant == "bridge": events[1]["sender"] = "@imposter:test"
                if variant == "protocol": events[1]["content"]["protocol"]["id"] = "linkedin"
                if variant == "membership": events[0]["content"]["membership"] = "leave"
                if variant == "encryption": events.append({"type": "m.room.encryption", "content": {"algorithm": "m.megolm.v1.aes-sha2"}})
                return httpx.Response(200, json=events)
            with self.subTest(variant=variant), self.assertRaises(InboxError):
                Matrix(MATRIX, httpx.MockTransport(answer)).send(ROOM["id"], "Test", REQUEST)

    def test_matrix_never_accepts_unmapped_room(self):
        with self.assertRaisesRegex(InboxError, "unbound_target"):
            Matrix(MATRIX, httpx.MockTransport(lambda r: self.fail("No request"))).send("!other:test", "Test", REQUEST)


if __name__ == "__main__":
    unittest.main()
