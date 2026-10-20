"""Tests for the skill selector: --out writes the skill text to a file and prints
only the short Task Card block; budgets and rule matching stay deterministic.

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


def make_repo(tmp):
    """A fake project with one personal-style skill and a project rule for it."""
    root = Path(tmp)
    skill = root / ".claude" / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: demo skill\n---\n# Demo\nDo demo things.\n",
                                    encoding="utf-8")
    fl = root / ".feature-ladder"
    fl.mkdir()
    (fl / "skill-rules.json").write_text(json.dumps({
        "rules": [{"id": "demo", "skill": "demo", "priority": 90, "when": {"keywords": ["demo"]}}],
        "disable": ["tdd", "tdd-app"],
    }), encoding="utf-8")
    return root


def run(root, *args):
    env = dict(os.environ, HOME=str(root))  # no personal rules or skills from the real home
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(root), env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r.returncode, r.stdout, r.stderr


TASK = json.dumps({"role": "implementer", "kind": "code", "paths": ["x.py"], "text": "add the demo widget"})


class OutFile(unittest.TestCase):
    def test_out_writes_file_and_prints_short_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, err = run(root, "--inject", "--out", ".feature-ladder/cards/T1-skills.md", TASK)
            self.assertEqual(code, 0, err)
            f = root / ".feature-ladder" / "cards" / "T1-skills.md"
            self.assertTrue(f.is_file())
            body = f.read_text(encoding="utf-8")
            self.assertIn("### Skill: demo", body)
            self.assertIn("Do demo things.", body)
            # the printed block names the file (absolute) and the skill, but carries no skill text
            self.assertIn("## Skills for this task", out)
            self.assertIn(str(f.resolve()), out)
            self.assertIn("demo", out)
            self.assertNotIn("Do demo things.", out)
            self.assertLess(len(out), 600)
            # the cards folder is git-ignored inside .feature-ladder
            self.assertIn("cards/", (root / ".feature-ladder" / ".gitignore").read_text(encoding="utf-8"))

    def test_out_with_no_selection_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            task = json.dumps({"role": "mechanic", "kind": "check", "paths": [], "text": "run the checks"})
            code, out, _ = run(root, "--inject", "--out", ".feature-ladder/cards/T2-skills.md", task)
            self.assertEqual(code, 0)
            self.assertIn("None selected", out)
            self.assertFalse((root / ".feature-ladder" / "cards" / "T2-skills.md").exists())

    def test_plain_inject_still_prints_full_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "--inject", TASK)
            self.assertEqual(code, 0)
            self.assertIn("Do demo things.", out)

    def test_json_output_names_rule(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, TASK)
            self.assertEqual(code, 0)
            data = json.loads(out)
            names = [s["skill"] for s in data["skills"]]
            self.assertIn("demo", names)
            self.assertTrue(any("rule demo" in w for s in data["skills"] for w in s["why"]))


if __name__ == "__main__":
    unittest.main()
