"""codex_review.py around the Codex call: timeout, the guard check, sign-in check, arguments.

The real Codex CLI is never run: every test uses a fake `codex` executable (a
.cmd shim on Windows, a shell script elsewhere) that runs a small Python file.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "cross-model-review" / "scripts"))

import codex_review  # noqa: E402

APPROVE = {"verdict": "approve", "summary": "fine", "findings": []}

# What the fake does is chosen per test through FAKE_CODEX_* environment variables.
FAKE = r'''
import json, os, sys, time
from pathlib import Path
a = sys.argv[1:]
if a[:2] == ["login", "status"]:
    print(os.environ.get("FAKE_CODEX_LOGIN", "Logged in using ChatGPT"))
    sys.exit(int(os.environ.get("FAKE_CODEX_LOGIN_RC", "0")))
root = Path(a[a.index("-C") + 1])
out = Path(a[a.index("-o") + 1])
sys.stdin.read()
for rel in filter(None, os.environ.get("FAKE_CODEX_WRITE", "").split(";")):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text("written by codex\n", encoding="utf-8")
for rel in filter(None, os.environ.get("FAKE_CODEX_DELETE", "").split(";")):
    (root / rel).unlink()
for rel in filter(None, os.environ.get("FAKE_CODEX_RESTORE", "").split(";")):
    import subprocess
    subprocess.run(["git", "checkout", "--", rel], cwd=str(root), check=True)
time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "0")))
out.write_text(os.environ.get("FAKE_CODEX_RESULT", "{}"), encoding="utf-8")
'''


def fake_exe(folder, name, body):
    """An executable named `name` that runs `body` with this Python."""
    folder = Path(folder)
    script = folder / f"{name}_impl.py"
    script.write_text(body, encoding="utf-8")
    if os.name == "nt":
        exe = folder / f"{name}.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = folder / name
        exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false", *args],
                   cwd=str(cwd), check=True, capture_output=True)


class Repo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.bin = base / "bin"
        self.bin.mkdir()
        self.root = base / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q")
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("y = 1\n", encoding="utf-8")
        git(self.root, "add", ".")
        git(self.root, "commit", "-q", "-m", "init")
        self.exe = fake_exe(self.bin, "codex", FAKE)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_CODEX_")}

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, argv, timeout=None, **fake):
        """Run codex_review.main in this repo against the fake codex. Returns (exit code, stdout, stderr)."""
        env = dict(self.env, **{f"FAKE_CODEX_{k.upper()}": str(v) for k, v in fake.items()})
        out, err = io.StringIO(), io.StringIO()
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*a, **k):
            made.append(real_mkdtemp(*a, **k))
            return made[-1]

        patches = [mock.patch.dict(os.environ, env, clear=True),
                   mock.patch.object(codex_review, "codex_bin", return_value=self.exe),
                   mock.patch.object(codex_review.os, "getcwd", return_value=str(self.root)),
                   mock.patch.object(codex_review.tempfile, "mkdtemp", side_effect=mkdtemp)]
        if timeout is not None:
            patches.append(mock.patch.object(codex_review, "TIMEOUT", timeout))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = codex_review.main(argv)
        self.tempdirs = made
        return code, out.getvalue(), err.getvalue()

    def record(self, slug, mode):
        return json.loads((self.root / ".feature-ladder" / "reviews" / f"{slug}-codex-{mode}.json").read_text(encoding="utf-8"))


class Timeout(Repo):
    def test_a_timeout_stops_the_whole_process_tree(self):
        marker = Path(self.tmp.name) / "late.txt"
        slow = fake_exe(self.bin, "slow", "import sys, time\ntime.sleep(3)\nopen(sys.argv[1], 'w').write('late')\n")
        started = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            codex_review.run([slow, str(marker)], str(self.root), timeout=0.5)
        self.assertLess(time.time() - started, 2.5, "the timeout did not return until the child finished")
        time.sleep(max(0, 3.6 - (time.time() - started)))
        self.assertFalse(marker.exists(), "the child kept running after the timeout")

    def test_a_timed_out_run_still_checks_the_guard_writes_the_record_and_cleans_up(self):
        code, out, _ = self.main(["adversarial", "--slug", "t1", "--uncommitted"], timeout=1,
                                 write="src/stray.py", sleep=30)
        self.assertEqual(code, 1)
        self.assertIn("timed out after 1s", out)
        self.assertIn("GUARD VIOLATION", out)
        self.assertIn("src/stray.py", out)
        rec = self.record("t1", "adversarial")
        self.assertTrue(rec["timed_out"])
        self.assertEqual(rec["files_changed_outside_allowed_dir"], ["src/stray.py"])
        self.assertTrue(self.tempdirs)
        self.assertFalse(any(Path(d).exists() for d in self.tempdirs), "temp dir leaked")


class Guard(Repo):
    def test_a_review_that_approves_is_recorded_with_no_violation(self):
        code, out, _ = self.main(["review", "--slug", "ok", "--uncommitted"], result=json.dumps(APPROVE))
        self.assertEqual(code, 0)
        self.assertNotIn("GUARD VIOLATION", out)
        self.assertEqual(self.record("ok", "review")["files_changed_outside_allowed_dir"], [])
        ignored = (self.root / ".feature-ladder" / ".gitignore").read_text(encoding="utf-8").splitlines()
        for entry in ("reviews/", "adversarial/", "run.json", "runs/"):
            self.assertIn(entry, ignored)

    def test_tests_written_inside_the_allowed_folder_are_not_a_violation(self):
        code, out, _ = self.main(["adversarial", "--slug", "adv", "--uncommitted"],
                                 write=".feature-ladder/adversarial/adv/test_x.py",
                                 result=json.dumps({"summary": "s", "test_command": "pytest", "tests": []}))
        self.assertEqual(code, 0)
        self.assertNotIn("GUARD VIOLATION", out)

    def test_reverting_uncommitted_work_is_a_violation(self):
        (self.root / "a.py").write_text("x = 2  # the user's uncommitted edit\n", encoding="utf-8")
        code, out, _ = self.main(["adversarial", "--slug", "rev", "--uncommitted"], restore="a.py",
                                 result=json.dumps({"summary": "s", "test_command": "pytest", "tests": []}))
        self.assertIn("GUARD VIOLATION", out)
        self.assertEqual(self.record("rev", "adversarial")["files_changed_outside_allowed_dir"], ["a.py"])

    def test_deleting_an_untracked_file_is_a_violation(self):
        (self.root / "notes.txt").write_text("mine\n", encoding="utf-8")
        _, out, _ = self.main(["adversarial", "--slug", "del", "--uncommitted"], delete="notes.txt",
                              result=json.dumps({"summary": "s", "test_command": "pytest", "tests": []}))
        self.assertIn("GUARD VIOLATION", out)
        self.assertEqual(self.record("del", "adversarial")["files_changed_outside_allowed_dir"], ["notes.txt"])

    def test_a_change_to_a_non_ascii_file_name_is_seen(self):
        name = "café notes.py"
        (self.root / name).write_text("a = 1\n", encoding="utf-8")
        before = codex_review.porcelain(self.root)
        self.assertIn(name, before)
        (self.root / name).write_text("a = 2\n", encoding="utf-8")
        self.assertEqual(codex_review.changed_paths(before, codex_review.porcelain(self.root)), [name])

    def test_a_rename_is_reported_under_its_new_name(self):
        git(self.root, "mv", "b.py", "c.py")
        self.assertEqual(sorted(codex_review.porcelain(self.root)), ["c.py"])


class SignIn(Repo):
    def check(self, text, rc=0):
        env = dict(self.env, FAKE_CODEX_LOGIN=text, FAKE_CODEX_LOGIN_RC=str(rc))
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(codex_review, "codex_bin", return_value=self.exe):
            return codex_review.check()

    def test_logged_in_passes(self):
        self.assertTrue(self.check("Logged in using ChatGPT")[0])

    def test_not_logged_in_fails_even_with_exit_zero(self):
        ok, msg = self.check("Not logged in")
        self.assertFalse(ok)
        self.assertIn("codex login", msg)

    def test_a_failing_status_command_fails(self):
        self.assertFalse(self.check("Logged in using ChatGPT", rc=1)[0])


class Arguments(Repo):
    def test_a_flag_without_its_value_is_a_one_line_error(self):
        for argv in (["review", "--slug"], ["review", "--base", "--uncommitted"], ["review", "--flags"]):
            with self.subTest(argv=argv):
                code, out, err = self.main(argv)
                self.assertEqual(code, 1)
                self.assertEqual(len((out + err).strip().splitlines()), 1, out + err)
                self.assertIn("needs a value", out + err)

    def test_effort_is_high_only_for_a_high_tier_or_a_risk_flag(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(codex_review.effort_for("review", "T3", []), "medium")
            self.assertEqual(codex_review.effort_for("review", "", []), "medium")
            self.assertEqual(codex_review.effort_for("review", "T4 senior", []), "high")
            for flag in ("security", "persistence", "concurrency", "cross_system", "public_api", "irreversible"):
                self.assertEqual(codex_review.effort_for("review", "", [flag]), "high", flag)

    def write_intake(self, slug, confirmed, **fields):
        d = self.root / ".feature-ladder" / "intake"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{slug}.json").write_text(json.dumps({"slug": slug, "confirmed_at": confirmed, "fields": fields}),
                                        encoding="utf-8")

    def test_flags_auto_collects_the_risk_flags_of_this_sessions_intakes(self):
        (self.root / ".feature-ladder").mkdir()
        (self.root / ".feature-ladder" / "session.json").write_text(
            json.dumps({"started": "2026-11-15T10:00:00+00:00"}), encoding="utf-8")
        self.write_intake("old", "2026-11-01T09:00:00+00:00", concurrency=True)
        self.write_intake("one", "2026-11-15T10:05:00+00:00", security=True, persistence=False, no_tests=True)
        self.write_intake("two", "2026-11-15T11:00:00+00:00", public_api=True)
        (self.root / ".feature-ladder" / "intake" / "one-scan.json").write_text('{"fields": {"irreversible": true}}')
        self.assertEqual(codex_review.session_flags(self.root), ["public_api", "security"])
        code, out, _ = self.main(["review", "--slug", "auto", "--uncommitted", "--flags", "auto"],
                                 result=json.dumps(APPROVE))
        self.assertEqual(code, 0)
        self.assertEqual(self.record("auto", "review")["effort"], "high")

    def test_flags_auto_with_no_session_intake_stays_medium(self):
        code, _, _ = self.main(["review", "--slug", "plain", "--uncommitted", "--flags", "auto"],
                               result=json.dumps(APPROVE))
        self.assertEqual(code, 0)
        self.assertEqual(self.record("plain", "review")["effort"], "medium")


if __name__ == "__main__":
    unittest.main()
