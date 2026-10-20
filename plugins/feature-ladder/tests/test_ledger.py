"""Tests for the token ledger hook and its report.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "hooks" / "scripts" / "ledger.py"

CARD = "TASK T3: add retry\nRung: implementer (sonnet/high)   Router score: 7   Flags: none\nScope: x.py\n"


def transcript(path, usages, model="claude-sonnet-5-5", card=CARD):
    recs = [{"message": {"role": "user", "content": card}}]
    for u in usages:
        recs.append({"message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}],
                                 "usage": u}})
    Path(path).write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def hook(root, payload):
    r = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload), cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r


def report(root, *args):
    r = subprocess.run([sys.executable, str(SCRIPT), "report", *args], cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    return r.returncode, r.stdout


def make_run(tmp, slug="demo"):
    root = Path(tmp)
    (root / ".git").mkdir()  # find_run stops at a repo root
    fl = root / ".feature-ladder"
    fl.mkdir()
    (fl / "run.json").write_text(json.dumps({"slug": slug, "active": True}), encoding="utf-8")
    return root


class Hook(unittest.TestCase):
    def test_records_usage_rung_and_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            t = root / "a.jsonl"
            transcript(t, [{"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 1000,
                            "cache_creation_input_tokens": 50},
                           {"input_tokens": 10, "output_tokens": 30}])
            r = hook(root, {"agent_type": "feature-ladder:implementer", "agent_id": "a1",
                            "agent_transcript_path": str(t), "cwd": str(root)})
            self.assertEqual(r.returncode, 0)
            self.assertEqual(r.stdout, "")  # never blocks, never speaks
            lines = (root / ".feature-ladder" / "runs" / "demo" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            e = json.loads(lines[0])
            self.assertEqual((e["rung"], e["model"], e["effort"], e["task"]), ("implementer", "claude-sonnet-5-5", "high", "T3"))
            self.assertEqual((e["turns"], e["input"], e["output"], e["cache_read"], e["cache_write"]), (2, 110, 50, 1000, 50))
            self.assertEqual(e["total"], 1210)
            self.assertIn("runs/", (root / ".feature-ladder" / ".gitignore").read_text(encoding="utf-8"))

    def test_second_stop_of_same_agent_replaces_not_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            t = root / "a.jsonl"
            transcript(t, [{"input_tokens": 1, "output_tokens": 1}])
            payload = {"agent_type": "mechanic", "agent_id": "m1", "agent_transcript_path": str(t), "cwd": str(root)}
            hook(root, payload)
            transcript(t, [{"input_tokens": 1, "output_tokens": 1}, {"input_tokens": 2, "output_tokens": 2}])
            hook(root, dict(payload, stop_hook_active=True))
            lines = (root / ".feature-ladder" / "runs" / "demo" / "usage.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["turns"], 2)

    def test_no_run_means_nothing_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            t = root / "a.jsonl"
            transcript(t, [{"input_tokens": 1, "output_tokens": 1}])
            r = hook(root, {"agent_type": "mechanic", "agent_transcript_path": str(t), "cwd": str(root)})
            self.assertEqual(r.returncode, 0)
            self.assertFalse((root / ".feature-ladder").exists())

    def test_garbage_payload_is_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run([sys.executable, str(SCRIPT)], input="not json", cwd=tmp,
                               capture_output=True, text=True, encoding="utf-8", timeout=60)
            self.assertEqual((r.returncode, r.stdout), (0, ""))


class Report(unittest.TestCase):
    def test_table_by_rung(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            for i, (agent, model, card, out) in enumerate([
                    ("implementer", "claude-sonnet-5-5", CARD, 300),
                    ("implementer", "claude-sonnet-5-5", CARD, 100),
                    ("mechanic", "claude-haiku-5-5", "TASK T4-check: run checks\nRung: mechanic (haiku/low)\n", 100)]):
                t = root / f"{i}.jsonl"
                transcript(t, [{"input_tokens": 10, "output_tokens": out}], model=model, card=card)
                hook(root, {"agent_type": agent, "agent_id": f"k{i}", "agent_transcript_path": str(t), "cwd": str(root)})
            code, out = report(root)
            self.assertEqual(code, 0)
            self.assertIn("| implementer | claude-sonnet-5-5/high | 2 | 2 | 20 |", out)
            self.assertIn("| 400 | 80% |", out)
            self.assertIn("| mechanic | claude-haiku-5-5/low | 1 | 1 | 10 |", out)
            self.assertIn("| **all** | | 3 | 3 | 30 |", out)
            code, js = report(root, "--json")
            self.assertEqual(json.loads(js)["slug"], "demo")

    def test_empty_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            code, out = report(root)
            self.assertEqual(code, 0)
            self.assertIn("No ledger", out)


if __name__ == "__main__":
    unittest.main()
