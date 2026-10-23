"""Golden classification cases for codex_review.triage(), beyond the unit tests
in test_codex_triage.py. The class mapping (informational / answered /
no-response onto the classes the code really has) is in the "_doc" header of
fixtures/codex_triage/cases.json.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "cross-model-review" / "scripts"))

from codex_review import triage  # noqa: E402

CORPUS = json.loads((PLUGIN / "tests" / "fixtures" / "codex_triage" / "cases.json").read_text(encoding="utf-8"))


class TriageGolden(unittest.TestCase):
    def test_header_documents_the_class_mapping(self):
        doc = CORPUS["_doc"]
        for word in ("actionable", "low", "duplicates", "informational", "answered", "no-response"):
            self.assertIn(word, doc)

    def test_every_case_classifies_as_recorded(self):
        for case in CORPUS["cases"]:
            with self.subTest(case=case["name"]):
                t = triage(case["findings"])
                self.assertEqual([x.get("title") for x in t["actionable"]], case["expect"]["actionable"])
                self.assertEqual([x.get("title") for x in t["low"]], case["expect"]["low"])
                self.assertEqual(t["duplicates"], case["expect"]["duplicates"])

    def test_every_finding_lands_in_exactly_one_class(self):
        for case in CORPUS["cases"]:
            with self.subTest(case=case["name"]):
                t = triage(case["findings"])
                self.assertEqual(len(t["actionable"]) + len(t["low"]) + t["duplicates"], len(case["findings"]))

    def test_triage_does_not_mutate_its_input(self):
        for case in CORPUS["cases"]:
            before = json.dumps(case["findings"], sort_keys=True)
            triage(case["findings"])
            self.assertEqual(json.dumps(case["findings"], sort_keys=True), before)


if __name__ == "__main__":
    unittest.main()
