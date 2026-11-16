"""Repo-level guards: duplicated files stay identical, secrets files are ignored,
shell scripts keep LF endings, CI has a time limit, and the README names real files.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import re
import subprocess
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
IN_GIT = (REPO / ".git").exists()


def git(*args):
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=30)


class Copies(unittest.TestCase):
    """The plugin folder is what gets installed, so it carries its own README
    and LICENSE; GitHub shows the ones at the repo root. They are full copies."""

    def test_readme_copies_are_identical(self):
        self.assertEqual((REPO / "README.md").read_bytes().replace(b"\r\n", b"\n"),
                         (PLUGIN / "README.md").read_bytes().replace(b"\r\n", b"\n"),
                         "README.md and plugins/feature-ladder/README.md differ: copy one over the other")

    def test_license_copies_are_identical(self):
        self.assertEqual((REPO / "LICENSE").read_bytes().replace(b"\r\n", b"\n"),
                         (PLUGIN / "LICENSE").read_bytes().replace(b"\r\n", b"\n"))


@unittest.skipUnless(IN_GIT, "not a git checkout")
class GitRules(unittest.TestCase):
    def test_the_hindsight_key_file_is_ignored_where_the_readme_says_to_create_it(self):
        for path in ("plugins/feature-ladder/hindsight/hindsight.env", "hindsight/hindsight.env"):
            with self.subTest(path=path):
                self.assertEqual(git("check-ignore", "-q", path).returncode, 0, f"{path} is not ignored")

    def test_the_example_env_file_is_not_ignored(self):
        self.assertEqual(git("check-ignore", "-q", "plugins/feature-ladder/hindsight/hindsight.env.example").returncode, 1)

    def test_shell_scripts_are_checked_out_with_lf(self):
        scripts = [p.relative_to(REPO).as_posix() for p in PLUGIN.rglob("*.sh")]
        self.assertTrue(scripts)
        for rel in scripts:
            with self.subTest(script=rel):
                self.assertIn("eol: lf", git("check-attr", "eol", "--", rel).stdout)


class Workflow(unittest.TestCase):
    def setUp(self):
        self.text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

    def test_jobs_have_a_time_limit(self):
        self.assertRegex(self.text, r"(?m)^\s+timeout-minutes:\s*\d+")

    def test_a_newer_push_cancels_the_run_it_supersedes(self):
        self.assertRegex(self.text, r"(?m)^concurrency:")
        self.assertIn("cancel-in-progress: true", self.text)


class ReadmeFiles(unittest.TestCase):
    def test_the_self_host_steps_name_files_that_exist(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        m = re.search(r"To self-host.*?\n\n", readme, re.S)
        self.assertIsNotNone(m)
        named = re.findall(r"[\w./-]+\.(?:sh|ps1|py|example)\b", m.group(0))
        self.assertTrue(named)
        for name in named:
            with self.subTest(file=name):
                self.assertTrue((PLUGIN / "hindsight" / Path(name).name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
