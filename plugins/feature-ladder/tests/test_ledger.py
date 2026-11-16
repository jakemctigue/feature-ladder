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


def block_lines(msg_id, usage, n, model="claude-sonnet-5-5", ts=None):
    """One API message as Claude Code writes it: one JSONL line per content block, same id, same usage."""
    out = []
    for i in range(n):
        rec = {"message": {"role": "assistant", "id": msg_id, "model": model, "usage": usage,
                           "content": [{"type": "text", "text": f"block {i}"}]}}
        if ts:
            rec["timestamp"] = ts
        out.append(rec)
    return out


def write_lines(path, recs, ts=None):
    first = {"message": {"role": "user", "content": CARD}}
    if ts:
        first["timestamp"] = ts
    Path(path).write_text("\n".join(json.dumps(r) for r in [first, *recs]) + "\n", encoding="utf-8")


class Accuracy(unittest.TestCase):
    def entries(self, root, slug="demo"):
        p = root / ".feature-ladder" / "runs" / slug / "usage.jsonl"
        return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []

    def test_one_api_message_written_as_several_lines_is_counted_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            t = root / "a.jsonl"
            write_lines(t, block_lines("msg_1", {"input_tokens": 3, "output_tokens": 100, "cache_read_input_tokens": 500}, 3)
                        + block_lines("msg_2", {"input_tokens": 2, "output_tokens": 40}, 2))
            hook(root, {"agent_type": "feature-ladder:implementer", "agent_id": "a1",
                        "agent_transcript_path": str(t), "cwd": str(root)})
            e = self.entries(root)[0]
            self.assertEqual((e["turns"], e["input"], e["output"], e["cache_read"]), (2, 5, 140, 500))

    def test_a_subagent_from_after_the_run_is_not_added_to_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            (root / ".feature-ladder" / "run.json").write_text(json.dumps(
                {"slug": "demo", "active": False, "started": "2026-10-23T12:59:37+00:00",
                 "finished": "2026-10-23T13:09:43+00:00"}), encoding="utf-8")
            late = root / "late.jsonl"
            write_lines(late, block_lines("m", {"output_tokens": 9}, 1, ts="2026-11-15T18:30:00.000Z"),
                        ts="2026-11-15T18:29:00.000Z")
            hook(root, {"agent_type": "general-purpose", "agent_id": "late", "agent_transcript_path": str(late),
                        "cwd": str(root)})
            self.assertEqual(self.entries(root), [])

    def test_the_coordinator_stopping_just_after_finish_is_still_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            (root / ".feature-ladder" / "run.json").write_text(json.dumps(
                {"slug": "demo", "active": False, "started": "2026-10-23T12:59:37+00:00",
                 "finished": "2026-10-23T13:09:43+00:00"}), encoding="utf-8")
            t = root / "coord.jsonl"
            write_lines(t, block_lines("m", {"output_tokens": 9}, 1, ts="2026-10-23T13:05:00.000Z"),
                        ts="2026-10-23T13:00:10.000Z")
            hook(root, {"agent_type": "feature-ladder:coordinator", "agent_id": "c1", "agent_transcript_path": str(t),
                        "cwd": str(root)})
            self.assertEqual(len(self.entries(root)), 1)

    def test_the_parent_sessions_transcript_is_never_summed_as_the_subagents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            parent = root / "session.jsonl"
            transcript(parent, [{"input_tokens": 100000, "output_tokens": 50000}])
            hook(root, {"agent_type": "feature-ladder:implementer", "agent_id": "a1",
                        "transcript_path": str(parent), "cwd": str(root)})
            self.assertEqual(self.entries(root), [])

    def test_an_unparsable_line_in_the_ledger_is_kept_and_does_not_stop_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_run(tmp)
            p = root / ".feature-ladder" / "runs" / "demo" / "usage.jsonl"
            p.parent.mkdir(parents=True)
            p.write_text("{broken\n", encoding="utf-8")
            t = root / "a.jsonl"
            transcript(t, [{"input_tokens": 1, "output_tokens": 2}])
            hook(root, {"agent_type": "mechanic", "agent_id": "m1", "agent_transcript_path": str(t), "cwd": str(root)})
            lines = p.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], "{broken")


if __name__ == "__main__":
    unittest.main()
