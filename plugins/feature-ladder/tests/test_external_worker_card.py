"""Tests for the queue card external_worker.py hands the OpenRouter worker:
Memory from the saved recall (the file confidence.py scores), Bounds and the
output contract built in, for the Jev preset and the default GLM queue.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import confidence as conf  # noqa: E402
import external_worker as ew  # noqa: E402

PITFALL = "[pitfall] server/mapRoutes.ts writes must await the Firestore batch commit or the emulator test times out"
CONVENTION = "[convention] npx vitest run server/mapRoutes is the gate for map route changes; it passes green"
OTHER = "[decision] the docs site builds with mkdocs and deploys from the docs branch"
RECALL = {"results": [{"text": OTHER}, {"text": PITFALL}, {"text": CONVENTION}]}

CARD = """TASK T1: save the map name to Firestore
Rung: implementer (sonnet/medium)   Router score: 4   Flags: none
Scope: server/mapRoutes.ts, server/mapRoutes.test.ts
Design: N/A - the request is the design
Confidence: 0.78 proceed (steady) - 3 memories
Done when: npx vitest run server/mapRoutes
Do not: touch other routes
Secrets: none
Return: Evidence Packet
"""


def repo(recall=RECALL, slug="map-name", preset=None, app=False):
    d = Path(tempfile.mkdtemp())
    fl = d / ".feature-ladder"
    if app:
        fl.mkdir(parents=True)
        (fl / "recall.json").write_text(json.dumps(recall), encoding="utf-8")
    else:
        (fl / "runs" / slug).mkdir(parents=True)
        (fl / "run.json").write_text(json.dumps({"slug": slug, "active": True}), encoding="utf-8")
        (fl / "runs" / slug / "recall.json").write_text(json.dumps(recall), encoding="utf-8")
    if preset:
        (fl / "providers.json").write_text(json.dumps({"openrouter": {"preset": preset}}), encoding="utf-8")
    return d


class PrepareCard(unittest.TestCase):
    def test_missing_memory_filled_from_recall_scope_first(self):
        root = repo()
        card, added = ew.prepare_card(CARD, root, "T1", 40, 1500)
        mem = ew._card_line(card, "Memory")
        self.assertIn("memory", added)
        self.assertTrue(mem.startswith("[pitfall] server/mapRoutes.ts"), mem)
        self.assertIn("[convention] npx vitest", mem)
        self.assertLess(mem.index("[convention]"), mem.index("[decision]"))

    def test_bounds_and_contract_built_in(self):
        card, added = ew.prepare_card(CARD, repo(), "T1", 12, 600, {"left": 3, "max": 4})
        bounds = ew._card_line(card, "Bounds")
        self.assertIn("12 turns", bounds)
        self.assertIn("600s", bounds)
        self.assertIn("3 of 4 left", bounds)
        self.assertIn("TASK T1 - <status", card)
        self.assertIn("Retained:", card)
        self.assertEqual({"memory", "bounds", "contract"}, set(added))

    def test_coordinator_memory_kept_verbatim(self):
        mine = CARD + "Memory: [pitfall] something the coordinator chose\n"
        card, added = ew.prepare_card(mine, repo(), "T1", 40, 1500)
        self.assertEqual("[pitfall] something the coordinator chose", ew._card_line(card, "Memory"))
        self.assertNotIn("memory", added)

    def test_memory_none_filled_only_when_recall_touches_scope(self):
        card, _ = ew.prepare_card(CARD + "Memory: none\n", repo(), "T1", 40, 1500)
        self.assertTrue(ew._card_line(card, "Memory").startswith("[pitfall]"))
        self.assertEqual(1, card.count("Memory:"))
        card, _ = ew.prepare_card(CARD + "Memory: none\n", repo({"results": [{"text": OTHER}]}), "T1", 40, 1500)
        self.assertEqual("none", ew._card_line(card, "Memory"))

    def test_no_recall_says_none(self):
        d = Path(tempfile.mkdtemp())
        card, _ = ew.prepare_card(CARD, d, "T1", 40, 1500)
        self.assertEqual("none", ew._card_line(card, "Memory"))

    def test_app_layout_recall(self):
        card, _ = ew.prepare_card(CARD, repo(app=True), "T1", 40, 1500)
        self.assertTrue(ew._card_line(card, "Memory").startswith("[pitfall]"))

    def test_existing_bounds_and_contract_not_duplicated(self):
        full = CARD + "Memory: none\nBounds: 5 turns\nRetained: none - n/a\n"
        card, added = ew.prepare_card(full, repo({"results": []}), "T1", 40, 1500)
        self.assertEqual([], added)
        self.assertEqual(1, card.count("Bounds:"))

    def test_memory_is_the_recall_the_sentiment_step_scores(self):
        """The card's Memory comes from the same recall.json confidence.py reads,
        so the worker sees exactly the facts behind the Confidence: line."""
        root = repo()
        f = ew.recall_file(root)
        scored = conf.decide("card", "save the map name", ["server/mapRoutes.ts"], "S",
                             memories=conf.parse_memories(f.read_text(encoding="utf-8")))
        card, _ = ew.prepare_card(CARD, root, "T1", 40, 1500)
        mem = ew._card_line(card, "Memory")
        for item in scored["surface"]["items"]:
            self.assertIn(item["text"][:80], mem)


class RunHandsTheCard(unittest.TestCase):
    """run() hands the prepared card to the child claude -p, for both presets."""

    def _run(self, preset):
        root = repo(preset=preset)
        card_file = root / ".feature-ladder" / "cards" / "T1.md"
        card_file.parent.mkdir(parents=True)
        card_file.write_text(CARD, encoding="utf-8")
        seen = {}
        real_run = subprocess.run

        def fake_run(cmd, **kw):
            if cmd[0] != "claude":
                return real_run(cmd, **kw)
            seen["cmd"], seen["input"], seen["env"] = cmd, kw.get("input"), kw.get("env")
            out = json.dumps({"type": "result", "num_turns": 3, "usage": {},
                              "result": "TASK T1 - done\nChanged: server/mapRoutes.ts\nRetained: none - n/a"})
            return subprocess.CompletedProcess(cmd, 0, out, "")

        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), \
                mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "get_key", return_value=("k", "test")), \
                mock.patch.object(ew, "usage_usd", return_value=None), \
                mock.patch.object(ew.subprocess, "run", side_effect=fake_run):
            import session_budget
            with mock.patch.object(session_budget, "check", return_value=(True, "")), \
                    mock.patch.object(session_budget, "status", return_value={"left": 2, "max": 4}):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = ew.run({"--card": str(card_file), "--id": "T1", "--cwd": str(root), "--max-turns": "9"})
        record = json.loads((root / ".feature-ladder" / "evidence" / "T1-external.json").read_text(encoding="utf-8"))
        return code, seen, record

    def _assert_card(self, seen, record):
        card = seen["input"]
        self.assertTrue(ew._card_line(card, "Memory").startswith("[pitfall] server/mapRoutes.ts"))
        self.assertIn("at most 9 turns", ew._card_line(card, "Bounds"))
        self.assertIn("2 of 4 left", ew._card_line(card, "Bounds"))
        self.assertIn("Output contract", card)
        self.assertEqual("9", seen["cmd"][seen["cmd"].index("--max-turns") + 1])
        self.assertIn("--append-system-prompt", seen["cmd"])
        self.assertEqual(card, record["card"])

    def test_jev_preset(self):
        code, seen, record = self._run("jev")
        self.assertEqual(0, code)
        self.assertTrue(record["model"].startswith("typesafe/jev-router"))
        self._assert_card(seen, record)

    def test_default_glm_queue(self):
        code, seen, record = self._run(None)
        self.assertEqual(0, code)
        self.assertTrue(record["model"].startswith("z-ai/glm-5.3-flash"))
        self._assert_card(seen, record)


if __name__ == "__main__":
    unittest.main()
