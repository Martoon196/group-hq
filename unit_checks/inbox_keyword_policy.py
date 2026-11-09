"""Offline keyword and delivery-identity checks for enabled inbox rules."""
import unittest
from datetime import timedelta
from types import SimpleNamespace

from app.services.inbox_keyword_policy import matching_rule, receipt_id, timestamp


class KeywordPolicyTests(unittest.TestCase):
    def setUp(self):
        self.start = timestamp("2026-11-09T12:00:00Z")
        self.fresh = self.start + timedelta(seconds=1)

    def rule(self, keyword, **options):
        return {"keyword": keyword, "enabled": True, "since": self.start.isoformat(), **options}

    def test_whole_phrase_matches_case_and_whitespace(self):
        rule = self.rule("book a viewing")
        self.assertEqual(matching_rule([rule], "BOOK  a\nviewing?!", self.fresh), rule)
        for text in ("scrapbook a viewing", "book a viewings", "book every viewing", "I mentioned book a viewing yesterday"):
            self.assertIsNone(matching_rule([rule], text, self.fresh))

    def test_overlaps_choose_one_specific_reply(self):
        rules = [self.rule("info"), self.rule("property info"), self.rule("info please")]
        self.assertEqual(matching_rule(rules, "Property info please", self.fresh, exact=False), rules[1])

    def test_old_or_paused_rules_are_ineligible(self):
        self.assertIsNone(matching_rule([self.rule("yes")], "yes", self.start))
        self.assertIsNone(matching_rule([self.rule("yes", enabled=False)], "yes", self.fresh))
        self.assertIsNone(matching_rule([self.rule("yes", since="")], "yes", self.fresh))

    def test_provider_times_need_an_unambiguous_instant(self):
        self.assertIsNone(timestamp("2026-11-09 12:00:00"))
        self.assertIsNone(timestamp("Yesterday"))
        self.assertEqual(timestamp("2026-11-09T13:00:00+01:00"), self.start)

    def test_receipt_is_scoped_to_original_native_account_thread_and_message(self):
        a = SimpleNamespace(identity="native-a")
        request = receipt_id(a, "1", "m1")
        self.assertEqual(request, receipt_id(a, "1", "m1"))
        for account, thread, message in ((a, "2", "m1"), (a, "1", "m2"), (SimpleNamespace(identity="native-b"), "1", "m1")):
            self.assertNotEqual(receipt_id(account, thread, message), request)


if __name__ == "__main__":
    unittest.main()
