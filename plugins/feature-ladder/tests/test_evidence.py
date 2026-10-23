"""The checked-in SBOM and SARIF in evidence/ must be the exact output of the
generator (so they cannot go stale), have the CycloneDX 1.5 / SARIF 2.1.0 shape
and carry nothing machine-specific.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

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

    def test_check_mode_exits_zero(self):
        r = subprocess.run([sys.executable, str(GEN), "--check"], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

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


if __name__ == "__main__":
    unittest.main()
