"""Route smoke without a database or a product call."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("SECRET_KEY", "unit-test-only")

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402
from app import config  # noqa: E402
from app.routers.webinars import open_page, webinars  # noqa: E402
from app.services.relaunch72_sso import verify_for_tests  # noqa: E402
from app.services.relaunch72_webinars import WebinarListing, WebinarsUnavailable  # noqa: E402

SECRET = "group-hq-shared-secret-synthetic-0123456789abcdef"


def request(path: str, query: str = ""):
    return Request({"type": "http", "method": "GET", "path": path, "root_path": "", "scheme": "http",
                    "server": ("testserver", 80), "headers": [], "query_string": query.encode()})


class WebinarsRouteTests(unittest.TestCase):
    founder = SimpleNamespace(email="founder@example.test")

    def test_listing_page_and_unavailable_state(self):
        empty = WebinarListing("mhp", "available", "MHP marketing", ())
        with patch("app.routers.webinars.fetch_webinars", return_value=empty), patch.object(config, "R72_SSO_SECRET", SECRET):
            response = webinars(request("/webinars", "brand=mhp"), self.founder)
        body = response.body.decode()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("No webinars yet", body)
        self.assertIn("/webinars/open?brand=mhp&amp;path=/portal/webinars/new", body)
        self.assertNotIn(SECRET, body)
        with patch("app.routers.webinars.fetch_webinars", side_effect=WebinarsUnavailable()):
            response = webinars(request("/webinars"), self.founder)
        self.assertIn("connection is unavailable", response.body.decode())
        with self.assertRaises(HTTPException):
            webinars(request("/webinars", "brand=kp"), self.founder)

    def test_open_redirects_to_the_brand_host_with_a_signed_single_use_token(self):
        with patch.object(config, "R72_SSO_SECRET", SECRET):
            response = open_page(request("/webinars/open", "brand=mhp&path=/portal/webinars/abc/host"), self.founder)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["cache-control"], "no-store")
        location = response.headers["location"]
        parts = urlsplit(location)
        self.assertEqual((parts.scheme, parts.netloc, parts.path), ("https", "app.martinhowardproperty.co.uk", "/portal/auth/group-hq"))
        token = parse_qs(parts.query)["t"][0]
        claims = verify_for_tests(token, SECRET)
        self.assertEqual((claims["email"], claims["brand"], claims["host"], claims["path"]),
                         ("founder@example.test", "mhp", "app.martinhowardproperty.co.uk", "/portal/webinars/abc/host"))
        self.assertLessEqual(claims["exp"] - claims["iat"], 120)

    def test_open_refuses_foreign_paths_and_falls_back_when_unconfigured(self):
        with patch.object(config, "R72_SSO_SECRET", SECRET):
            for query in ("brand=pp&path=https://evil.example/portal", "brand=pp&path=//evil.example", "brand=pp&path=/admin",
                          "brand=pp&path=/portal&next=x"):
                with self.assertRaises(HTTPException):
                    open_page(request("/webinars/open", query), self.founder)
        with patch.object(config, "R72_SSO_SECRET", ""):
            response = open_page(request("/webinars/open", "brand=pp&path=/portal/webinars"), self.founder)
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/webinars?brand=pp&err="))


if __name__ == "__main__":
    unittest.main()
