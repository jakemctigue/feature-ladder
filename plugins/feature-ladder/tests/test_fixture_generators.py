"""The golden fixtures are generated: scripts/fixtures/gen_*.py must reproduce
the committed files byte for byte (so nobody hand-edits a fixture and loses the
edit on the next regeneration), from any working directory.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
GENERATORS = PLUGIN / "scripts" / "fixtures"
FIXTURES = {"gen_routing.py": PLUGIN / "tests" / "fixtures" / "routing" / "golden_cases.json",
            "gen_rank.py": PLUGIN / "tests" / "fixtures" / "skill_ranking" / "cases.json",
            "gen_triage.py": PLUGIN / "tests" / "fixtures" / "codex_triage" / "cases.json"}


class Generators(unittest.TestCase):
    def test_every_generator_reproduces_its_committed_fixture(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            for name, fixture in FIXTURES.items():
                with self.subTest(generator=name):
                    before = fixture.read_bytes()
                    r = subprocess.run([sys.executable, str(GENERATORS / name), "--check"], cwd=elsewhere,
                                       capture_output=True, text=True, encoding="utf-8", timeout=120)
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertEqual(fixture.read_bytes(), before)  # --check never writes

    def test_check_reports_drift(self):
        sys.path.insert(0, str(GENERATORS))
        import fixture_io
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            target = Path(tmp) / "cases.json"
            self.assertEqual(fixture_io.emit(target, {"a": 1}, check=True), 1)       # missing
            self.assertEqual(fixture_io.emit(target, {"a": 1}, check=False), 0)
            self.assertEqual(target.read_bytes(), b'{\n  "a": 1\n}\n')              # LF, sorted, indented
            self.assertEqual(fixture_io.emit(target, {"a": 1}, check=True), 0)
            self.assertEqual(fixture_io.emit(target, {"a": 2}, check=True), 1)       # drift
            self.assertEqual(target.read_bytes(), b'{\n  "a": 1\n}\n')

    def test_the_old_scratch_copies_are_gone(self):
        scratch = PLUGIN.parents[1] / ".feature-ladder" / "scratch"
        self.assertEqual(sorted(p.name for p in scratch.glob("gen_*.py")) if scratch.is_dir() else [], [])


if __name__ == "__main__":
    unittest.main()
