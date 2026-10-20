"""The Claude app skill and the README must point at the release in plugin.json."""
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins" / "feature-ladder"
SKILL = REPO / "claude-app" / "model-ladder-router" / "SKILL.md"
VERSION = "v" + json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]


class ReleasePins(unittest.TestCase):
    def test_app_skill_pins_the_plugin_version(self):
        pins = set(re.findall(r"\bv\d+\.\d+\.\d+\b", SKILL.read_text(encoding="utf-8")))
        self.assertEqual(pins, {VERSION})

    def test_readme_names_the_plugin_version(self):
        m = re.search(r"currently `(v\d+\.\d+\.\d+)`", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), VERSION)

    def test_every_file_the_app_downloads_exists(self):
        text = SKILL.read_text(encoding="utf-8")
        files = re.search(r"for f in (.+?); do", text).group(1).split()
        self.assertTrue(files)
        for f in files:
            self.assertTrue((PLUGIN / "skills" / f).is_file(), f)


if __name__ == "__main__":
    unittest.main()
