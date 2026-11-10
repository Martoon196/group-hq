import unittest
from unittest.mock import patch

from app.services import relaunch72_sso as sso

SECRET = "group-hq-shared-secret-synthetic-0123456789abcdef"
# The same inputs signed by orchestrator/src/portal/group-hq-sso.ts (its test pins this token too).
VECTOR = ("eyJ2IjoiZ2hxLWhhbmRvZmYtMSIsImVtYWlsIjoiZm91bmRlckBleGFtcGxlLnRlc3QiLCJicmFuZCI6Im1ocCIsImhvc3QiOiJhcHAubWFydGluaG93YXJkcHJvcGVydHkuY28udWsiLCJwYXRoIjoiL3BvcnRhbC93ZWJpbmFycyIsImlhdCI6MTc5NDMwMTIwMCwiZXhwIjoxNzk0MzAxMjkwLCJub25jZSI6Im5ubm5ubm5ubm5ubm5ubm5ubm5ubm5ubiJ9"
          ".cSgbazHxniiCnWLaQEm5KLOo2QtFlP5ApIgCMmTHO-k")


class HandoffTokenTests(unittest.TestCase):
    def test_matches_the_relaunch72_test_vector_byte_for_byte(self):
        with patch.object(sso, "LIFETIME_SECONDS", 90):
            token = sso.sign("Founder@Example.test", "mhp", "/portal/webinars", secret=SECRET, now=1794301200,
                             nonce="n" * 24)
        self.assertEqual(token, VECTOR)

    def test_unconfigured_or_bad_inputs_never_sign(self):
        for kwargs in (dict(secret=""), dict(secret="short")):
            with self.assertRaises(sso.HandoffUnavailable):
                sso.sign("founder@example.test", "pp", "/portal/webinars", **kwargs)
        for email, brand, path in (("not-an-email", "pp", "/portal"), ("founder@example.test", "kp", "/portal"),
                                   ("founder@example.test", "pp", "https://evil.example/portal"),
                                   ("founder@example.test", "pp", "//evil.example/portal"),
                                   ("founder@example.test", "pp", "/portal//x"), ("founder@example.test", "pp", "/admin")):
            with self.assertRaises(sso.HandoffUnavailable):
                sso.sign(email, brand, path, secret=SECRET)

    def test_redirect_target_is_the_brand_host_handoff_route(self):
        target = sso.handoff("founder@example.test", "pp", "/portal/webinars/abc/host", secret=SECRET)
        self.assertEqual(target.host, "hq.propertypredator.com")
        self.assertTrue(target.url.startswith("https://hq.propertypredator.com/portal/auth/group-hq?t="))
        self.assertNotIn(SECRET, target.url)
        self.assertEqual(sso.host_for("mhp"), "app.martinhowardproperty.co.uk")


if __name__ == "__main__":
    unittest.main()
