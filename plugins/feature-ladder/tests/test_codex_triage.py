"""Tests for the deterministic Codex finding triage and the batched verification card.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "cross-model-review" / "scripts"))

from codex_review import triage, verify_card, REVIEW_SCHEMA  # noqa: E402


def f(sev, file, line, title, **kw):
    d = {"severity": sev, "file": file, "line": line, "title": title, "detail": kw.get("detail", "d"),
         "suggested_fix": "fix", "confidence": kw.get("confidence", "medium"), "repro": kw.get("repro", "")}
    return d


class Triage(unittest.TestCase):
    def test_low_is_set_aside_and_order_is_by_severity(self):
        t = triage([f("low", "a.py", 1, "style"), f("medium", "b.py", 5, "edge"), f("critical", "c.py", 9, "crash")])
        self.assertEqual([x["severity"] for x in t["actionable"]], ["critical", "medium"])
        self.assertEqual([x["title"] for x in t["low"]], ["style"])
        self.assertEqual(t["duplicates"], 0)

    def test_same_file_and_title_is_one_finding(self):
        t = triage([f("high", "a.py", 10, "Unchecked None"), f("medium", "a.py", 42, "unchecked  none")])
        self.assertEqual(len(t["actionable"]), 1)
        self.assertEqual(t["actionable"][0]["severity"], "high")  # the higher severity survives
        self.assertEqual(t["duplicates"], 1)

    def test_same_file_and_line_is_one_finding(self):
        t = triage([f("medium", "a.py", 7, "off by one"), f("medium", "a.py", 7, "loop bound wrong")])
        self.assertEqual(len(t["actionable"]), 1)
        self.assertEqual(t["duplicates"], 1)

    def test_distinct_findings_survive(self):
        t = triage([f("medium", "a.py", 7, "x"), f("medium", "a.py", 8, "y"), f("medium", "b.py", 7, "x")])
        self.assertEqual(len(t["actionable"]), 3)

    def test_schema_requires_repro(self):
        self.assertIn("repro", REVIEW_SCHEMA["properties"]["findings"]["items"]["required"])


class Card(unittest.TestCase):
    def test_card_lists_every_finding_with_repro(self):
        act = [f("high", "a.py", 3, "crash", repro="python -m pytest tests/test_a.py -q"),
               f("medium", "b.py", 4, "edge")]
        card = verify_card("demo", act, ".feature-ladder/reviews/demo-codex-review.json")
        self.assertIn("TASK codex-verify-demo: confirm or refute 2 Codex finding(s)", card)
        self.assertIn("Rung: mechanic (haiku/low)", card)
        self.assertIn("1. [high/medium] a.py:3 crash", card)
        self.assertIn("repro: python -m pytest tests/test_a.py -q", card)
        self.assertIn("2. [medium/medium] b.py:4 edge", card)
        self.assertIn("repro: (none given)", card)
        self.assertIn("Do not: fix anything", card)


if __name__ == "__main__":
    unittest.main()
