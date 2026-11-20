"""Shared state helpers: one state root, atomic JSON writes, a lock, one .gitignore list.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import ladder_state  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=str(cwd), check=True, capture_output=True)


class StateRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "main"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_subfolder_resolves_to_the_checkout_root(self):
        (self.root / "src" / "deep").mkdir(parents=True)
        self.assertEqual(ladder_state.state_root(self.root / "src" / "deep"), self.root)

    def test_a_linked_worktree_resolves_to_the_main_checkout(self):
        wt = self.root.parent / "wt"
        git(self.root, "worktree", "add", "-q", str(wt))
        self.assertEqual(ladder_state.state_root(wt), self.root)

    def test_outside_git_the_nearest_feature_ladder_folder_wins(self):
        plain = Path(self.tmp.name).resolve() / "plain"
        (plain / ".feature-ladder").mkdir(parents=True)
        (plain / "a" / "b").mkdir(parents=True)
        with unittest.mock.patch.object(ladder_state, "_git_common_dir", return_value=None):
            self.assertEqual(ladder_state.state_root(plain / "a" / "b"), plain)


class JsonState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_reads_as_the_default(self):
        self.assertEqual(ladder_state.read_json(self.dir / "nope.json", {"a": 1}), {"a": 1})

    def test_corrupt_file_reads_as_default_unless_strict(self):
        p = self.dir / "s.json"
        p.write_text('{"size": "S", "spawned": [{"agent": "impl', encoding="utf-8")
        self.assertIsNone(ladder_state.read_json(p))
        with self.assertRaises(ladder_state.CorruptState):
            ladder_state.read_json(p, strict=True)

    def test_write_is_atomic_and_leaves_no_temp_file(self):
        p = self.dir / "sub" / "s.json"
        ladder_state.write_json(p, {"a": [1, 2]})
        self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": [1, 2]})
        self.assertEqual([f.name for f in p.parent.iterdir()], ["s.json"])

    def test_a_read_during_a_windows_replace_is_retried_not_missing(self):
        f = Path(self.tmp.name) / "run.json"
        ladder_state.write_json(f, {"active": True})
        real = Path.read_text
        calls = []

        def flaky(path, *a, **kw):
            calls.append(path)
            if len(calls) < 3:
                raise PermissionError(13, "The process cannot access the file")
            return real(path, *a, **kw)

        with mock.patch.object(ladder_state, "WINDOWS", True), \
                mock.patch.object(ladder_state.time, "sleep"), \
                mock.patch.object(Path, "read_text", flaky):
            self.assertEqual(ladder_state.read_json(f), {"active": True})
        self.assertEqual(len(calls), 3)

    def test_a_lasting_permission_error_still_reads_as_the_default(self):
        f = Path(self.tmp.name) / "run.json"
        ladder_state.write_json(f, {"active": True})
        with mock.patch.object(ladder_state, "WINDOWS", True), \
                mock.patch.object(ladder_state.time, "sleep"), \
                mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")):
            self.assertEqual(ladder_state.read_json(f, default="d"), "d")

    def test_bom_is_tolerated(self):
        p = self.dir / "s.json"
        p.write_text("﻿{\"a\": 1}", encoding="utf-8")
        self.assertEqual(ladder_state.read_json(p), {"a": 1})

    def test_locked_update_loses_no_writes_under_contention(self):
        p = self.dir / ".feature-ladder" / "counter.json"
        ladder_state.write_json(p, {"n": 0})

        def bump():
            for _ in range(15):
                with ladder_state.locked(self.dir):
                    d = ladder_state.read_json(p, strict=True)
                    d["n"] += 1
                    ladder_state.write_json(p, d)

        threads = [threading.Thread(target=bump) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(ladder_state.read_json(p)["n"], 90)

    def test_a_stale_lock_is_broken(self):
        lock = self.dir / ".feature-ladder" / "state.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("stale", encoding="utf-8")
        import os
        old = lock.stat().st_mtime - 3600
        os.utime(lock, (old, old))
        with ladder_state.locked(self.dir, timeout=2):
            pass
        self.assertFalse(lock.exists())


class Gitignore(unittest.TestCase):
    def test_every_runtime_entry_is_listed_once_and_user_lines_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gi = root / ".feature-ladder" / ".gitignore"
            gi.parent.mkdir()
            gi.write_text("mine/\nruns/\n", encoding="utf-8")
            ladder_state.ensure_gitignore(root)
            ladder_state.ensure_gitignore(root)
            lines = gi.read_text(encoding="utf-8").splitlines()
            self.assertIn("mine/", lines)
            for entry in ladder_state.GITIGNORE:
                self.assertEqual(lines.count(entry), 1, entry)
            for entry in ("run.json", "session.json", "runs/", "cards/", "evidence/", "memory-ledger.jsonl", "*.lock"):
                self.assertIn(entry, ladder_state.GITIGNORE)


import unittest.mock  # noqa: E402  (used by StateRoot)

class ProcessTree(unittest.TestCase):
    """One implementation of "run it, and at the timeout stop everything it started"."""

    def test_a_command_that_finishes_returns_its_code_and_output(self):
        code, out, err = ladder_state.run_tree([sys.executable, "-c", "import sys; print(40 + 2); sys.exit(3)"])
        self.assertEqual((code, out.strip()), (3, "42"))

    def test_the_timeout_stops_the_command_and_what_it_started(self):
        import time
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "grandchild-finished"
            grandchild = Path(tmp) / "grandchild.py"
            grandchild.write_text("import sys, time, pathlib\ntime.sleep(4)\npathlib.Path(sys.argv[1]).write_text('x')\n",
                                  encoding="utf-8")
            child = Path(tmp) / "child.py"
            child.write_text("import subprocess, sys, time\n"
                             "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\ntime.sleep(30)\n",
                             encoding="utf-8")
            t0 = time.monotonic()
            code, _, _ = ladder_state.run_tree([sys.executable, str(child), str(grandchild), str(marker)], timeout=1)
            self.assertIsNone(code)
            self.assertLess(time.monotonic() - t0, 10)
            time.sleep(5)
            self.assertFalse(marker.exists(), "the grandchild outlived the timeout")

    def test_the_scripts_share_this_one_helper(self):
        sys.path.insert(0, str(PLUGIN / "skills" / "cross-model-review" / "scripts"))
        import codex_review
        import pr_checkpoint
        import verify
        self.assertIs(verify.run_tree, ladder_state.run_tree)
        self.assertIs(verify.kill_tree, ladder_state.kill_tree)
        self.assertIs(verify.find_bash, ladder_state.find_bash)
        self.assertIs(pr_checkpoint._kill_tree, ladder_state.kill_tree)
        self.assertIs(codex_review.kill_tree, ladder_state.kill_tree)


if __name__ == "__main__":
    unittest.main()
