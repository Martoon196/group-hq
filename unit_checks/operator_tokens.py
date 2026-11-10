"""Signed operator tokens without a database: round trip, kind confusion, expiry, single-use codes."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("SECRET_KEY", "unit-test-only")

from app.services import operator_tokens as tokens  # noqa: E402

SECRET = "operator-oauth-secret-synthetic-0123456789abcdef"
OTHER = "operator-oauth-secret-synthetic-fedcba9876543210"


class OperatorTokenTests(unittest.TestCase):
    def setUp(self):
        tokens.reset_consumed_for_tests()

    def test_sign_and_verify_round_trip_in_the_handoff_byte_style(self):
        token = tokens.sign(tokens.ACCESS, {"sub": "founder@example.test", "scope": "operator", "iat": 1000, "exp": 4600, "jti": "x"},
                            secret=SECRET)
        body, dot, mac = token.partition(".")
        self.assertEqual(dot, ".")
        self.assertNotIn("=", token)
        self.assertTrue(token.isascii())
        claims = tokens.verify(tokens.ACCESS, token, secret=SECRET, now=2000)
        self.assertEqual(claims["sub"], "founder@example.test")
        self.assertNotIn(SECRET, token)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, token, secret=OTHER, now=2000)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, token[:-2] + "zz", secret=SECRET, now=2000)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, "not.a.token", secret=SECRET, now=2000)

    def test_a_token_of_one_kind_is_refused_as_another(self):
        claims = {"sub": "founder@example.test", "scope": "operator", "iat": 1000, "exp": 4600, "jti": "x"}
        refresh = tokens.sign(tokens.REFRESH, claims, secret=SECRET)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, refresh, secret=SECRET, now=2000)
        code = tokens.sign(tokens.CODE, {**claims, "n": "nonce"}, secret=SECRET)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, code, secret=SECRET, now=2000)
        self.assertEqual(tokens.verify(tokens.REFRESH, refresh, secret=SECRET, now=2000)["jti"], "x")

    def test_expiry_is_enforced_and_a_client_id_needs_none(self):
        token = tokens.sign(tokens.ACCESS, {"sub": "a@example.test", "iat": 1000, "exp": 4600}, secret=SECRET)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, token, secret=SECRET, now=4600)
        no_exp = tokens.sign(tokens.ACCESS, {"sub": "a@example.test", "iat": 1000}, secret=SECRET)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.ACCESS, no_exp, secret=SECRET, now=1001)
        client = tokens.sign(tokens.CLIENT, {"uris": ["https://claude.ai/api/mcp/auth_callback"], "name": "Claude", "iat": 1000}, secret=SECRET)
        self.assertEqual(tokens.verify(tokens.CLIENT, client, secret=SECRET, now=10**10)["name"], "Claude")

    def test_an_unconfigured_or_short_secret_never_signs(self):
        for secret in ("", "short"):
            with self.assertRaises(tokens.TokenError):
                tokens.sign(tokens.ACCESS, {"exp": 1}, secret=secret)

    def test_a_code_nonce_is_consumed_once_and_the_set_stays_bounded(self):
        self.assertTrue(tokens.consume_code("n1", exp=2000, now=1900))
        self.assertFalse(tokens.consume_code("n1", exp=2000, now=1901))
        self.assertTrue(tokens.consume_code("n2", exp=2000, now=1901))
        # an expired nonce is forgotten, since its code could not be exchanged anyway
        self.assertTrue(tokens.consume_code("n1", exp=2100, now=2001))
        for i in range(tokens.CONSUMED_CAP + 50):
            tokens.consume_code(f"flood{i}", exp=10**9, now=3000)
        self.assertLessEqual(len(tokens._consumed), tokens.CONSUMED_CAP)


if __name__ == "__main__":
    unittest.main()
