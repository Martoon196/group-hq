"""Standalone navigation proof; no application/database fixtures are needed."""
from hashlib import sha256
from pathlib import Path
import re
import unittest

from jinja2 import Environment, StrictUndefined


class CrmWorkspaceTemplateTest(unittest.TestCase):
    def test_mhp_uses_native_selection_and_status_while_pp_is_unchanged(self):
        source = (Path(__file__).resolve().parents[1] / "app/templates/_crm_workspaces.html").read_text(encoding="utf-8")
        rendered = Environment(undefined=StrictUndefined, autoescape=True).from_string(source).render()
        cards = dict(re.findall(r'<article[^>]+data-workspace="([^"]+)"[^>]*>(.*?)</article>', rendered, re.S))
        self.assertEqual(set(cards), {"mhp", "pp"})
        mhp = cards["mhp"]
        self.assertEqual(re.findall(r'href="([^"]+)"', mhp), [
            "https://app.martinhowardproperty.co.uk/hq/marketing",
            "https://app.martinhowardproperty.co.uk/hq/growth",
        ])
        self.assertIn('class="btn btn-primary" href="https://app.martinhowardproperty.co.uk/hq/marketing"', mhp)
        self.assertIn("Manage brand enquiries in MHP Marketing", mhp)
        self.assertIn("Your Desk is your personal space for investor leads and email conversations", mhp)
        self.assertIn("Webinar email status", mhp)
        self.assertIn("registrations and automatic email counts", mhp)
        self.assertNotIn("campaign builder", mhp.lower())
        pp_source = re.search(r'<article[^>]+data-workspace="pp".*?</article>', source, re.S)
        self.assertIsNotNone(pp_source)
        # Exact base df5d6aa4 PP card, with the repository's LF text convention.
        self.assertEqual(sha256(pp_source.group(0).encode("utf-8")).hexdigest(),
                         "72b193f08f3e1b4374978bd3bf45eaf997a9ad399a1b3c573eaee652131a18ed")


if __name__ == "__main__":
    unittest.main()
