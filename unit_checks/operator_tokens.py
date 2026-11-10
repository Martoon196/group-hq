"""Signed client ids without a database: round trip, tampering, other secrets. Codes and tokens are opaque values
kept as hashes in Postgres; tests/test_operator_oauth.py covers them."""
import os
import unittest

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("SECRET_KEY", "unit-test-only")

from app.services import operator_tokens as tokens  # noqa: E402

SECRET = "operator-oauth-secret-synthetic-0123456789abcdef"
OTHER = "operator-oauth-secret-synthetic-fedcba9876543210"


class OperatorTokenTests(unittest.TestCase):
    def test_client_ids_round_trip_in_the_handoff_byte_style(self):
        client = tokens.sign(tokens.CLIENT, {"uris": ["https://claude.ai/api/mcp/auth_callback"], "name": "Claude", "iat": 1000}, secret=SECRET)
        self.assertNotIn("=", client)
        self.assertTrue(client.isascii())
        self.assertEqual(tokens.verify(tokens.CLIENT, client, secret=SECRET, now=10**10)["name"], "Claude")
        self.assertNotIn(SECRET, client)
        for bad in (client[:-2] + "zz", "not.a.token", "", "x" * 5000):
            with self.assertRaises(tokens.TokenError):
                tokens.verify(tokens.CLIENT, bad, secret=SECRET)
        with self.assertRaises(tokens.TokenError):
            tokens.verify(tokens.CLIENT, client, secret=OTHER)

    def test_only_client_ids_are_signed(self):
        self.assertEqual(tokens.KINDS, (tokens.CLIENT,))
        for kind in ("ghq-oauth-code", "ghq-oauth-access", "ghq-oauth-refresh"):
            with self.assertRaises(ValueError):
                tokens.sign(kind, {"sub": "x"}, secret=SECRET)

    def test_an_unconfigured_or_short_secret_never_signs(self):
        for secret in ("", "short"):
            with self.assertRaises(tokens.TokenError):
                tokens.sign(tokens.CLIENT, {"uris": []}, secret=secret)


if __name__ == "__main__":
    unittest.main()
