"""CI failure-mode corpus: every hand-written log in fixtures/ci_logs/ must
classify to the failure mode below, each mode must have a section in
docs/ci-troubleshooting.md, and every classifier anchor must resolve to a
heading there. The logs are synthetic (representative, not captured); the
classifier is plain regexes, so nothing here needs the network or a model.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import re
import sys
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
LOGS = PLUGIN / "tests" / "fixtures" / "ci_logs"
DOC = REPO / "docs" / "ci-troubleshooting.md"
sys.path.insert(0, str(PLUGIN / "scripts" / "evidence"))

import ci_log_classifier as clf  # noqa: E402

EXPECTED = {
    "unittest_failure.log": "unittest-failure",
    "compileall_syntax_error.log": "compileall-syntax-error",
    "setup_python_version_missing.log": "python-version-missing",
    "windows_path_crlf.log": "windows-path-or-crlf",
    "import_error.log": "import-error",
    "timeout_cancelled.log": "timeout-or-cancelled",
    "unclassified_checkout_network.log": "unclassified",
}


def slug(heading):
    return re.sub(r"[^a-z0-9_-]", "", heading.strip().lower().replace(" ", "-"))


def doc_anchors():
    return {slug(m.group(1)) for m in re.finditer(r"^#{2,3} (.+)$", DOC.read_text(encoding="utf-8"), re.M)}


class CiLogs(unittest.TestCase):
    def test_every_log_has_an_expectation_and_vice_versa(self):
        self.assertEqual({p.name for p in LOGS.glob("*.log")}, set(EXPECTED))

    def test_each_log_classifies_to_its_failure_mode(self):
        for name, mode in EXPECTED.items():
            with self.subTest(log=name):
                result = clf.classify((LOGS / name).read_text(encoding="utf-8"))
                self.assertEqual(result["mode"], mode)

    def test_crlf_checkout_of_a_log_classifies_the_same(self):
        for name, mode in EXPECTED.items():
            with self.subTest(log=name):
                raw = (LOGS / name).read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                self.assertEqual(clf.classify(raw.decode("utf-8"))["mode"], mode)

    def test_every_anchor_is_a_heading_in_the_troubleshooting_doc(self):
        anchors = doc_anchors()
        modes = {m for m, _a, _p in clf.RULES} | {clf.UNCLASSIFIED[0]}
        for mode, anchor, _p in clf.RULES:
            self.assertIn(anchor, anchors, mode)
        self.assertIn(clf.UNCLASSIFIED[1], anchors)
        self.assertEqual(modes, set(EXPECTED.values()))

    def test_empty_log_is_unclassified(self):
        self.assertEqual(clf.classify("")["mode"], "unclassified")


if __name__ == "__main__":
    unittest.main()
