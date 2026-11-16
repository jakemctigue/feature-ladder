"""Tests for with_secrets.py: the command gets the secret in its environment, the
transcript never does, and failures are reported as what they are.

gcloud is a fake on PATH (a .cmd shim on Windows, as the real one is); nothing
here talks to Google.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
SCRIPT = SCRIPTS / "with_secrets.py"
sys.path.insert(0, str(SCRIPTS))

import verify  # noqa: E402
import with_secrets  # noqa: E402

SECRET = "FAKESECRETVALUE1234"
HAS_SHELL = sys.platform != "win32" or bool(verify.find_bash())

FAKE_GCLOUD = r'''
import os, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_GCLOUD_MODE", "ok")
if args[:2] == ["config", "get-value"]:
    print("(unset)")
    sys.exit(0)
name = args[args.index("--secret") + 1] if "--secret" in args else "?"
if mode == "hang":
    time.sleep(30)
if mode == "notfound":
    sys.stderr.write("ERROR: (gcloud.secrets.versions.access) NOT_FOUND: Secret [projects/1/secrets/%s] "
                     "not found or has no versions.\n" % name)
    sys.exit(1)
if mode == "signedout":
    sys.stderr.write("ERROR: (gcloud.secrets.versions.access) You do not currently have an active account "
                     "selected.\nPlease run:\n\n  $ gcloud auth login\n\nto obtain new credentials.\n")
    sys.exit(1)
sys.stdout.write(os.environ.get("FAKE_SECRET_VALUE", "FAKESECRETVALUE1234") + "\n")
'''


def shim(bindir, name, script):
    """An executable called `name` that runs `script` with this interpreter."""
    if sys.platform == "win32":
        (bindir / f"{name}.cmd").write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        f = bindir / name
        f.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        f.chmod(0o755)


class Base(unittest.TestCase):
    def setUp(self):
        self.bindir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.bindir, ignore_errors=True)
        (self.bindir / "fake_gcloud.py").write_text(FAKE_GCLOUD, encoding="utf-8")
        shim(self.bindir, "gcloud", self.bindir / "fake_gcloud.py")

    def env(self, **extra):
        env = dict(os.environ, PATH=str(self.bindir) + os.pathsep + os.environ.get("PATH", ""),
                   GOOGLE_CLOUD_PROJECT="demo-project")
        env.pop("FEATURE_LADDER_CHECK_SHELL", None)
        env.update(extra)
        return env

    def run_cli(self, *args, timeout=60, **extra):
        r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, env=self.env(**extra),
                           timeout=timeout, cwd=str(self.bindir))
        return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


class RunsTheCommand(Base):
    def test_the_secret_is_in_the_environment_and_masked_in_the_output(self):
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", sys.executable, "-c",
                                      "import os; print('got', os.environ['MY_KEY'])")
        self.assertEqual(code, 0, err)
        self.assertIn("got ***", out)
        self.assertNotIn(SECRET, out + err)

    def test_a_program_installed_as_a_cmd_shim_runs(self):
        tool = self.bindir / "tool.py"
        tool.write_text("import sys; print('tool ran', *sys.argv[1:])", encoding="utf-8")
        shim(self.bindir, "mytool", tool)
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", "mytool", "--version")
        self.assertEqual(code, 0, err)
        self.assertIn("tool ran --version", out)

    def test_a_program_that_does_not_exist_is_exit_127_not_a_traceback(self):
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", "no-such-program-xyz", "--version")
        self.assertEqual(code, 127)
        self.assertNotIn("Traceback", err)
        self.assertIn("no-such-program-xyz", err)

    @unittest.skipUnless(HAS_SHELL, "needs a POSIX shell (Git Bash on Windows)")
    def test_one_string_is_shell_text_and_expands_the_secret(self):
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", "echo key is $MY_KEY")
        self.assertEqual(code, 0, err)
        self.assertIn("key is ***", out)

    @unittest.skipUnless(HAS_SHELL, "needs a POSIX shell (Git Bash on Windows)")
    def test_bash_c_puts_the_secret_into_an_argument(self):
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", "bash", "-c", 'echo "Bearer $MY_KEY"')
        self.assertEqual(code, 0, err)
        self.assertIn("Bearer ***", out)

    def test_output_outside_cp1252_does_not_turn_success_into_failure(self):
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", sys.executable, "-c",
                                      "import sys; sys.stdout.buffer.write('\\u2714 Deploy complete\\n'.encode('utf-8'))",
                                      PYTHONIOENCODING="cp1252", PYTHONUTF8="0")
        self.assertEqual(code, 0, err)
        self.assertIn("✔ Deploy complete", out)

    def test_the_commands_exit_code_is_returned(self):
        code, _, _ = self.run_cli("--secret", "MY_KEY", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(code, 7)

    def test_output_arrives_while_the_command_is_still_running(self):
        p = subprocess.Popen([sys.executable, str(SCRIPT), "--secret", "MY_KEY", "--", sys.executable, "-u", "-c",
                              "import time; print('first'); time.sleep(4); print('second')"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env(), cwd=str(self.bindir))
        try:
            t0 = time.time()
            first = p.stdout.readline().decode("utf-8", "replace").strip()
            waited = time.time() - t0
            self.assertEqual(first, "first")
            self.assertLess(waited, 3.5, "the first line only arrived when the command had finished")
        finally:
            p.communicate(timeout=30)

    def test_a_command_timeout_stops_it(self):
        t0 = time.time()
        code, _, err = self.run_cli("--secret", "MY_KEY", "--timeout", "1", "--", sys.executable, "-c",
                                    "import time; time.sleep(20)")
        self.assertEqual(code, 124, err)
        self.assertLess(time.time() - t0, 10)


class Masking(Base):
    def test_each_line_of_a_multi_line_secret_is_masked(self):
        value = "-----BEGIN KEY-----\nMIIEvQIBADANBgkqhkiG9w0\n-----END KEY-----"
        code, out, err = self.run_cli("--secret", "PEM", "--", sys.executable, "-c",
                                      "import os; print('line:', os.environ['PEM'].splitlines()[1])",
                                      FAKE_SECRET_VALUE=value)
        self.assertEqual(code, 0, err)
        self.assertNotIn("MIIEvQIBADANBgkqhkiG9w0", out)
        self.assertIn("line: ***", out)

    def test_common_encodings_of_the_value_are_masked(self):
        code, out, err = self.run_cli("--secret", "MY_KEY", "--", sys.executable, "-c",
                                      "import os, base64; print(base64.b64encode(os.environ['MY_KEY'].encode()).decode())")
        self.assertEqual(code, 0, err)
        self.assertNotIn(base64.b64encode(SECRET.encode()).decode(), out)

    def test_mask_helper_keeps_ordinary_text(self):
        self.assertEqual(with_secrets.mask("token abcd1234 ok", ["abcd1234"]), "token *** ok")
        self.assertEqual(with_secrets.mask("a ab abc", ["abc"]), "a ab abc")  # under 4 characters: not a secret


class GcloudFailures(Base):
    def test_a_secret_whose_name_contains_auth_is_unreadable_not_signed_out(self):
        code, out, _ = self.run_cli("check", "OAUTH_CLIENT_SECRET", "STRIPE_KEY", FAKE_GCLOUD_MODE="notfound")
        self.assertEqual(code, 1, out)
        self.assertIn("OAUTH_CLIENT_SECRET: UNREADABLE", out)
        self.assertIn("STRIPE_KEY: UNREADABLE", out)
        self.assertNotIn("NOT SIGNED IN", out)

    def test_a_missing_secret_named_credentials_is_exit_1(self):
        code, _, err = self.run_cli("--secret", "DB_CREDENTIALS", "--", sys.executable, "-c", "pass",
                                    FAKE_GCLOUD_MODE="notfound")
        self.assertEqual(code, 1, err)

    def test_signed_out_is_exit_3(self):
        code, out, _ = self.run_cli("check", "MY_KEY", FAKE_GCLOUD_MODE="signedout")
        self.assertEqual(code, 3, out)
        self.assertIn("NOT SIGNED IN", out)
        code, _, _ = self.run_cli("--secret", "MY_KEY", "--", sys.executable, "-c", "pass", FAKE_GCLOUD_MODE="signedout")
        self.assertEqual(code, 3)

    def test_a_gcloud_that_never_answers_is_given_up_on(self):
        t0 = time.time()
        code, out, _ = self.run_cli("check", "MY_KEY", FAKE_GCLOUD_MODE="hang", FEATURE_LADDER_GCLOUD_TIMEOUT="2")
        self.assertEqual(code, 1, out)
        self.assertIn("did not answer", out)
        self.assertLess(time.time() - t0, 15)

    def test_gcloud_gets_no_stdin_to_wait_on(self):
        seen = {}

        def fake_run_tree(args, **kw):
            seen.update(kw)
            return 0, "value\n", ""

        orig = with_secrets.run_tree
        with_secrets.run_tree = fake_run_tree
        try:
            self.assertEqual(with_secrets.fetch("gcloud", "X", None), ("value", None))
        finally:
            with_secrets.run_tree = orig
        self.assertIsNone(seen.get("input"))  # run_tree gives the child DEVNULL when there is no input
        self.assertTrue(seen.get("timeout"))


if __name__ == "__main__":
    unittest.main()
