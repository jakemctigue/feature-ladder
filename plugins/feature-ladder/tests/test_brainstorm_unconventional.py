"""The brainstorm's question workflow includes the unconventional-thinking pass.

Pins: an option the scout marks `"unconventional": true` is labelled
`Unconventional: ...`, asked last and never recommended; a detail carries one
at most and it keeps its place among the four options a question can show;
picking it settles the detail like any other option; `decide` counts the
questions that carry one; and the guide, the slash command and the Claude app
skill all name the pass.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "complexity-router" / "scripts"))
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import brainstorm  # noqa: E402

ODD = {"label": "Event log as the store", "description": "From accounting ledgers: append only, replay to rebuild. "
       "Buys audit and undo; risks slower reads.", "unconventional": True}


def sheet(options, **over):
    d = dict({"value": "Postgres", "confidence": "medium", "evidence": ["proposal"], "options": options}, **over)
    return brainstorm.normalise({"request": "build a ledger app", "dimensions": {"data_store": d}})


class Placement(unittest.TestCase):
    def test_it_is_labelled_asked_last_and_never_recommended(self):
        s = sheet([ODD, {"label": "Postgres", "description": "relational"}, {"label": "SQLite", "description": "a file"}])
        opts = brainstorm._options(s["dimensions"]["data_store"])
        self.assertEqual(opts[0]["label"], "Postgres (Recommended)")
        self.assertEqual(opts[-1]["label"], "Unconventional: Event log as the store")
        self.assertTrue(opts[-1]["unconventional"])
        self.assertNotIn("unconventional", opts[0])

    def test_it_keeps_its_place_when_there_are_four_conventional_options(self):
        plain = [{"label": n, "description": ""} for n in ("Postgres", "SQLite", "MySQL", "DynamoDB")]
        opts = brainstorm._options(sheet(plain + [ODD])["dimensions"]["data_store"])
        self.assertEqual([o["label"] for o in opts],
                         ["Postgres (Recommended)", "SQLite", "MySQL", "Unconventional: Event log as the store"])

    def test_one_per_detail_and_never_alone(self):
        second = dict(ODD, label="Unconventional: CRDT store")
        opts = brainstorm.unconventional_last([{"label": "Postgres"}, ODD, second])
        self.assertEqual(sum(1 for o in opts if brainstorm.is_unconventional(o)), 1)
        self.assertEqual(brainstorm.unconventional_last([ODD]), [])  # nothing conventional beside it

    def test_a_label_that_already_says_so_is_not_prefixed_twice(self):
        opts = brainstorm.unconventional_last([{"label": "Postgres"}, {"label": "Unconventional: CRDT store"}])
        self.assertEqual(opts[-1]["label"], "Unconventional: CRDT store")
        self.assertTrue(opts[-1]["unconventional"])


class Asked(unittest.TestCase):
    def test_decide_counts_the_questions_that_carry_one(self):
        s = sheet([{"label": "Postgres", "description": "relational"}, ODD])
        s["dimensions"]["hosting"] = brainstorm.normalise({"dimensions": {"hosting": {
            "value": "Fly.io", "confidence": "low", "options": [{"label": "Fly.io"}, {"label": "Render"}]}}})["dimensions"]["hosting"]
        out = brainstorm.decide(s, mode="default")
        self.assertEqual(out["counts"]["unconventional"], 1)
        q = next(q for q in out["questions"] if q["detail"] == "data_store")
        self.assertEqual(q["options"][-1]["label"], "Unconventional: Event log as the store")

    def test_picking_it_settles_the_detail_with_its_tradeoff(self):
        d = sheet([{"label": "Postgres", "description": "relational"}, ODD])["dimensions"]["data_store"]
        out = brainstorm.resolve(d, "Unconventional: Event log as the store")
        self.assertEqual((out["status"], out["source"]), ("decided", "user"))
        self.assertEqual(out["value"], "Unconventional: Event log as the store")
        self.assertIn("accounting ledgers", out["detail"])
        self.assertTrue(out["changed"])

    def test_the_recommendation_still_confirms_the_proposal(self):
        d = sheet([{"label": "Postgres", "description": "relational"}, ODD])["dimensions"]["data_store"]
        self.assertEqual(brainstorm.resolve(d, "Postgres (Recommended)")["source"], "user (confirmed)")


class Wiring(unittest.TestCase):
    def test_the_guide_the_command_and_the_app_skill_name_the_pass(self):
        self.assertIn("4b. Unconventional pass", brainstorm.GUIDE)
        self.assertIn("unconventional-thinking", brainstorm.GUIDE)
        for f in (PLUGIN / "commands" / "feature.md", REPO / "claude-app" / "model-ladder-router" / "SKILL.md"):
            text = f.read_text(encoding="utf-8")
            self.assertIn("unconventional-thinking", text, f.name)
            self.assertIn("Unconventional:", text, f.name)


if __name__ == "__main__":
    unittest.main()
