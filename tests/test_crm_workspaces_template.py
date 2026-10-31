"""Standalone navigation proof; no application/database fixtures are needed."""
from pathlib import Path
import re
import unittest

from jinja2 import Environment, StrictUndefined


class CrmWorkspaceTemplateTest(unittest.TestCase):
    def test_native_flow_shortcuts_preserve_workspace_selection_and_existing_tools(self):
        source = (Path(__file__).resolve().parents[1] / "app/templates/_crm_workspaces.html").read_text(encoding="utf-8")
        rendered = Environment(undefined=StrictUndefined, autoescape=True).from_string(source).render()
        cards = dict(re.findall(r'<article[^>]+data-workspace="([^"]+)"[^>]*>(.*?)</article>', rendered, re.S))
        self.assertEqual(set(cards), {"mhp", "pp"})
        mhp = cards["mhp"]
        self.assertEqual(re.findall(r'href="([^"]+)"', mhp), [
            "https://app.martinhowardproperty.co.uk/hq/marketing",
            "https://app.martinhowardproperty.co.uk/hq/growth",
            "https://app.martinhowardproperty.co.uk/hq/marketing",
        ])
        self.assertIn('class="btn btn-primary" href="https://app.martinhowardproperty.co.uk/hq/marketing"', mhp)
        self.assertIn("Manage brand enquiries in MHP Marketing", mhp)
        self.assertIn("Your Desk is your personal space for investor leads and email conversations", mhp)
        self.assertIn("Webinar email status", mhp)
        self.assertIn("registrations and automatic email counts", mhp)
        self.assertIn("Choose brand marketing, then open the sequence builder", mhp)
        self.assertIn("Build follow-up flows", mhp)
        self.assertNotIn("/hq/marketing/sequences", mhp)
        pp = cards["pp"]
        self.assertIn('class="btn btn-primary" href="https://hq.propertypredator.com/portal/journeys/board"', pp)
        self.assertEqual(re.findall(r'href="([^"]+)"', pp), [
            "https://hq.propertypredator.com/portal/journeys/board",
            "https://hq.propertypredator.com/portal/crm/contacts",
            "https://hq.propertypredator.com/portal/crm/tasks",
            "https://hq.propertypredator.com/portal/inbox",
            "https://hq.propertypredator.com/portal/campaigns/sequences",
            "https://hq.propertypredator.com/portal/follow-ups",
        ])
        self.assertIn("Build follow-up flows", pp)
        self.assertIn("Unconnected channels need setup and a joint test before sending", pp)
        self.assertIn("prepared sequences; it does not activate live email campaigns", pp)


if __name__ == "__main__":
    unittest.main()
