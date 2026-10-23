"""Golden cases for the deterministic router (route.py) and the budget the intake
derives from it (intake.budget_for). The expected values in
fixtures/routing/golden_cases.json record the router's behaviour at the time
the corpus was written; they pin the scoring table, the band edges, the
provider split, the hard flags and the size/worker budget.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "complexity-router" / "scripts"))
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import intake  # noqa: E402
import route  # noqa: E402

CORPUS = json.loads((PLUGIN / "tests" / "fixtures" / "routing" / "golden_cases.json").read_text(encoding="utf-8"))
CASES = CORPUS["cases"]


class RoutingGolden(unittest.TestCase):
    def test_corpus_spans_every_band_and_the_hard_flags(self):
        agents = {(c["expect"]["agent"], c["expect"]["model"], c["expect"]["effort"]) for c in CASES}
        for want in [("mechanic", "haiku", "low"), ("implementer", "sonnet", "medium"),
                     ("implementer", "sonnet", "high"), ("senior-implementer", "opus", "medium"),
                     ("senior-implementer", "opus", "high"), ("evidence-collector", "haiku", "medium"),
                     ("domain-expert", "opus", "medium"), ("domain-expert-high", "opus", "high")]:
            self.assertIn(want, agents)
        self.assertEqual({c["expect"]["size"] for c in CASES}, {"S", "M", "L"})
        self.assertTrue(any(c["expect"]["hard_flags"] for c in CASES))
        self.assertGreaterEqual(len(CASES), 15)
        self.assertEqual(len({c["name"] for c in CASES}), len(CASES))

    def test_every_case_matches_the_router(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                got = route.route(case["input"])
                for key, want in case["expect"].items():
                    self.assertEqual(got[key], want, f"{case['name']}: {key}")

    def test_every_case_matches_the_intake_budget(self):
        for case in CASES:
            with self.subTest(case=case["name"]):
                budget = intake.budget_for(route.route(case["input"]), case["input"])
                for key, want in case["budget"].items():
                    self.assertEqual(budget[key], want, f"{case['name']}: budget.{key}")

    def test_routing_is_deterministic(self):
        for case in CASES:
            self.assertEqual(route.route(case["input"]), route.route(case["input"]))


if __name__ == "__main__":
    unittest.main()
