"""Tests for verify.py, the token-free acceptance check runner.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "skills" / "orchestration-ladder" / "scripts" / "verify.py"
sys.path.insert(0, str(SCRIPT.parent))

import verify  # noqa: E402

# Checks are shell text: forward slashes and quotes so the same string runs under
# Git Bash (Windows), cmd.exe (the fallback) and /bin/sh.
PY = '"' + Path(sys.executable).as_posix() + '"'


def git(root, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=str(root),
                   check=True, capture_output=True)


def make_repo(tmp):
    root = Path(tmp)
    git(root, "init", "-q")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "a.py")
    git(root, "commit", "-q", "-m", "init")
    return root


def run(root, *args, env=None):
    r = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", timeout=120, env=dict(os.environ, **env) if env else None)
    return r.returncode, r.stdout, r.stderr


def evidence(root, task):
    return json.loads((root / ".feature-ladder" / "evidence" / f"{task}.json").read_text(encoding="utf-8"))


class Checks(unittest.TestCase):
    def test_all_green_in_scope_and_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "a.py").write_text("x = 2\n", encoding="utf-8")
            code, out, err = run(root, "run", "--id", "T1", "--check", f"{PY} -c \"import a\"",
                                 "--check", f"{PY} -c \"print('two lines'); print('ok')\"",
                                 "--scope", "a.py", "--changed", "a.py")
            self.assertEqual(code, 0, out + err)
            self.assertIn("TASK T1 - passed - checks 2/2 passed", out)
            e = evidence(root, "T1")
            self.assertEqual([c["exit"] for c in e["checks"]], [0, 0])
            self.assertEqual(e["checks"][1]["tail"], ["two lines", "ok"])
            self.assertEqual(e["changed"], ["a.py"])
            self.assertEqual(e["verdict"], "passed")
            self.assertIn("evidence/", (root / ".feature-ladder" / ".gitignore").read_text(encoding="utf-8"))

    def test_failing_check_exits_1_with_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "run", "--id", "T2", "--check", f"{PY} -c \"print('boom'); raise SystemExit(3)\"")
            self.assertEqual(code, 1)
            self.assertIn("-> 3", out)
            self.assertIn("| boom", out)
            self.assertEqual(evidence(root, "T2")["verdict"], "failed")

    def test_out_of_scope_change_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "a.py").write_text("x = 2\n", encoding="utf-8")
            (root / "stray.txt").write_text("oops\n", encoding="utf-8")
            code, out, _ = run(root, "run", "--id", "T3", "--check", "true" if sys.platform != "win32" else "cd .",
                               "--scope", "a.py")
            self.assertEqual(code, 2, out)
            self.assertIn("outside scope: stray.txt", out)
            self.assertEqual(evidence(root, "T3")["outside_scope"], ["stray.txt"])

    def test_claim_mismatch_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "a.py").write_text("x = 2\n", encoding="utf-8")
            code, out, _ = run(root, "run", "--id", "T4", "--check", f"{PY} -c pass",
                               "--scope", "a.py", "--scope", "tests/", "--changed", "tests/test_a.py")
            self.assertEqual(code, 2, out)
            self.assertIn("claimed but unchanged: tests/test_a.py", out)
            self.assertIn("changed but not claimed: a.py", out)

    def test_directory_and_glob_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("", encoding="utf-8")
            (root / "b.sql").write_text("", encoding="utf-8")
            code, out, _ = run(root, "run", "--id", "T5", "--check", f"{PY} -c pass", "--scope", "tests/", "--scope", "*.sql")
            self.assertEqual(code, 0, out)

    def test_cache_noise_is_not_a_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "a.pyc").write_bytes(b"\x00")
            code, out, _ = run(root, "run", "--id", "T6", "--check", f"{PY} -c pass", "--scope", "a.py")
            self.assertEqual(code, 0, out)

    def test_secret_exposure_is_refused_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "run", "--id", "T7", "--check", "cat .env")
            self.assertEqual(code, 1)
            self.assertIn("refused", out)
            self.assertEqual(evidence(root, "T7")["checks"][0]["status"], "refused")

    def test_outside_repo_check_runs_and_is_logged_for_review(self):
        # Since v1.11 the former never-list is not a gate: the check runs (here it
        # fails, there is no remote) and run.json records it with review: pending.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".feature-ladder").mkdir()
            (root / ".feature-ladder" / "run.json").write_text(json.dumps({"active": True, "base": "main"}), encoding="utf-8")
            code, out, _ = run(root, "run", "--id", "T7b", "--check", "git push --force origin main")
            self.assertEqual(code, 1)
            self.assertNotIn("refused", out)
            self.assertEqual(evidence(root, "T7b")["checks"][0]["status"], "failed")
            logged = json.loads((root / ".feature-ladder" / "run.json").read_text(encoding="utf-8"))["outside_actions"]
            self.assertEqual((logged[0]["tier"], logged[0]["review"]), ("never", "pending"))

    def test_timeout_stops_the_command_and_its_children(self):
        # The check is shell text, so the interpreter is a grandchild of verify.py:
        # killing only the shell used to leave it running and verify waiting on its pipes.
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            marker = (Path(tmp) / "still-running.txt").as_posix()
            code_text = f"import time; time.sleep(4); open(r'{marker}', 'w').write('x')"
            t0 = time.time()
            code, out, _ = run(root, "run", "--id", "T8", "--timeout", "1", "--check", f'{PY} -c "{code_text}"')
            elapsed = time.time() - t0
            self.assertEqual(code, 1)
            self.assertIn("timeout", out)
            self.assertLess(elapsed, 3.5, f"--timeout 1 took {elapsed:.1f}s")
            time.sleep(max(0.0, 5.5 - (time.time() - t0)))
            self.assertFalse(Path(marker).exists(), "the timed-out command kept running")

    def test_total_budget_stops_before_later_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            marker = (Path(tmp) / "second-ran.txt").as_posix()
            code, out, _ = run(root, "run", "--id", "T8b", "--timeout", "30", "--total-timeout", "1",
                               "--check", f'{PY} -c "import time; time.sleep(10)"',
                               "--check", f"{PY} -c \"open(r'{marker}', 'w').write('x')\"")
            self.assertEqual(code, 1, out)
            e = evidence(root, "T8b")
            self.assertEqual([c["status"] for c in e["checks"]], ["timeout", "timeout"])
            self.assertIn("budget", e["checks"][1]["reason"])
            self.assertFalse(Path(marker).exists())

    def test_card_file_and_show(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            card = root / "card.json"
            card.write_text(json.dumps({"id": "T9", "checks": [f"{PY} -c pass"], "scope": ["a.py", "card.json"]}),
                            encoding="utf-8")
            code, out, _ = run(root, "run", "--card", str(card))
            self.assertEqual(code, 0, out)
            code, out, _ = run(root, "show", "--id", "T9")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["task"], "T9")

    def test_no_checks_is_an_argument_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, _, err = run(root, "run", "--id", "T10")
            self.assertEqual(code, 3)
            self.assertIn("no checks", err)

    def test_outside_git_skips_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(Path(tmp), "run", "--id", "T11", "--check", f"{PY} -c pass", "--scope", "x")
            self.assertEqual(code, 0, out)
            self.assertIn("not a git repo", out)


class Worktrees(unittest.TestCase):
    def test_evidence_lands_in_the_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            wt = Path(tmp) / "wt"
            git(root, "worktree", "add", "-q", str(wt), "-b", "task")
            (wt / "a.py").write_text("x = 3\n", encoding="utf-8")
            code, out, err = run(wt, "run", "--id", "W1", "--check", f"{PY} -c pass", "--scope", "a.py", "--changed", "a.py")
            self.assertEqual(code, 0, out + err)
            self.assertTrue((root / ".feature-ladder" / "evidence" / "W1.json").is_file())
            self.assertFalse((wt / ".feature-ladder").exists())


class DocumentedFlow(unittest.TestCase):
    """The coordinator's own files and ordinary path spellings must not bounce a packet."""

    def test_report_files_at_the_root_are_not_a_scope_or_claim_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "a.py").write_text("x = 2\n", encoding="utf-8")
            (root / "FEATURE-REPORT.md").write_text("# report\n", encoding="utf-8")
            (root / "DESIGN.md").write_text("# design\n", encoding="utf-8")
            code, out, err = run(root, "run", "--id", "D1", "--check", f"{PY} -c pass",
                                 "--scope", "a.py", "--changed", "a.py")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(evidence(root, "D1")["changed"], ["a.py"])

    def test_a_report_file_that_is_the_deliverable_still_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "DESIGN.md").write_text("# design\n", encoding="utf-8")
            code, out, err = run(root, "run", "--id", "D2", "--check", f"{PY} -c pass",
                                 "--scope", "DESIGN.md", "--changed", "DESIGN.md")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(evidence(root, "D2")["changed"], ["DESIGN.md"])

    def test_non_ascii_file_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "docs").mkdir()
            (root / "docs" / "café notes.md").write_text("x\n", encoding="utf-8")
            code, out, err = run(root, "run", "--id", "D3", "--check", f"{PY} -c pass",
                                 "--scope", "docs/café notes.md", "--changed", "docs/café notes.md")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(evidence(root, "D3")["changed"], ["docs/café notes.md"])

    def test_dot_slash_and_backslash_spellings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "src").mkdir()
            (root / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
            code, out, err = run(root, "run", "--id", "D4", "--check", f"{PY} -c pass",
                                 "--scope", "./src/x.py", "--changed", ".\\src\\x.py")
            self.assertEqual(code, 0, out + err)

    def test_a_rename_reports_the_new_path_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            git(root, "mv", "a.py", "b.py")
            code, out, err = run(root, "run", "--id", "D5", "--check", f"{PY} -c pass", "--scope", "b.py")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(evidence(root, "D5")["changed"], ["b.py"])


@unittest.skipUnless(sys.platform != "win32" or getattr(verify, "find_bash", lambda: None)(),
                     "needs a POSIX shell (Git Bash on Windows)")
class PosixChecks(unittest.TestCase):
    """Workers write "Done when" commands in the bash they run in; verify must run them the same way."""

    def test_env_prefix_and_negation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, err = run(root, "run", "--id", "P1", "--check", "CI=1 test -f a.py && ! grep -q TODO a.py")
            self.assertEqual(code, 0, out + err)

    def test_cmd_can_be_forced(self):
        if sys.platform != "win32":
            self.skipTest("cmd.exe only exists on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "run", "--id", "P2", "--check", "CI=1 test -f a.py",
                               env={"FEATURE_LADDER_CHECK_SHELL": "cmd"})
            self.assertEqual(code, 1, out)


class OutsideLog(unittest.TestCase):
    def test_a_corrupt_run_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".feature-ladder").mkdir()
            bad = '{"active": true, "outside_actions": [{"cmd": "x"'
            (root / ".feature-ladder" / "run.json").write_text(bad, encoding="utf-8")
            verify._log_outside(str(root), "git push --force origin main", {"tier": "never"})
            self.assertEqual((root / ".feature-ladder" / "run.json").read_text(encoding="utf-8"), bad)
            self.assertEqual([f.name for f in (root / ".feature-ladder").iterdir()], ["run.json"])


if __name__ == "__main__":
    unittest.main()
