"""hooks/scripts/guard_irreversible.py: the PreToolUse(Bash) audit hook.

It runs before EVERY Bash call, so it must cost nothing when no run is active,
find the run from a linked worktree, and never lose a row when two commands
are logged at once.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "hooks" / "scripts" / "guard_irreversible.py"
sys.path.insert(0, str(PLUGIN / "hooks" / "scripts"))

import guard_irreversible  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=str(cwd), check=True, capture_output=True)


def hook(cwd, command):
    payload = {"tool_name": "Bash", "cwd": str(cwd), "tool_input": {"command": command}}
    return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(payload), capture_output=True,
                          text=True, encoding="utf-8", timeout=60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "main"
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def start_run(self):
        d = self.root / ".feature-ladder"
        d.mkdir(exist_ok=True)
        (d / "run.json").write_text(json.dumps({"active": True, "slug": "x", "environments": {}}), encoding="utf-8")
        return d / "run.json"


class FindRun(Base):
    def test_an_ordinary_checkout_without_a_run_costs_no_git_call(self):
        (self.root / "src").mkdir()
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("spawned a process")):
            self.assertEqual(guard_irreversible.find_run(self.root / "src"), (None, None))

    def test_a_linked_worktree_finds_the_main_checkouts_run(self):
        f = self.start_run()
        wt = self.root.parent / "wt"
        git(self.root, "worktree", "add", "-q", str(wt))
        root, found = guard_irreversible.find_run(wt)
        self.assertEqual((root, found), (self.root, f))


class Logging(Base):
    def test_a_corrupt_run_file_is_ignored_silently(self):
        f = self.start_run()
        f.write_text('{"active": true, "slug": "x', encoding="utf-8")
        r = hook(self.root, "firebase deploy --project prod")
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))

    def test_commands_logged_at_the_same_time_all_land_in_the_run_file(self):
        f = self.start_run()
        cmds = [f"firebase deploy --only functions:f{i} --project prod-app" for i in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda c: hook(self.root, c), cmds))
        run = json.loads(f.read_text(encoding="utf-8"))
        logged = {a["cmd"] for a in run.get("outside_actions", [])}
        self.assertEqual(len(logged), 8, logged)


if __name__ == "__main__":
    unittest.main()
