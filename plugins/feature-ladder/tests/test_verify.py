"""Tests for verify.py, the token-free acceptance check runner.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "skills" / "orchestration-ladder" / "scripts" / "verify.py"
PY = sys.executable


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


def run(root, *args):
    r = subprocess.run([PY, str(SCRIPT), *args], cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
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

    def test_never_list_command_is_refused_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "run", "--id", "T7", "--check", "git push --force origin main")
            self.assertEqual(code, 1)
            self.assertIn("refused", out)
            self.assertEqual(evidence(root, "T7")["checks"][0]["status"], "refused")

    def test_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "run", "--id", "T8", "--timeout", "1",
                               "--check", f"{PY} -c \"import time; time.sleep(5)\"")
            self.assertEqual(code, 1)
            self.assertIn("timeout", out)

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


if __name__ == "__main__":
    unittest.main()
