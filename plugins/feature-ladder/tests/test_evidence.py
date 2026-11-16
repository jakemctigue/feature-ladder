"""The checked-in SBOM and SARIF in evidence/ must be the exact output of the
generator (so they cannot go stale), have the CycloneDX 1.5 / SARIF 2.1.0 shape
and carry nothing machine-specific.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
GEN = PLUGIN / "scripts" / "evidence" / "generate_evidence.py"
sys.path.insert(0, str(GEN.parent))

import generate_evidence as g  # noqa: E402


def lf(path):
    return Path(path).read_bytes().decode("utf-8").replace("\r\n", "\n")


class Evidence(unittest.TestCase):
    def test_checked_in_files_equal_regeneration(self):
        for name, text in g.build().items():
            with self.subTest(file=name):
                self.assertEqual(lf(REPO / "evidence" / name), text)

    def test_generation_is_repeatable(self):
        self.assertEqual(g.build(), g.build())

    def check(self, out_dir=None):
        """Exit code and output of `generate_evidence.py --check`, in process."""
        buf = io.StringIO()
        with contextlib.ExitStack() as stack:
            if out_dir is not None:
                stack.enter_context(mock.patch.object(g, "OUT", Path(out_dir)))
            stack.enter_context(contextlib.redirect_stdout(buf))
            code = g.main(["--check"])
        return code, buf.getvalue()

    def test_check_mode_exits_zero_when_current(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_check_mode_exits_one_and_names_the_stale_or_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in g.build().items():
                (Path(tmp) / name).write_bytes(text.encode("utf-8"))
            (Path(tmp) / g.SBOM).write_text("{}\n", encoding="utf-8")
            code, out = self.check(tmp)
            self.assertEqual(code, 1)
            self.assertIn(g.SBOM, out)
            self.assertNotIn(g.SARIF, out)
            (Path(tmp) / g.SARIF).unlink()
            self.assertIn(g.SARIF, self.check(tmp)[1])

    def test_sbom_shape(self):
        sbom = json.loads(lf(REPO / "evidence" / g.SBOM))
        self.assertEqual((sbom["bomFormat"], sbom["specVersion"], sbom["version"]), ("CycloneDX", "1.5", 1))
        comp = sbom["metadata"]["component"]
        plugin = json.loads(lf(PLUGIN / ".claude-plugin" / "plugin.json"))
        self.assertEqual((comp["name"], comp["version"]), (plugin["name"], plugin["version"]))
        names = [c["name"] for c in sbom["components"]]
        self.assertEqual(names, sorted(names))
        self.assertTrue(names)
        for c in sbom["components"]:
            self.assertEqual(c["type"], "file")
            self.assertRegex(c["hashes"][0]["content"], r"^[0-9a-f]{64}$")
            self.assertFalse(c["name"].startswith("/") or ":" in c["name"] or "\\" in c["name"])
        props = {p["name"]: p["value"] for p in sbom["properties"]}
        self.assertEqual(props["feature-ladder:third-party-dependencies"], "0", props)
        self.assertNotIn("serialNumber", sbom)
        self.assertNotIn("timestamp", sbom["metadata"])

    def test_hash_ignores_line_endings(self):
        rel = "plugins/feature-ladder/scripts/evidence/ci_log_classifier.py"
        crlf = (REPO / rel).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        import hashlib
        self.assertEqual(hashlib.sha256(crlf.decode().replace("\r\n", "\n").encode()).hexdigest(), g.sha256(rel))

    def test_sarif_shape(self):
        sarif = json.loads(lf(REPO / "evidence" / g.SARIF))
        self.assertEqual(sarif["version"], "2.1.0")
        run = sarif["runs"][0]
        rule_ids = {r["id"] for r in run["tool"]["driver"]["rules"]}
        self.assertEqual(rule_ids, {r[0] for r in g.RULES})
        for res in run["results"]:
            self.assertIn(res["ruleId"], rule_ids | {"FL000"})
            self.assertIn(res["level"], {"note", "warning", "error"})
            loc = res["locations"][0]["physicalLocation"]
            self.assertFalse(loc["artifactLocation"]["uri"].startswith("/"))
            self.assertGreaterEqual(loc["region"]["startLine"], 1)

    def test_no_absolute_paths_or_timestamps_in_outputs(self):
        for name in (g.SBOM, g.SARIF):
            text = lf(REPO / "evidence" / name)
            self.assertNotRegex(text, r"[A-Za-z]:\\\\|/home/|/Users/|\d{4}-\d\d-\d\dT\d\d:")
            self.assertNotIn(str(REPO).replace("\\", "/"), text)

    def test_scanner_flags_known_bad_patterns(self):
        import tempfile
        bad = ("import subprocess, os\nsubprocess.run('x', shell=True)\neval('1')\nos.system('x')\n"
               "api_token = 'ghp_" + "a" * 30 + "'\nok = subprocess.run(['x'])\n")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.py").write_text(bad, encoding="utf-8")
            old = g.REPO
            g.REPO = Path(tmp)
            try:
                found = sorted(g.scan_file("bad.py"))
            finally:
                g.REPO = old
        self.assertEqual([r for r, _ in found], ["FL001", "FL002", "FL003", "FL004"])


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=str(cwd), check=True, capture_output=True)


class Sources(unittest.TestCase):
    """What the SBOM lists: the plugin's scripts as git sees them, so a file
    git ignores (a local scratch script, a virtualenv) cannot make --check fail."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        for rel in ("plugins/p/a.py", "plugins/p/new.py", "plugins/p/scratch/local.py", "plugins/p/gone.py",
                    "plugins/p/tests/test_a.py", "plugins/p/caf\u00e9.py", "outside.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text("x = 1\n", encoding="utf-8")
        patcher = mock.patch.object(g, "REPO", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_outside_git_every_non_test_script_under_plugins_is_listed(self):
        self.assertEqual(g.sources(), ["plugins/p/a.py", "plugins/p/caf\u00e9.py", "plugins/p/gone.py",
                                       "plugins/p/new.py", "plugins/p/scratch/local.py"])

    def test_in_git_ignored_and_deleted_files_are_left_out_and_new_files_are_kept(self):
        (self.root / ".gitignore").write_text("scratch/\n", encoding="utf-8")
        git(self.root, "init", "-q")
        git(self.root, "add", ".gitignore", "plugins/p/a.py", "plugins/p/gone.py", "plugins/p/tests/test_a.py",
            "plugins/p/caf\u00e9.py")
        git(self.root, "commit", "-q", "-m", "init")
        (self.root / "plugins/p/gone.py").unlink()  # deleted, not yet staged
        # new.py is untracked but not ignored: it is about to be committed, so it belongs in the SBOM
        self.assertEqual(g.sources(), ["plugins/p/a.py", "plugins/p/caf\u00e9.py", "plugins/p/new.py"])


if __name__ == "__main__":
    unittest.main()
