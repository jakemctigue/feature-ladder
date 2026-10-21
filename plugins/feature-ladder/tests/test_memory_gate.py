"""The Hindsight gate: scoped recall, no writes during a run, only concrete repo-anchored facts.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "hindsight-memory" / "scripts"))
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import memory_gate  # noqa: E402
import session_budget  # noqa: E402

GOOD = "[pitfall] Firestore batch writes over 500 ops fail in src/maps_workflow.py; split them with chunked()"


class Facts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".feature-ladder").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def rejected(self, line):
        with self.assertRaises(ValueError):
            memory_gate.check_line(self.root, line)

    def test_a_concrete_anchored_fact_passes(self):
        kind, fact, _ = memory_gate.check_line(self.root, GOOD)
        self.assertEqual(kind, "pitfall")
        self.assertTrue(fact.startswith("Firestore"))

    def test_summaries_narration_and_vague_lines_are_rejected(self):
        for line in ("[outcome] Added the maps workflow status write",               # no outcome kind
                     "Firestore writes need chunking in src/maps_workflow.py",        # no kind
                     "[decision] We implemented the status write in src/maps.py",    # narration
                     "[convention] The tests pass and everything works well overall",  # summary, no anchor
                     "[pitfall] Be careful with writes",                              # too short, no anchor
                     "[decision] " + "x_y " * 80):                                    # too long
            self.rejected(line)

    def test_duplicates_are_rejected(self):
        memory_gate.record(self.root, memory_gate.parse_line(GOOD)[1], "pitfall")
        self.rejected(GOOD)

    def test_remember_queues_good_lines_and_refuses_the_rest(self):
        out = session_budget.remember(self.root, [GOOD, "[outcome] shipped it", GOOD])
        self.assertEqual(out["queued"], 1)
        self.assertEqual(len(out["rejected"]), 2)
        flushed = memory_gate.flush(self.root, dry_run=True)
        self.assertEqual(flushed["would_write"], [GOOD.replace("[pitfall] ", "[pitfall] ", 1)])


class HookGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".feature-ladder").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_active(self, active=True):
        (self.root / ".feature-ladder" / "run.json").write_text(json.dumps({"active": active, "slug": "maps"}))

    def gate(self, tool, args):
        return memory_gate.gate(f"mcp__plugin_feature-ladder_hindsight__{tool}", args, self.root, "bossforge")

    def test_recall_is_scoped_capped_and_counted(self):
        self.run_active()
        ok, _, args = self.gate("recall", {"query": "maps workflow firestore", "max_tokens": 8000, "tags": ["x"]})
        self.assertTrue(ok)
        self.assertEqual(args["tags"], ["project:bossforge"])
        self.assertEqual(args["max_tokens"], memory_gate.MAX_RECALL_TOKENS)
        for _ in range(memory_gate.RUN_RECALLS - 1):
            self.assertTrue(self.gate("recall", {"query": "q"})[0])
        self.assertFalse(self.gate("recall", {"query": "q"})[0])

    def test_no_retain_during_a_run(self):
        self.run_active()
        ok, reason, _ = self.gate("retain", {"content": GOOD})
        self.assertFalse(ok)
        self.assertIn("wrap", reason)

    def test_retain_outside_a_run_is_gated_and_normalised(self):
        ok, _, args = self.gate("retain", {"content": GOOD, "tags": ["feature:maps", "random"]})
        self.assertTrue(ok)
        self.assertEqual(args["tags"], ["project:bossforge", "feature:maps", "pitfall"])
        self.assertEqual(args["context"], "feature-ladder")
        self.assertFalse(self.gate("retain", {"content": GOOD})[0])  # now a duplicate
        self.assertFalse(self.gate("retain", {"content": "[decision] We built it successfully"})[0])

    def test_reflect_and_other_tools_are_denied(self):
        for tool in ("reflect", "get_knowledge_page", "update_bank", "delete_memory"):
            self.assertFalse(self.gate(tool, {})[0], tool)
        self.assertTrue(self.gate("list_banks", {})[0])

    def test_hook_denies_through_resolve_bank(self):
        self.run_active()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        payload = {"tool_name": "mcp__plugin_feature-ladder_hindsight__retain", "cwd": str(self.root),
                   "tool_input": {"content": GOOD}}
        r = subprocess.run([sys.executable, str(PLUGIN / "skills" / "hindsight-memory" / "scripts" / "resolve_bank.py"),
                            "--pretooluse"], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        self.assertEqual(json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")


if __name__ == "__main__":
    unittest.main()
