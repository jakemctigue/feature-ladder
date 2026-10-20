"""Tests for the retain gate: who is bounced for a missing Retained footer, who is exempt.

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
    def test_worker_without_footer_is_bounced_once(self):
        code, out = hook({"agent_type": "feature-ladder:implementer", "last_assistant_message": "TASK T1 - done\nChanged: a.py"})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["decision"], "block")

    def test_worker_with_footer_passes(self):
        code, out = hook({"agent_type": "feature-ladder:implementer", "last_assistant_message": "...\nRetained: none - nothing durable"})
        self.assertEqual((code, out), (0, ""))

    def test_exempt_rungs_are_never_bounced(self):
        for agent in ("feature-ladder:mechanic", "mechanic", "feature-ladder:codex-reviewer", "feature-ladder:memory-scribe"):
            code, out = hook({"agent_type": agent, "last_assistant_message": "TASK check - done\nChecks run: pytest -> 0"})
            self.assertEqual((code, out), (0, ""), agent)

    def test_second_stop_is_not_bounced_again(self):
        code, out = hook({"agent_type": "feature-ladder:implementer", "stop_hook_active": True,
                          "last_assistant_message": "no footer"})
        self.assertEqual((code, out), (0, ""))

    def test_other_agents_are_ignored(self):
        code, out = hook({"agent_type": "Explore", "last_assistant_message": "no footer"})
        self.assertEqual((code, out), (0, ""))


if __name__ == "__main__":
    unittest.main()
