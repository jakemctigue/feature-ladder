"""confidence.parse_memories decodes MCP recall envelopes into individual memories.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import confidence as conf  # noqa: E402

ENVELOPES = json.loads((PLUGIN / "tests" / "fixtures" / "mcp_recall_envelopes.json").read_text(encoding="utf-8"))
EXPECT = [("pitfall", "deploy failed twice"), ("test-strategy", "npm test is the gate"), ("convention", "use rtk")]


class McpEnvelopes(unittest.TestCase):
    def test_both_shapes_yield_individual_memories_with_kinds(self):
        for name, payload in ENVELOPES.items():
            for form in (payload, json.dumps(payload)):
                with self.subTest(shape=name, as_text=isinstance(form, str)):
                    got = [(m["kind"], m["text"]) for m in conf.parse_memories(form)]
                    self.assertEqual(got, EXPECT)


class LiveRecallBeforeAnyMemory(unittest.TestCase):
    def test_a_repository_without_a_bank_is_no_memories_and_no_call(self):
        import tempfile
        from unittest import mock
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "hindsight-memory" / "scripts"))
        import resolve_bank
        calls = []

        class Boom:
            def start(self):
                return self

            def call(self, tool, args):
                calls.append(tool)
                raise AssertionError("no network call is needed")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(resolve_bank, "resolve", return_value={"bank_id": "coding-agent::x", "how": "planned"}), \
                mock.patch.object(resolve_bank, "Mcp", Boom):
            memories, why = conf.live_recall("retries in upload", tmp)
        self.assertEqual(memories, [])
        self.assertIn("no memories", why)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
