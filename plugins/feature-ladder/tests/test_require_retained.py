"""Tests for the retain gate: only the coordinator is bounced for a missing Retained footer;
workers defer memory to /feature-ladder:wrap and are never bounced.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "require_retained.py"


def hook(payload):
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    return r.returncode, r.stdout.strip()


class RetainGate(unittest.TestCase):
    def test_coordinator_without_footer_is_bounced_once(self):
        code, out = hook({"agent_type": "feature-ladder:coordinator", "last_assistant_message": "Done. PR #3"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertIn("session_budget.py remember", json.loads(out)["reason"])

    def test_coordinator_with_footer_passes(self):
        code, out = hook({"agent_type": "feature-ladder:coordinator",
                          "last_assistant_message": "...\nRetained: deferred - 2 lines queued for /feature-ladder:wrap"})
        self.assertEqual((code, out), (0, ""))

    def test_workers_are_never_bounced(self):
        for agent in ("feature-ladder:implementer", "feature-ladder:mechanic", "mechanic", "feature-ladder:domain-expert",
                      "feature-ladder:codex-reviewer", "feature-ladder:integrator"):
            code, out = hook({"agent_type": agent, "last_assistant_message": "TASK check - done\nChecks run: pytest -> 0"})
            self.assertEqual((code, out), (0, ""), agent)

    def test_second_stop_is_not_bounced_again(self):
        code, out = hook({"agent_type": "feature-ladder:coordinator", "stop_hook_active": True,
                          "last_assistant_message": "no footer"})
        self.assertEqual((code, out), (0, ""))

    def test_other_agents_are_ignored(self):
        code, out = hook({"agent_type": "Explore", "last_assistant_message": "no footer"})
        self.assertEqual((code, out), (0, ""))


class QueueIsChecked(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".feature-ladder").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def stop(self, message):
        return hook({"agent_type": "feature-ladder:coordinator", "cwd": str(self.root),
                     "last_assistant_message": message})

    def test_the_footer_alone_does_not_pass_when_nothing_was_queued(self):
        code, out = self.stop("Done.\nRetained: deferred - 5 lines queued for /feature-ladder:wrap")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "block")
        self.assertIn("queue is empty", json.loads(out)["reason"])

    def test_the_footer_passes_when_the_queue_has_lines(self):
        (self.root / ".feature-ladder" / "session-memory.md").write_text(
            "- [pitfall] `a.py` must call setup() before run().\n", encoding="utf-8")
        self.assertEqual(self.stop("Done.\nRetained: deferred - 1 lines queued for /feature-ladder:wrap"), (0, ""))

    def test_nothing_worth_keeping_is_an_honest_footer(self):
        self.assertEqual(self.stop("Done.\nRetained: deferred - 0 lines queued for /feature-ladder:wrap"), (0, ""))

    def test_a_transcript_line_whose_message_is_text_does_not_crash_the_hook(self):
        t = self.root / "t.jsonl"
        t.write_text(json.dumps({"message": "plain string"}) + "\n" + json.dumps(
            {"message": {"role": "assistant", "content": "no footer here"}}) + "\n", encoding="utf-8")
        code, out = hook({"agent_type": "feature-ladder:coordinator", "agent_transcript_path": str(t)})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "block")


if __name__ == "__main__":
    unittest.main()
