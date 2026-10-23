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


if __name__ == "__main__":
    unittest.main()
