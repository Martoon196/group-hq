import unittest

import httpx

from app.services.relaunch72_crm import CrmUnavailable, fetch_leads


class CrmReadTests(unittest.TestCase):
    def test_unconfigured_never_calls_product(self):
        with self.assertRaises(CrmUnavailable):
            fetch_leads(token="")

    def test_fixed_host_token_and_validated_detail_link(self):
        seen = []

        def answer(request):
            seen.append(request)
            return httpx.Response(200, json={"brand": "pp", "leads": [{
                "id": "59e2f6cf-c35e-4a23-991f-012d29fbecba", "name": "Test Lead",
                "source": "form", "lifecycle": "lead", "stage": "New",
                "nextTaskAt": None, "lastActivityAt": None,
            }], "nextCursor": None})

        with httpx.Client(transport=httpx.MockTransport(answer)) as client:
            page = fetch_leads(client=client, token="x" * 32)
        self.assertEqual(len(page.leads), 1)
        self.assertEqual(page.leads[0].detail_url,
                         "https://hq.propertypredator.com/portal/crm/contacts/59e2f6cf-c35e-4a23-991f-012d29fbecba")
        self.assertEqual(str(seen[0].url), "https://hq.propertypredator.com/api/group-hq/crm/leads?brand=pp")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer " + "x" * 32)

    def test_bad_identity_or_brand_fails_closed(self):
        for payload in ({"brand": "mhp", "leads": []}, {"brand": "pp", "leads": [{
            "id": "javascript:alert(1)", "name": "X", "lifecycle": "lead"}], "nextCursor": None}):
            with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
                with self.assertRaises(CrmUnavailable):
                    fetch_leads(client=client, token="x" * 32)

    def test_redirect_and_bad_cursor_are_unavailable(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.test"}))) as client:
            with self.assertRaises(CrmUnavailable):
                fetch_leads(client=client, token="x" * 32)
        with self.assertRaises(CrmUnavailable):
            fetch_leads("bad!", token="x" * 32)


if __name__ == "__main__":
    unittest.main()
