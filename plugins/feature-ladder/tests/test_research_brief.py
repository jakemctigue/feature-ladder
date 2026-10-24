"""The research brief: three parts, qualitative cost labels, no numbers in the cost section.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
ROUTER = PLUGIN / "skills" / "complexity-router" / "scripts"
sys.path.insert(0, str(ROUTER))

import brief  # noqa: E402
import route as router  # noqa: E402

REQUEST = "add  retry to the   upload client"


def fields(**over):
    base = {"kind": "code", "files": 2, "loc": 60, "modules": 1, "ambiguity": 1, "novelty": 1, "blast_radius": 1,
            "no_tests": False}
    base.update(over)
    return base


def data(request=REQUEST, **over):
    f = over.pop("fields", fields())
    d = {"request": request, "fields": f, "route": router.route(f),
         "candidate_files": ["src/upload.py", "tests/test_upload.py"],
         "test_command": "python -m unittest", "secrets": {"names": ["UPLOAD_API_KEY"]},
         "open_decisions": [{"id": "backoff", "question": "Which backoff?", "options": ["fixed", "exponential"]}],
         "switches": ["researcher"]}
    d.update(over)
    return d


def sections(md):
    parts = re.split(r"(?m)^## ", md)
    keys = {"What the feature": "1", "What it would": "2", "What to expect": "3"}
    return {keys[next(k for k in keys if p.startswith(k))]: p for p in parts[1:]}


class Labels(unittest.TestCase):
    def worker(self, f):
        route = router.route(f)
        return [r for r in brief.cost_rows(route, {"workers": 1}) if r[0].startswith("worker")][0], route

    def test_glm_row_is_low_and_bills_openrouter(self):
        (_, rung, label, bill), route = self.worker(fields(files=1, loc=20))
        self.assertEqual(route["provider"], "glm")
        self.assertEqual((label, bill), ("low token cost", "OpenRouter credits"))
        self.assertEqual(rung, "GLM Flash")

    def test_sonnet_medium_is_medium_on_claude_plan(self):
        (_, rung, label, bill), route = self.worker(fields(files=4, loc=150, persistence=True, modules=3))
        self.assertEqual((route["model"], route["effort"], route["size"]), ("sonnet", "medium", "M"))
        self.assertEqual((rung, label, bill), ("Sonnet medium", "medium token cost", "Claude plan"))

    def test_sonnet_high_and_opus_are_high(self):
        high = dict(files=9, loc=400, modules=3, persistence=True, security=True)
        for f in (fields(**high), fields(**high, concurrency=True, cross_system=True, irreversible=True,
                                         new_dependency=True, no_tests=True)):
            (_, _, label, bill), route = self.worker(f)
            self.assertEqual(label, "high token cost", route)
            self.assertEqual(bill, "Claude plan")

    def test_size_l_bumps_worker_one_label_capped_at_high(self):
        base = {"agent": "implementer", "model": "sonnet", "effort": "medium", "provider": "claude"}
        m = [r for r in brief.cost_rows(dict(base, size="M"), {}) if r[0].startswith("worker")][0]
        big = [r for r in brief.cost_rows(dict(base, size="L"), {}) if r[0].startswith("worker")][0]
        self.assertEqual((m[2], big[2]), ("medium token cost", "high token cost"))
        glm = dict(base, provider="glm", model="haiku", size="L")
        self.assertEqual([r for r in brief.cost_rows(glm, {}) if r[0].startswith("worker")][0][2],
                         "medium token cost")
        top = dict(base, model="opus", size="L")
        self.assertEqual([r for r in brief.cost_rows(top, {}) if r[0].startswith("worker")][0][2],
                         "high token cost")

    def test_plan_rows(self):
        route = dict(router.route(fields(files=9, loc=400, modules=3, persistence=True, security=True)))
        names = [r[0] for r in brief.cost_rows(route, {"workers": 4}, reviewer=True, evidence=True)]
        joined = " | ".join(names)
        for want in ("scout", "evidence collector", "coordinator", "worker", "integrator", "reviewer"):
            self.assertIn(want, joined)
        one = " | ".join(r[0] for r in brief.cost_rows(route, {"workers": 1}))
        self.assertNotIn("integrator", one)
        self.assertNotIn("reviewer", one)
        self.assertNotIn("evidence", one)


class Brief(unittest.TestCase):
    def test_cost_section_has_no_digits_and_only_labels(self):
        for f in (fields(), fields(files=1, loc=20), fields(files=9, loc=400, modules=3, persistence=True,
                                                            security=True, concurrency=True, cross_system=True,
                                                            irreversible=True, new_dependency=True)):
            md = brief.make_brief(data(fields=f, route=router.route(f), reviewer=True,
                                       external={"name": "libfoo", "notes": "v2 changes 3 APIs"},
                                       budget={"workers": 4}))
            cost = sections(md)["2"]
            self.assertIsNone(re.search(r"\d", cost), cost)
            for pct in ("%", "$", "token count", "turns"):
                self.assertNotIn(pct, cost)
            self.assertRegex(cost, r"(low|medium|high) token cost")

    def test_three_parts_in_order(self):
        md = brief.make_brief(data())
        heads = re.findall(r"(?m)^## (.*)$", md)
        self.assertEqual(heads, ["What the feature wants from the codebase", "What it would cost",
                                 "What to expect"])
        s1, s3 = sections(md)["1"], sections(md)["3"]
        for want in ("src/upload.py", "tests/test_upload.py", "python -m unittest", "UPLOAD_API_KEY",
                     "Which backoff?", "exponential"):
            self.assertIn(want, s1)
        for want in ("Predicted rung", "size", "verify.py", "review: pending", "--researcher"):
            self.assertIn(want, s3)

    def test_deterministic(self):
        self.assertEqual(brief.make_brief(data()), brief.make_brief(data()))

    def test_secret_values_never_present(self):
        md = brief.make_brief(data(secrets={"names": ["UPLOAD_API_KEY"], "where": {}}))
        self.assertIn("UPLOAD_API_KEY", md)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.addCleanup(self.tmp.cleanup)
        f = {k: {"value": v, "confidence": "low", "evidence": []} for k, v in fields().items()}
        self.prop = {"request": REQUEST, "slug": "retry-upload-client", "repo_root": str(self.root),
                     "candidate_files": ["src/upload.py"], "repo": {"test_command": "pytest"},
                     "fields": f, "open_decisions": []}
        self.pp = self.root / "p.json"
        self.pp.write_text(json.dumps(self.prop), encoding="utf-8")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(ROUTER / "brief.py"), *args, "--cwd", str(self.root)],
                              capture_output=True, text=True, encoding="utf-8")

    def test_build_find_show_roundtrip(self):
        r = self.run_cli("build", "--proposal", str(self.pp), "--no-skills")
        self.assertEqual(r.returncode, 0, r.stderr)
        saved = self.root / ".feature-ladder" / "intake" / "retry-upload-client-brief.md"
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_text(encoding="utf-8"), r.stdout)
        self.assertIn("ambiguity and blast radius", r.stdout)
        # a saved proposal sits next to the brief
        prop = saved.with_name("retry-upload-client-proposal.json")
        prop.write_text("{}", encoding="utf-8")
        f = self.run_cli("find", "  ADD".lower() + " retry to the upload   client")
        self.assertEqual(f.returncode, 0, f.stderr)
        self.assertEqual(Path(f.stdout.splitlines()[0]).resolve(), saved.resolve())
        self.assertEqual(Path(f.stdout.splitlines()[1]).resolve(), prop.resolve())
        s = self.run_cli("show", REQUEST)
        self.assertEqual(s.stdout, r.stdout)

    def test_find_misses_and_no_confirmed_intake_written(self):
        self.run_cli("build", "--proposal", str(self.pp), "--no-skills")
        self.assertNotEqual(self.run_cli("find", "something else entirely").returncode, 0)
        self.assertFalse((self.root / ".feature-ladder" / "intake" / "retry-upload-client.json").exists())

    def test_build_needs_input(self):
        self.assertEqual(self.run_cli("build").returncode, 1)


if __name__ == "__main__":
    unittest.main()
