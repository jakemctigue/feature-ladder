"""session_budget.py: state that survives corruption, races, slug reuse and second sessions.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
LADDER = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(LADDER))
sys.path.insert(0, str(PLUGIN / "skills" / "hindsight-memory" / "scripts"))

import memory_gate  # noqa: E402
import session_budget  # noqa: E402

SCRIPT = str(LADDER / "session_budget.py")
GOOD = "[pitfall] Firestore batch writes over 500 ops fail in src/maps_workflow.py; split them with chunked()"


def cli(root, *args, env=None, stdin=None):
    return subprocess.run([sys.executable, SCRIPT, *args, "--cwd", str(root)], input=stdin,
                          capture_output=True, text=True, timeout=60, env=env)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.dir = self.root / ".feature-ladder"
        self.dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def start_run(self, slug="maps", started="2026-11-15T10:00:00+00:00", budget=None, active=True):
        run = {"active": active, "slug": slug, "started": started,
               "budget": budget or {"size": "S", "workers": 1, "design": False}, "switches": []}
        (self.dir / "run.json").write_text(json.dumps(run), encoding="utf-8")

    def hook(self, agent_type, model=None):
        payload = {"tool_name": "Agent", "cwd": str(self.root),
                   "tool_input": {"subagent_type": agent_type, "model": model, "prompt": "x"}}
        r = subprocess.run([sys.executable, SCRIPT, "--pretooluse"], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60)
        return not r.stdout.strip(), r.stdout


class CorruptState(Base):
    def test_an_unreadable_session_file_denies_the_spawn_and_is_left_alone(self):
        session_budget.set_size(self.root, "S")
        self.start_run()
        broken = '{"size": "S", "max_agents": 2, "spawned": [{"agent": "impl'
        (self.dir / "session.json").write_text(broken, encoding="utf-8")
        ok, reason = session_budget.check(self.root, "implementer", "sonnet")
        self.assertFalse(ok)
        self.assertIn("session.json", reason)
        self.assertEqual((self.dir / "session.json").read_text(encoding="utf-8"), broken)

    def test_status_reports_an_unreadable_session_file(self):
        (self.dir / "session.json").write_text("{not json", encoding="utf-8")
        r = cli(self.root, "status")
        self.assertEqual(json.loads(r.stdout)["budget"], "unreadable")

    def test_new_replaces_an_unreadable_session_file(self):
        (self.dir / "session.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(cli(self.root, "new").returncode, 0)
        self.assertEqual(session_budget.status(self.root)["budget"], "unset")


class Races(Base):
    def test_parallel_charges_never_exceed_the_session_budget(self):
        session_budget.set_size(self.root, "L")  # 8

        def charge(_):
            return cli(self.root, "charge", "--agent", "implementer", "--model", "glm").returncode

        with ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(charge, range(12)))
        self.assertEqual(codes.count(0), 8, codes)
        self.assertEqual(codes.count(4), 4, codes)
        self.assertEqual(session_budget.status(self.root)["used"], 8)


class RunScope(Base):
    def test_a_new_run_with_the_same_slug_does_not_inherit_the_old_runs_spawns(self):
        session_budget.set_size(self.root, "L")
        self.start_run(started="2026-11-15T10:00:00+00:00")
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        self.assertFalse(session_budget.check(self.root, "implementer", "sonnet")[0])  # workers 1 + retry
        self.start_run(started="2026-11-15T11:30:00+00:00")  # retry after an abort: same slug
        ok, reason = session_budget.check(self.root, "implementer", "sonnet")
        self.assertTrue(ok, reason)

    def test_a_retry_of_the_same_worker_is_not_two_coding_agents(self):
        session_budget.set_size(self.root, "L")
        self.start_run(budget={"size": "M", "workers": 2, "design": False})
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])  # the retry
        ok, reason = session_budget.check(self.root, "integrator", "fable")
        self.assertFalse(ok)
        self.assertIn("different", reason)
        self.assertTrue(session_budget.check(self.root, "implementer", "z-ai/glm-5.3-flash", via="external")[0])
        self.assertTrue(session_budget.check(self.root, "integrator", "fable")[0])


class Refund(Base):
    def test_a_worker_that_never_ran_gives_its_slot_back(self):
        session_budget.set_size(self.root, "S")
        self.start_run(budget={"size": "M", "workers": 2, "design": False})
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        self.assertTrue(session_budget.check(self.root, "implementer", "glm", via="external")[0])
        self.assertFalse(session_budget.check(self.root, "implementer", "glm", via="external")[0])  # S = 2, spent
        self.assertTrue(session_budget.refund(self.root, "feature-ladder:implementer", "glm", via="external"))
        self.assertEqual(session_budget.status(self.root)["used"], 1)
        self.assertTrue(session_budget.check(self.root, "implementer", "glm", via="external")[0])

    def test_refund_removes_only_a_matching_spawn(self):
        session_budget.set_size(self.root, "S")
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        self.assertFalse(session_budget.refund(self.root, "implementer", "glm", via="external"))
        self.assertEqual(session_budget.status(self.root)["used"], 1)


class Sessions(Base):
    def session_start(self, session_id, source="startup"):
        env = {k: v for k, v in os.environ.items() if k != "FEATURE_LADDER_CHILD"}
        subprocess.run([sys.executable, SCRIPT, "--session-start"], timeout=60, env=env, text=True,
                       input=json.dumps({"source": source, "session_id": session_id, "cwd": str(self.root)}))

    def test_a_second_session_does_not_reset_the_budget_while_a_run_is_active(self):
        session_budget.set_size(self.root, "S")
        self.start_run()
        self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        self.session_start("another-window")
        st = session_budget.status(self.root)
        self.assertEqual((st["budget"], st["used"]), ("S", 1))

    def test_a_new_session_still_resets_it_when_no_run_is_active(self):
        session_budget.set_size(self.root, "S")
        self.start_run(active=False)
        self.session_start("next-day")
        self.assertEqual(session_budget.status(self.root)["budget"], "unset")


class WhatCounts(Base):
    def test_agents_from_outside_the_ladder_are_not_charged_outside_a_run(self):
        session_budget.set_size(self.root, "S")
        for agent in ("Explore", "web-fetch", "general-purpose", "claude-code-guide"):
            self.assertTrue(self.hook(agent)[0], agent)
        self.assertEqual(session_budget.status(self.root)["used"], 0)
        self.assertTrue(self.hook("feature-ladder:codex-reviewer", "haiku")[0])
        self.assertEqual(session_budget.status(self.root)["used"], 1)

    def test_any_agent_is_charged_inside_a_run(self):
        session_budget.set_size(self.root, "L")
        self.start_run()
        self.assertTrue(self.hook("general-purpose", "sonnet")[0])
        self.assertEqual(session_budget.status(self.root)["used"], 1)


class Queue(Base):
    def test_close_keeps_queued_lines_that_were_never_written(self):
        lines = [f"[pitfall] `mod_{i}.py` must call setup_{i}() before run_{i}()." for i in range(3)]
        self.assertEqual(session_budget.remember(self.root, lines)["queued"], 3)
        memory_gate.record(self.root, memory_gate.parse_line(lines[0])[1], "pitfall")  # only the first was retained
        self.assertEqual(cli(self.root, "close").returncode, 0)
        left = (self.dir / "session-memory.md").read_text(encoding="utf-8")
        self.assertNotIn("mod_0.py", left)
        self.assertIn("mod_1.py", left)
        self.assertIn("mod_2.py", left)
        self.assertEqual(len(list(self.dir.glob("session-memory-*.md"))), 1)  # the full queue is archived

    def test_close_archives_a_fully_written_queue(self):
        session_budget.remember(self.root, [GOOD])
        memory_gate.record(self.root, memory_gate.parse_line(GOOD)[1], "pitfall")
        cli(self.root, "close")
        self.assertFalse((self.dir / "session-memory.md").exists())

    def test_memory_prints_text_a_cp1252_console_cannot_encode(self):
        (self.dir / "session-memory.md").write_text("- [pitfall] `a.py` → call b() first\n", encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        r = subprocess.run([sys.executable, SCRIPT, "memory", "--cwd", str(self.root)],
                           capture_output=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace")[-300:])
        self.assertIn("a.py", r.stdout.decode("utf-8", "replace"))


class CommandLine(Base):
    def test_a_misspelt_option_is_an_error_not_silently_ignored(self):
        r = cli(self.root, "set", "--sise", "S")
        self.assertEqual(r.returncode, 1)
        self.assertIn("--sise", r.stderr)
        self.assertEqual(session_budget.status(self.root)["budget"], "unset")

    def test_an_option_without_its_value_is_an_error(self):
        r = subprocess.run([sys.executable, SCRIPT, "charge", "--cwd", str(self.root), "--agent"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--agent", r.stderr)

    def test_the_subcommands_still_parse_their_options(self):
        self.assertEqual(json.loads(cli(self.root, "set", "--size", "M").stdout)["max"], 4)
        r = cli(self.root, "remember", "--line", GOOD, "--line", "[outcome] shipped it")
        self.assertEqual(json.loads(r.stdout)["queued"], 1)
        self.assertEqual(json.loads(cli(self.root, "orchestrator", "--set", "fable").stdout)["key"], "fable")


if __name__ == "__main__":
    unittest.main()
