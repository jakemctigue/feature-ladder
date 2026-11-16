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
        memory_gate.confirm(self.root, args)                         # PostToolUse: the write happened
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


class WriteIsOnlyRecordedOnceItHappened(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".feature-ladder").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, tool, args):
        return memory_gate.gate(f"mcp__plugin_feature-ladder_hindsight__{tool}", args, self.root, "bossforge")

    def test_a_retain_that_never_completed_can_be_retried(self):
        ok, _, args = self.gate("retain", {"content": GOOD})
        self.assertTrue(ok)
        self.assertEqual(memory_gate.retained_hashes(self.root), set())  # nothing is "retained" yet
        ok, reason, _ = self.gate("retain", {"content": GOOD})
        self.assertFalse(ok)                                             # the first call is still in flight
        self.assertIn("in flight", reason)
        p, st = memory_gate._state(self.root)
        st["pending"] = {h: 0 for h in st["pending"]}                    # ...and then it timed out / was denied
        p.write_text(json.dumps(st), encoding="utf-8")
        self.assertTrue(self.gate("retain", {"content": GOOD})[0])

    def test_confirm_records_the_fact_and_then_it_is_a_duplicate(self):
        ok, _, args = self.gate("retain", {"content": GOOD})
        self.assertTrue(memory_gate.confirm(self.root, args))
        self.assertEqual(len(memory_gate.retained_hashes(self.root)), 1)
        ok, reason, _ = self.gate("retain", {"content": GOOD})
        self.assertFalse(ok)
        self.assertIn("already retained", reason)

    def test_already_normalised_calls_pass_through_unchanged(self):
        recall = {"query": "maps workflow", "tags": ["project:bossforge"], "tags_match": "all", "max_tokens": 1000,
                  "budget": "low", "types": ["world", "experience"]}
        ok, _, out = self.gate("recall", dict(recall))
        self.assertTrue(ok)
        self.assertEqual(out, recall)  # nothing for the hook to rewrite: no second call in auto mode
        q = self.root / ".feature-ladder" / "session-memory.md"
        q.write_text(f"- {GOOD}\n", encoding="utf-8")
        call = memory_gate.flush(self.root, dry_run=True, project="bossforge")["retain_calls"][0]
        ok, _, out = self.gate("retain", json.loads(json.dumps(call)))
        self.assertTrue(ok)
        self.assertEqual(out, call)


class GateIsRobust(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".feature-ladder").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_malformed_arguments_are_normalised_not_raised(self):
        ok, _, args = memory_gate.gate("x__recall", {"query": "q", "max_tokens": "lots"}, self.root, "p")
        self.assertTrue(ok)
        self.assertEqual(args["max_tokens"], memory_gate.MAX_RECALL_TOKENS)
        self.assertEqual(args["tags"], ["project:p"])
        ok, _, args = memory_gate.gate("x__retain", {"content": GOOD, "tags": "pitfall", "metadata": "oops"},
                                       self.root, "p")
        self.assertTrue(ok)
        self.assertEqual(args["metadata"], {"source": "feature-ladder", "kind": "pitfall"})

    def test_recall_skips_observation_duplicates_by_default(self):
        _, _, args = memory_gate.gate("x__recall", {"query": "q"}, self.root, "p")
        self.assertNotIn("observation", args["types"])

    def test_recall_outside_a_run_is_not_counted_and_old_runs_are_pruned(self):
        state = self.root / ".feature-ladder" / "memory-gate.json"
        state.write_text(json.dumps({"recalls": {"old-run": 3, "no-run": 40}}), encoding="utf-8")
        memory_gate.gate("x__recall", {"query": "q"}, self.root, "p")
        self.assertEqual(json.loads(state.read_text(encoding="utf-8")).get("recalls", {}), {})
        (self.root / ".feature-ladder" / "run.json").write_text(
            json.dumps({"active": True, "slug": "maps", "started": "t1"}), encoding="utf-8")
        memory_gate.gate("x__recall", {"query": "q"}, self.root, "p")
        self.assertEqual(list(json.loads(state.read_text(encoding="utf-8"))["recalls"].values()), [1])

    def test_flush_reports_the_lines_it_left_for_the_next_wrap(self):
        q = self.root / ".feature-ladder" / "session-memory.md"
        q.write_text("".join(f"- [pitfall] `mod_{i}.py` must call setup_{i}() before run_{i}().\n" for i in range(20)),
                     encoding="utf-8")
        out = memory_gate.flush(self.root, dry_run=True, project="p")
        self.assertEqual(len(out["would_write"]), memory_gate.SESSION_RETAINS)
        self.assertEqual(len(out["deferred"]), 20 - memory_gate.SESSION_RETAINS)
        self.assertEqual(out["queued"], len(out["would_write"]) + len(out["deferred"]) + len(out["rejected"]))


class FactRules(unittest.TestCase):
    def ok(self, line):
        memory_gate.parse_line(line)

    def bad(self, line):
        with self.assertRaises(ValueError):
            memory_gate.parse_line(line)

    def test_abbreviations_are_not_sentence_ends(self):
        self.ok("[pitfall] Pin actions in .github/workflows/tests.yml, e.g. actions/checkout@v4, i.e. no floating tags.")
        self.bad("[pitfall] `route.py` prints rungs. It exits 2 on bad input. Check stderr for the reason.")

    def test_the_user_as_a_domain_noun_is_not_narration(self):
        self.ok("[convention] Rows in the user table of schema.sql must keep the tenant_id column first.")
        self.bad("[decision] The user asked for retries in src/upload.py so they were added.")

    def test_prose_with_a_slash_is_not_an_anchor(self):
        self.bad("[decision] Things are generally good and/or fine in most cases really.")
        self.bad("[decision] It is 24/7 important to remember that quality matters a lot here.")
        self.ok("[convention] New components go under src/components and are exported from the index.")


if __name__ == "__main__":
    unittest.main()
