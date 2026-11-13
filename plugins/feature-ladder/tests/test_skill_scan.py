"""Tests for the skill rescan: `select_skills.py --scan` turns whatever is
installed into a catalog with no model, says which rules point at skills that
are gone and which installed skills no rule reaches, and reads multi-line
descriptions and plugin manifest names the way Claude Code shows them.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "skills" / "skill-selector" / "scripts" / "select_skills.py"
sys.path.insert(0, str(SCRIPT.parent))

import select_skills as ss  # noqa: E402


def skill(folder, name, description="a skill", body="# Body\nDo it.\n"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}", encoding="utf-8")


def make_home(tmp):
    """A fake home with a personal skill, an account-synced skill and one
    installed plugin whose marketplace key differs from its manifest name."""
    home = Path(tmp) / "home"
    skills = home / ".claude" / "skills"
    skill(skills / "valibot", "valibot", "Schema validation with Valibot")
    skill(skills / "synced" / "bucket" / "docx", "docx", "Word documents")
    (skills / "folded").mkdir(parents=True)
    (skills / "folded" / "SKILL.md").write_text(
        "---\nname: folded\ndescription: >-\n  Deploys things\n  to Railway.\nlicense: MIT\n---\n# Folded\n", encoding="utf-8")
    plug = home / "plugins-store" / "data-agent-kit"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "dak"}), encoding="utf-8")
    skill(plug / "skills" / "bigquery-sql", "bigquery-sql", "BigQuery SQL")
    (home / ".claude" / "plugins").mkdir(parents=True)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"data-agent-kit@claude-plugins-official": [{"installPath": str(plug)}]}}), encoding="utf-8")
    work = Path(tmp) / "work"
    work.mkdir()
    return home, work


def run(home, work, *args):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(work), env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r.returncode, r.stdout, r.stderr


class Frontmatter(unittest.TestCase):
    def test_block_scalar_description_is_read_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, _ = make_home(tmp)
            meta, body = ss._frontmatter(home / ".claude" / "skills" / "folded" / "SKILL.md")
            self.assertEqual(meta["description"], "Deploys things to Railway.")
            self.assertEqual(meta["license"], "MIT")
            self.assertTrue(body.startswith("# Folded"))

    def test_single_line_values_are_unchanged(self):
        self.assertEqual(ss._parse_meta(['name: "demo"', "description: 'Use when: x'"]),
                         {"name": "demo", "description": "Use when: x"})


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home, self.work = make_home(self.tmp.name)
        self.out = Path(self.tmp.name) / "catalog.json"

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, *extra):
        code, out, err = run(self.home, self.work, "--scan", "--out", str(self.out), *extra)
        return code, (json.loads(out) if out.strip().startswith("{") else out), err

    def test_scan_writes_one_record_per_file_with_its_aliases(self):
        code, report, err = self.scan()
        self.assertEqual(code, 0, err)
        cat = json.loads(self.out.read_text(encoding="utf-8"))
        by_name = {k["name"]: k for k in cat["skills"]}
        self.assertEqual(by_name["docx"]["aliases"], ["anthropic-skills:docx"])
        self.assertEqual(by_name["docx"]["source"], "account")
        self.assertEqual(by_name["valibot"]["source"], "personal")
        # the plugin's skills answer to the marketplace key and to the manifest name
        self.assertEqual(by_name["data-agent-kit:bigquery-sql"]["aliases"], ["dak:bigquery-sql"])
        self.assertEqual(by_name["folded"]["description"], "Deploys things to Railway.")
        self.assertEqual(report["skills"], cat["count"])
        self.assertEqual(report["names"], cat["count"] + 2)

    def test_report_names_rules_whose_skill_is_gone_and_skills_no_rule_reaches(self):
        fl = self.work / ".feature-ladder"
        fl.mkdir()
        (fl / "skill-rules.json").write_text(json.dumps({"rules": [
            {"id": "ghost", "skill": "not-installed-anywhere", "when": {"keywords": ["ghost"]}}]}), encoding="utf-8")
        _, report, _ = self.scan()
        self.assertIn({"rule": "ghost", "skill": "not-installed-anywhere"}, report["rules_missing_skill"])
        self.assertIn("folded", report["unreached"])           # no rule names it
        self.assertNotIn("valibot", report["unreached"])       # the shipped valibot rule reaches it
        self.assertNotIn("docx", report["unreached"])
        # a rule written with either plugin prefix reaches the skill
        self.assertNotIn("data-agent-kit:bigquery-sql", report["unreached"])

    def test_same_skills_give_the_same_catalog_and_edits_are_reported(self):
        self.scan()
        first = self.out.read_bytes()
        _, again, _ = self.scan()
        self.assertEqual(self.out.read_bytes(), first)
        self.assertEqual(again["since_last_scan"], {"added": [], "removed": [], "changed": []})
        skill(self.home / ".claude" / "skills" / "valibot", "valibot", body="# Body\nChanged.\n")
        skill(self.home / ".claude" / "skills" / "fresh", "fresh")
        code, drift, _ = self.scan("--check")
        self.assertEqual(code, 1)                              # --check: drift is an error ...
        self.assertEqual(self.out.read_bytes(), first)         # ... and nothing is written
        self.assertEqual(drift["since_last_scan"]["added"], ["fresh"])
        self.assertEqual(drift["since_last_scan"]["changed"], ["valibot"])
        _, after, _ = self.scan()
        self.assertNotEqual(after["digest"], again["digest"])

    def test_manifest_prefix_selects_the_plugin_skill(self):
        fl = self.work / ".feature-ladder"
        fl.mkdir()
        (fl / "skill-rules.json").write_text(json.dumps({"rules": [
            {"id": "bq-dak", "skill": "dak:bigquery-sql", "priority": 99, "when": {"keywords": ["warehouse"]}}]}),
            encoding="utf-8")
        task = json.dumps({"role": "senior-implementer", "kind": "docs", "text": "query the bigquery warehouse"})
        code, out, err = run(self.home, self.work, task)
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        # both names fire (the shipped rule and this one) but the file is injected once
        self.assertEqual([s["skill"] for s in data["skills"]], ["dak:bigquery-sql"])
        self.assertTrue(any(d["skill"] == "data-agent-kit:bigquery-sql" and "same file" in d["reason"]
                            for d in data["dropped"]))


class ShippedRules(unittest.TestCase):
    def test_rule_ids_are_unique_and_every_rule_is_valid(self):
        rules = json.loads((PLUGIN / "skills" / "skill-selector" / "skill-rules.json").read_text(encoding="utf-8"))["rules"]
        ids = [r["id"] for r in rules]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(ss._valid(r) for r in rules))


if __name__ == "__main__":
    unittest.main()
