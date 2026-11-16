"""hooks/scripts/py.sh: the launcher every hook goes through.

It must start Python once (not probe-then-run), skip the Windows Store stub,
and make the hook's stdout UTF-8. Skipped when no bash is available.
(The snippets avoid quote characters: MSYS bash re-parses its Windows command
line and would strip single quotes before py.sh ever saw them.)

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
PY_SH = PLUGIN / "hooks" / "scripts" / "py.sh"


def find_bash():
    if os.name == "nt":  # C:\Windows\System32\bash.exe is WSL, not what Claude Code runs hooks with
        git = shutil.which("git")
        if git:
            for rel in ("../bin/bash.exe", "../../bin/bash.exe", "../usr/bin/bash.exe", "../../usr/bin/bash.exe"):
                cand = (Path(git).parent / rel).resolve()
                if cand.is_file():
                    return str(cand)
        return None
    return shutil.which("bash")


BASH = find_bash()


def posix(path):
    """A path bash on this machine understands (C:\\x -> /c/x under Git Bash)."""
    p = str(path).replace("\\", "/")
    if os.name == "nt" and len(p) > 1 and p[1] == ":":
        p = "/" + p[0].lower() + p[2:]
    return p


@unittest.skipUnless(BASH, "no bash on this machine")
class Launcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.log = self.dir / "starts.log"

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, name, where=None, works=True):
        """A fake interpreter that logs each start and then runs the real Python (or fails like the Store stub)."""
        d = where or self.bin
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        body = f'#!/bin/sh\necho "{name} $*" >> "{posix(self.log)}"\n'
        body += f'exec "{posix(sys.executable)}" "$@"\n' if works else "exit 49\n"
        f.write_text(body, encoding="utf-8", newline="\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
        return d

    def run_sh(self, path_dirs, *args, stdin=""):
        env = dict(os.environ, TMPDIR=posix(self.dir), TMP=str(self.dir), TEMP=str(self.dir))
        env.pop("PYTHONUTF8", None)
        # PATH holds only our fake interpreters: py.sh needs nothing but shell builtins,
        # and a real /usr/bin/python3 must not be found instead of the fakes.
        script = f'PATH="{":".join(posix(d) for d in path_dirs)}"; export PATH; . "{posix(PY_SH)}"'
        return subprocess.run([BASH, "-c", script + ' "$@"', "py.sh", *args], input=stdin, env=env,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)

    def starts(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.is_file() else []

    def test_python_is_started_once_per_hook_call(self):
        self.fake("python3")
        r = self.run_sh([self.bin], "-c", "print(40+2)")
        self.assertEqual(r.stdout.strip(), "42", r.stderr)
        self.assertEqual(len(self.starts()), 1, self.starts())

    def test_stdin_and_arguments_reach_the_script(self):
        self.fake("python3")
        r = self.run_sh([self.bin], "-c", "import sys; print(sys.stdin.read().strip(), sys.argv[1])", "arg one",
                        stdin="payload")
        self.assertEqual(r.stdout.strip(), "payload arg one", r.stderr)

    def test_a_store_stub_is_skipped_and_the_working_interpreter_is_remembered(self):
        stub = self.fake("python3", where=self.dir / "WindowsApps", works=False)
        self.fake("python")
        r = self.run_sh([stub, self.bin], "-c", "print(40+2)")
        self.assertEqual(r.stdout.strip(), "42", r.stderr)
        self.assertTrue(any(s.startswith("python ") and "print" in s for s in self.starts()), self.starts())
        self.log.unlink()
        r = self.run_sh([stub, self.bin], "-c", "print(50+5)")
        self.assertEqual(r.stdout.strip(), "55", r.stderr)
        self.assertEqual(len(self.starts()), 1, self.starts())  # no probing the second time

    def test_with_only_store_candidates_the_probe_runs_once_and_is_cached(self):
        store = self.dir / "WindowsApps"
        self.fake("python3", where=store, works=False)   # the stub
        self.fake("python", where=store, works=True)     # a real Store install
        r = self.run_sh([store], "-c", "print(40+2)")
        self.assertEqual(r.stdout.strip(), "42", r.stderr)
        self.assertEqual([s.split()[0] for s in self.starts()], ["python3", "python", "python"])  # probe, probe, run
        self.log.unlink()
        r = self.run_sh([store], "-c", "print(50+5)")
        self.assertEqual(r.stdout.strip(), "55", r.stderr)
        self.assertEqual([s.split()[0] for s in self.starts()], ["python"])

    def test_a_real_interpreter_outside_the_store_folder_is_preferred(self):
        store = self.fake("python3", where=self.dir / "WindowsApps", works=True)
        self.fake("python")
        self.run_sh([store, self.bin], "-c", "pass")
        self.assertTrue(all(s.startswith("python ") for s in self.starts()), self.starts())

    def test_hook_output_is_utf8_whatever_the_console_code_page(self):
        self.fake("python3")
        r = self.run_sh([self.bin], "-c", "print('\\u2192 ok')")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("\u2192 ok", r.stdout)

    def test_no_interpreter_exits_zero_with_a_message(self):
        empty = self.dir / "empty"
        empty.mkdir()
        r = self.run_sh([empty], "x.py")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no working python", r.stderr)


if __name__ == "__main__":
    unittest.main()
