"""Route smoke without a database or a product call; the production suite covers login separately."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://unused:unused@127.0.0.1/unused")
os.environ.setdefault("SECRET_KEY", "unit-test-only")

from starlette.requests import Request  # noqa: E402
from app.routers.crm import crm  # noqa: E402
from app.services.relaunch72_crm import CrmUnavailable, Lead, LeadPage  # noqa: E402


class CrmRouteTests(unittest.TestCase):
    @staticmethod
    def request():
        return Request({"type": "http", "method": "GET", "path": "/crm", "root_path": "",
                        "scheme": "http", "server": ("testserver", 80), "headers": [], "query_string": b""})

    def test_read_only_lead_page_and_unavailable_state(self):
        founder = SimpleNamespace(email="founder@example.test")
        lead = Lead("59e2f6cf-c35e-4a23-991f-012d29fbecba", "Test Lead", "form", "lead", "New", None,
                    None, "https://hq.propertypredator.com/portal/crm/contacts/59e2f6cf-c35e-4a23-991f-012d29fbecba")
        with patch("app.routers.crm.fetch_leads", return_value=LeadPage((lead,), None)):
            response = crm(self.request(), founder)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Test Lead", response.body.decode())
        self.assertEqual(response.headers["cache-control"], "no-store")
        with patch("app.routers.crm.fetch_leads", side_effect=CrmUnavailable()):
            response = crm(self.request(), founder)
        self.assertEqual(response.status_code, 200)
        self.assertIn("connection is unavailable", response.body.decode())


if __name__ == "__main__":
    unittest.main()
