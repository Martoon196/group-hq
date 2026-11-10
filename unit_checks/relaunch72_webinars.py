import unittest

import httpx

from app.services.relaunch72_webinars import WebinarsUnavailable, fetch_webinars

WEBINAR_ID = "59e2f6cf-c35e-4a23-991f-012d29fbecba"


def listing(**over):
    base = f"/portal/webinars/{WEBINAR_ID}"
    item = {"id": WEBINAR_ID, "title": "Unregistered land", "kind": "live", "status": "published",
            "startsAt": "2026-11-25T19:00:00.000Z", "timezone": "Europe/London", "durationMinutes": 60,
            "sessionState": "idle", "registered": 12, "crmLinked": 12, "updatedAt": "2026-11-10T09:00:00.000Z",
            "publicUrl": "https://app.martinhowardproperty.co.uk/w/unregistered-land",
            "paths": {"manage": base, "host": f"{base}/host", "studio": f"{base}/studio", "people": f"{base}/people"}}
    item.update(over)
    return {"brand": "mhp", "availability": "available", "workspace": {"id": WEBINAR_ID, "name": "MHP marketing", "timezone": "Europe/London"},
            "webinars": [item]}


class WebinarReadTests(unittest.TestCase):
    def test_unconfigured_never_calls_product(self):
        with self.assertRaises(WebinarsUnavailable):
            fetch_webinars("mhp", token="")
        with self.assertRaises(WebinarsUnavailable):
            fetch_webinars("kp", token="x" * 32)

    def test_fixed_host_token_and_validated_shape(self):
        seen = []

        def answer(request):
            seen.append(request)
            return httpx.Response(200, json=listing())

        with httpx.Client(transport=httpx.MockTransport(answer)) as client:
            result = fetch_webinars("mhp", client=client, token="x" * 32)
        self.assertEqual(str(seen[0].url), "https://hq.propertypredator.com/api/group-hq/webinars?brand=mhp")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer " + "x" * 32)
        self.assertEqual(result.availability, "available")
        self.assertEqual(result.workspace_name, "MHP marketing")
        webinar = result.webinars[0]
        self.assertEqual((webinar.kind_label, webinar.status_label, webinar.registered), ("Live", "Published", 12))
        self.assertEqual(webinar.host_path, f"/portal/webinars/{WEBINAR_ID}/host")

    def test_not_connected_is_a_state_not_an_error(self):
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"brand": "pp", "availability": "not_connected"}))) as client:
            self.assertEqual(fetch_webinars("pp", client=client, token="x" * 32).availability, "not_connected")

    def test_bad_payloads_fail_closed(self):
        for payload in (
            listing(id="javascript:alert(1)"),
            listing(publicUrl="https://evil.example/w/x"),
            listing(paths={"manage": "https://evil.example", "host": "/x", "studio": "/x", "people": "/x"}),
            listing(kind="zoom"),
            listing(registered="12"),
            {"brand": "pp", "availability": "available", "workspace": {"name": "x"}, "webinars": []},
        ):
            with httpx.Client(transport=httpx.MockTransport(lambda request, p=payload: httpx.Response(200, json=p))) as client:
                with self.assertRaises(WebinarsUnavailable):
                    fetch_webinars("mhp", client=client, token="x" * 32)
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "https://evil.test"}))) as client:
            with self.assertRaises(WebinarsUnavailable):
                fetch_webinars("mhp", client=client, token="x" * 32)


if __name__ == "__main__":
    unittest.main()
