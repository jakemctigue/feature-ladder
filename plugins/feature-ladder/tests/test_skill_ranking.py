"""Ranking fixtures for the skill selector: for a task (role, kind, paths, text,
flags) and a synthetic set of installed skills, select_skills.select() must pick
the same skills in the same order, drop the same ones for the same reason and
keep excluded skills out. It reads the SHIPPED skill-rules.json only (never the
user's personal or project rules) and never touches the real skill inventory,
so results are the same on every machine.

test_select_skills.py covers the CLI (--out, --inject, role budgets) with a
demo rule; this file covers the shipped rule set.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "skill-selector" / "scripts"))

import select_skills as ss  # noqa: E402

RULES = json.loads((PLUGIN / "skills" / "skill-selector" / "skill-rules.json").read_text(encoding="utf-8"))
CORPUS = json.loads((PLUGIN / "tests" / "fixtures" / "skill_ranking" / "cases.json").read_text(encoding="utf-8"))
SETTINGS = {"max_skills": RULES["max_skills"], "max_chars": RULES["max_chars"],
            "max_chars_by_role": {k.lower(): v for k, v in RULES["max_chars_by_role"].items()}}
ALL_SKILLS = sorted({r["skill"] for r in RULES["rules"]})


def installed(profile):
    if profile == "claude-code":
        return ALL_SKILLS
    if profile == "claude-app":
        return [n for n in ALL_SKILLS if not n.startswith("superpowers:")]
    if profile == "none":
        return []
    raise ValueError(profile)


def build_inventory(tmp, case):
    inv = {}
    for name in installed(case["installed"]):
        path = Path(tmp) / (name.replace(":", "__") + ".md")
        path.write_text("---\nname: x\n---\n" + "b" * case.get("body_chars", {}).get(name, 100), encoding="utf-8")
        inv[name] = path
    return inv


class SkillRanking(unittest.TestCase):
    def test_every_case_selects_the_expected_skills(self):
        for case in CORPUS["cases"]:
            with self.subTest(case=case["name"]), tempfile.TemporaryDirectory() as tmp:
                result = ss.select(case["task"], SETTINGS, RULES["rules"], build_inventory(tmp, case))
                self.assertEqual([s["skill"] for s in result["skills"]], case["expect_order"])
                self.assertEqual({d["skill"]: d["reason"] for d in result["dropped"]}, case["expect_dropped"])
                self.assertEqual(sorted(result["unresolved"]), case["expect_unresolved"])
                for name in case.get("expect_absent", []):
                    self.assertNotIn(name, [s["skill"] for s in result["skills"]])

    def test_never_more_than_max_skills(self):
        for case in CORPUS["cases"]:
            self.assertLessEqual(len(case["expect_order"]), RULES["max_skills"], case["name"])

    def test_selection_is_deterministic(self):
        case = next(c for c in CORPUS["cases"] if c["name"] == "max-skills-3-drops-the-rest")
        with tempfile.TemporaryDirectory() as tmp:
            inv = build_inventory(tmp, case)
            a = ss.select(case["task"], SETTINGS, RULES["rules"], inv)
            b = ss.select(case["task"], SETTINGS, RULES["rules"], inv)
            self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
