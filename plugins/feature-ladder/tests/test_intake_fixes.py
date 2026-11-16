"""Regression tests for the intake: environment classification, count buckets,
reuse of a saved intake, question selection, git path handling, tolerant
proposal parsing and path markers.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
ROUTER = PLUGIN / "skills" / "complexity-router" / "scripts"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import intake  # noqa: E402
import route as router  # noqa: E402

REQUEST = "add retry to the upload client"


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8").stdout


def make_repo(tmp, files=None):
    root = Path(tmp).resolve() / "repo"
    root.mkdir()
    git(root, "init", "-q")
    for rel, text in (files or {"src/upload_client.py": "def upload():\n    return 1\n",
                                "tests/test_upload_client.py": "def test_ok():\n    assert True\n"}).items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def cli(root, *args):
    return subprocess.run([sys.executable, str(ROUTER / "intake.py"), *args], cwd=str(root),
                          capture_output=True, text=True, encoding="utf-8", timeout=120)


def high(**vals):
    base = {"kind": "code", "files": 2, "loc": 40, "modules": 1, "public_api": False, "persistence": False,
            "security": False, "concurrency": False, "cross_system": False, "new_dependency": False,
            "no_tests": False, "irreversible": False, "ambiguity": 1, "novelty": 0, "blast_radius": 1,
            "design_agreed": False}
    base.update(vals)
    return {k: {"value": v, "confidence": "high", "evidence": ["fixture"]} for k, v in base.items()}


class Environments(unittest.TestCase):
    PROJECTS = {"default": "backstage-prod", "live": "latest-app", "contest": "contest-app-prod",
                "dev": "myapp-dev", "qa": "myapp-qa2", "staging": "shop_staging"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_repo(self.tmp.name)
        (self.root / ".firebaserc").write_text(json.dumps({"projects": self.PROJECTS}), encoding="utf-8")

    def test_a_project_whose_name_merely_contains_test_or_stage_is_production(self):
        env = intake.environments(self.root)
        self.assertEqual(sorted(env["production"]), ["backstage-prod", "contest-app-prod", "latest-app"])
        self.assertEqual(sorted(env["nonproduction"]), ["myapp-dev", "myapp-qa2", "shop_staging"])
        self.assertEqual(env["default"], "backstage-prod")

    def test_a_prod_token_wins_over_a_nonproduction_token(self):
        (self.root / ".firebaserc").write_text(json.dumps({"projects": {"staging": "shop-prod"}}), encoding="utf-8")
        self.assertEqual(intake.environments(self.root)["production"], ["shop-prod"])

    def test_scan_reports_the_same_environments(self):
        self.assertEqual(intake.scan(REQUEST, str(self.root))["environments"], intake.environments(self.root))


class CountBuckets(unittest.TestCase):
    CASES = {"files": range(1, 12), "loc": (0, 10, 30, 31, 40, 60, 120, 121, 150, 200, 300, 301, 350, 900),
             "modules": range(1, 6)}

    def test_the_recommended_option_scores_like_the_scouts_estimate(self):
        for name, values in self.CASES.items():
            for v in values:
                opts = intake._options(name, {"value": v, "evidence": []})
                rec = [o for o in opts if o["label"].endswith("(Recommended)")]
                self.assertEqual(len(rec), 1, (name, v))
                self.assertEqual(router.POINTS[name](rec[0]["value"]), router.POINTS[name](v), (name, v))

    def test_alternatives_are_the_neighbouring_buckets(self):
        self.assertEqual(intake._alternatives("files", {"value": 2}), [1, 6])
        self.assertEqual(intake._alternatives("files", {"value": 7}), [6])
        self.assertEqual(intake._alternatives("loc", {"value": 40}), [30, 300])
        for name, values in self.CASES.items():
            for v in values:
                for alt in intake._alternatives(name, {"value": v}):
                    self.assertNotEqual(router.POINTS[name](alt), router.POINTS[name](v), (name, v, alt))


class Questions(unittest.TestCase):
    OPUS = dict(files=8, loc=400, modules=3, security=True, concurrency=True, novelty=2, cross_system=True,
                irreversible=True, public_api=True, persistence=True)

    def asked(self, fields):
        prop = {"request": REQUEST, "slug": "x", "fields": fields, "open_decisions": []}
        with mock.patch.object(intake, "session_status", return_value={"budget": "S", "orchestrator": {"chosen": True}}):
            return [q["field"] for q in intake.decide(prop)["questions"]]

    def test_an_uncertain_design_step_is_asked(self):
        fields = high(**self.OPUS)
        fields["design_agreed"] = {"value": False, "confidence": "low", "evidence": []}
        self.assertTrue(router.route({k: v["value"] for k, v in fields.items()})["design_first"])
        self.assertIn("design_agreed", self.asked(fields))

    def test_uncertain_ambiguity_that_would_add_a_design_step_is_asked(self):
        # Score 5: ambiguity 0 or 2 keeps implementer/sonnet/medium, but 2 adds a design step.
        fields = high(files=3, loc=100, public_api=True)
        fields["ambiguity"] = {"value": 1, "confidence": "low", "evidence": []}
        vals = {k: v["value"] for k, v in fields.items()}
        for alt in (0, 2):
            self.assertEqual(intake._key(router.route(dict(vals, ambiguity=alt)))[:3], intake._key(router.route(vals))[:3])
        self.assertIn("ambiguity", self.asked(fields))

    def test_a_confident_proposal_asks_nothing(self):
        self.assertEqual(self.asked(high()), [])


class TolerantProposals(unittest.TestCase):
    def setUp(self):
        self.patch = mock.patch.object(intake, "session_status",
                                       return_value={"budget": "S", "orchestrator": {"chosen": True}, "left": 2})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_bare_values_are_read_as_low_confidence_fields(self):
        prop = {"request": REQUEST, "slug": "x", "fields": {"files": 3, "loc": 90, "kind": "code"}}
        dec = intake.decide(prop)
        self.assertEqual(dec["predicted_route"]["agent"], router.route({"files": 3, "loc": 90})["agent"])
        final = intake.apply(prop, {})
        self.assertEqual((final["fields"]["files"], final["fields"]["loc"]), (3, 90))

    def test_null_values_do_not_crash(self):
        fields = high()
        for name in ("files", "ambiguity", "persistence"):
            fields[name] = {"value": None, "confidence": "low"}
        prop = {"request": REQUEST, "slug": "x", "fields": fields}
        intake.decide(prop)
        intake.apply(prop, {})

    def test_cli_rejects_a_bad_proposal_and_a_bad_mode_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            bad = root / "bad.json"
            bad.write_text(json.dumps({"request": "x", "fields": ["not", "a", "mapping"]}), encoding="utf-8")
            good = root / "good.json"
            good.write_text(json.dumps({"request": "x", "slug": "x", "fields": high(), "repo_root": str(root)}),
                            encoding="utf-8")
            for args in (["decide", str(bad)], ["apply", str(bad)], ["decide", str(good), "--mode", "Default"],
                         ["decide"], ["card", "{not json"]):
                r = cli(root, *args)
                self.assertEqual(r.returncode, 1, (args, r.stdout[-200:], r.stderr[-300:]))
                self.assertNotIn("Traceback", r.stderr, args)
            self.assertEqual(cli(root, "decide", str(good), "--mode", "auto").returncode, 0)


class GitPaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_repo(self.tmp.name, {
            "docs/café-upload.md": "how the upload retry works\n",
            "src/upload_client.py": "def upload():\n    return 'retry'\n",
            "package-lock.json": '{"upload": "retry retry retry"}\n',
            "pnpm-lock.yaml": "upload: retry\n",
        })

    def test_non_ascii_names_arrive_unquoted_and_lockfiles_are_not_candidates(self):
        (self.root / "notes café.txt").write_text("x\n", encoding="utf-8")
        s = intake.scan(REQUEST, str(self.root))
        self.assertIn("docs/café-upload.md", s["candidate_files"])
        self.assertIn("src/upload_client.py", s["candidate_files"])
        for f in s["candidate_files"]:
            self.assertTrue((self.root / f).is_file(), f)
        self.assertNotIn("package-lock.json", s["candidate_files"])
        self.assertNotIn("pnpm-lock.yaml", s["candidate_files"])
        self.assertIn("notes café.txt", s["git"]["dirty_files"])
        self.assertEqual(s["git"]["dirty_count"], 1)

    def test_scan_records_branch_and_head_with_few_git_spawns(self):
        calls = []
        real = intake._run

        def counting(cmd, cwd):
            calls.append(cmd)
            return real(cmd, cwd)

        with mock.patch.object(intake, "_run", side_effect=counting):
            s = intake.scan(REQUEST, str(self.root))
        self.assertLessEqual(len(calls), 7, calls)
        self.assertEqual(len([c for c in calls if "grep" in c and "-o" in c]), 1)
        self.assertEqual(s["git"]["head"], git(self.root, "rev-parse", "HEAD").strip())
        self.assertEqual(s["git"]["branch"], git(self.root, "rev-parse", "--abbrev-ref", "HEAD").strip())

    def test_a_renamed_file_is_one_dirty_entry(self):
        git(self.root, "mv", "src/upload_client.py", "src/uploader.py")
        s = intake.scan(REQUEST, str(self.root))
        self.assertEqual(s["git"]["dirty_files"], ["src/uploader.py"])

    def test_terms_have_no_empty_entry(self):
        self.assertEqual(intake.terms_of("fix ____ placeholder in config"), ["placeholder", "config"])

    def test_scan_of_a_missing_folder_fails(self):
        r = cli(self.root, "scan", REQUEST, "--cwd", str(self.root / "does" / "not" / "exist"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a folder", r.stderr)
        self.assertFalse((self.root / "does").exists())


class PathMarkers(unittest.TestCase):
    def hit(self, area, path):
        return intake._match(path, intake.AREA_MARKERS[area])

    def test_look_alike_names_are_not_areas(self):
        for area, path in (("concurrency", "package-lock.json"), ("concurrency", "pnpm-lock.yaml"),
                           ("concurrency", "src/Block.tsx"), ("concurrency", "lib/clock.py"),
                           ("security", "docs/oracle.md"), ("security", "src/author.ts"),
                           ("cross_system", "src/disintegration.py")):
            self.assertIsNone(self.hit(area, path), (area, path))

    def test_real_area_files_still_match(self):
        for area, path in (("concurrency", "src/file_lock.py"), ("concurrency", "app/workers/sync.py"),
                           ("concurrency", "src/threading_utils.py"), ("security", "src/auth/login.py"),
                           ("security", "src/AuthService.ts"), ("security", "api/oauth2.py"),
                           ("security", "src/tokens.py"), ("security", "src/authentication.py"),
                           ("cross_system", "src/firestore_client.py"), ("cross_system", "src/ApiClient.ts"),
                           ("persistence", "db/migrations/0001_init.sql"), ("persistence", "app/models.py"),
                           ("public_api", "src/routes/users.ts"), ("public_api", "tool/cli.py")):
            self.assertIsNotNone(self.hit(area, path), (area, path))


class SmallSlips(unittest.TestCase):
    def test_package_json_with_a_bom_still_yields_the_test_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "package.json").write_text("﻿" + json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8")
            self.assertEqual(intake.test_command(root), "npm test")

    def test_runner_command_uses_the_running_interpreter(self):
        cmd = intake.runner_command(Path("x") / "external_worker.py", Path("c") / "T1.md", "T1", "implementer")
        self.assertTrue(cmd.startswith(f'"{sys.executable}" '), cmd)
        self.assertNotIn("python3 ", cmd.replace(sys.executable, ""))
        self.assertIn("run --card", cmd)
        self.assertTrue(cmd.endswith("--id T1 --rung implementer"))

    def test_skill_flags_read_values_the_way_the_router_does(self):
        self.assertEqual(intake.skill_flags({"concurrency": "no", "security": "yes", "persistence": True, "public_api": 0}),
                         {"security": True, "persistence": True})

    def test_codex_hints_carry_no_dead_field(self):
        rec = intake.codex_recommendation({"novelty": 2}, "add a parser", {"size": "S"})
        self.assertEqual(sorted(rec), ["at_wrap", "review"])
        self.assertFalse(rec["review"])


class SecretsAndCard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_repo(self.tmp.name, {
            "src/upload_client.py": "import os\nKEY = os.environ.get('UPLOAD_API_KEY')\n"
                                    "client = secretmanager.SecretManagerServiceClient()\n"
                                    "name = 'projects/shop-prod/secrets/STRIPE_WEBHOOK_SECRET/versions/latest'\n",
            ".env.example": "UPLOAD_API_KEY=changeme-not-a-real-value\nLOG_LEVEL=info\n",
        })

    def test_secret_names_are_found_and_values_never_are(self):
        s = intake.scan(REQUEST, str(self.root))["secrets"]
        self.assertIn("UPLOAD_API_KEY", s["names"])
        self.assertIn("STRIPE_WEBHOOK_SECRET", s["names"])
        self.assertEqual(s["template_keys"], ["UPLOAD_API_KEY"])
        self.assertEqual(s["provider"], "google-secret-manager")
        self.assertNotIn("changeme-not-a-real-value", json.dumps(s))

    def test_card_prints_the_route_then_the_skills_block(self):
        task = {"id": "T 1/x", "kind": "docs", "files": 1, "loc": 10, "paths": ["README.md"], "text": "draft the readme"}
        r = cli(self.root, "card", json.dumps(task))
        self.assertEqual(r.returncode, 0, r.stderr)
        head, _, block = r.stdout.partition("\n\n")
        route = json.loads(head)
        self.assertEqual(route["agent"], "mechanic")
        runner = route["runner"]  # external when the GLM tier is set up on this machine, else subagent
        self.assertIn(runner["kind"], ("subagent", "external"))
        if runner["kind"] == "external":
            self.assertTrue(runner["command"].startswith(f'"{sys.executable}" '), runner["command"])
            self.assertIn("--id T-1-x", runner["command"])
        self.assertTrue(block.startswith("## Skills for this task"))

    def test_slugify_drops_stopwords_and_keeps_five_words(self):
        self.assertEqual(intake.slugify("Add retry to the upload client, with backoff and jitter please"),
                         "retry-upload-client-backoff-jitter")
        self.assertEqual(intake.slugify("the of and"), "feature")


class SavedIntake(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = make_repo(self.tmp.name)
        self.intake_dir = self.root / ".feature-ladder" / "intake"

    def confirm(self, **over):
        """scan -> apply through the CLI, as /feature-ladder:scope does."""
        scan = intake.scan(REQUEST, str(self.root))
        prop = dict(scan, fields=high())
        pf, af = self.root / "p.json", self.root / "a.json"
        pf.write_text(json.dumps(prop), encoding="utf-8")
        af.write_text(json.dumps({"decisions": {"session_agents": "Small", "orchestrator": "Opus 5.5 xhigh"}}),
                      encoding="utf-8")
        r = cli(self.root, "apply", str(pf), str(af))
        self.assertEqual(r.returncode, 0, r.stderr)
        path = Path(json.loads(r.stdout)["written_to"])
        if over:
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved.update(over)
            path.write_text(json.dumps(saved), encoding="utf-8")
        return path

    def find(self, cwd=None, *flags):
        r = cli(cwd or self.root, "find", *flags, REQUEST)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_apply_records_the_head_it_was_confirmed_at(self):
        saved = json.loads(self.confirm().read_text(encoding="utf-8"))
        self.assertEqual(saved["git_head"], git(self.root, "rev-parse", "HEAD").strip())

    def test_find_only_looks_so_scoping_twice_changes_nothing(self):
        path = self.confirm()
        for _ in range(2):
            self.assertEqual(Path(self.find()).resolve(), path.resolve())
        self.assertNotIn("used", json.loads(path.read_text(encoding="utf-8")))

    def test_find_mark_uses_it_up(self):
        path = self.confirm()
        self.assertEqual(Path(self.find(None, "--mark")).resolve(), path.resolve())
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["used"])
        self.assertEqual(self.find(), "")

    def test_an_intake_a_run_started_from_is_not_reused(self):
        # pr_checkpoint.py start --intake stamps "used" on the file it reads.
        path = self.confirm(used="2026-11-15T00:00:00+00:00")
        self.assertEqual(self.find(), "")
        self.assertTrue(path.is_file())

    def test_mark_used_hides_it_from_find(self):
        path = self.confirm()
        r = cli(self.root, "mark-used", str(path))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["used"])
        self.assertEqual(self.find(), "")
        self.assertTrue(intake.mark_used(path))          # idempotent
        self.assertFalse(intake.mark_used(self.root / "missing.json"))

    def test_the_intake_of_a_finished_run_is_not_reused(self):
        path = self.confirm()
        saved = json.loads(path.read_text(encoding="utf-8"))
        later = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(timespec="seconds")
        (self.root / ".feature-ladder" / "run.json").write_text(json.dumps(
            {"slug": saved["slug"], "active": False, "finished": later}), encoding="utf-8")
        self.assertEqual(self.find(), "")

    def test_an_aborted_run_alone_does_not_retire_the_intake(self):
        path = self.confirm()
        saved = json.loads(path.read_text(encoding="utf-8"))
        (self.root / ".feature-ladder" / "run.json").write_text(json.dumps(
            {"slug": saved["slug"], "active": False, "aborted": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
        self.assertEqual(Path(self.find()).resolve(), path.resolve())

    def test_an_intake_confirmed_on_another_history_is_stale(self):
        self.confirm(git_head="0" * 40)
        self.assertEqual(self.find(), "")

    def test_an_old_intake_is_stale(self):
        old = (datetime.now(timezone.utc) - timedelta(days=intake.MAX_INTAKE_AGE_DAYS + 1)).isoformat(timespec="seconds")
        self.confirm(confirmed_at=old)
        self.assertEqual(self.find(), "")

    def test_a_later_commit_on_the_same_branch_keeps_it_fresh(self):
        path = self.confirm()
        (self.root / "README.md").write_text("x\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "more")
        self.assertEqual(Path(self.find()).resolve(), path.resolve())

    def test_a_linked_worktree_finds_the_main_checkouts_intake(self):
        path = self.confirm()
        wt = self.root.parent / "wt"
        git(self.root, "worktree", "add", "-q", str(wt))
        self.assertEqual(Path(self.find(wt)).resolve(), path.resolve())

    def test_saving_keeps_every_runtime_file_ignored(self):
        self.confirm()
        lines = (self.root / ".feature-ladder" / ".gitignore").read_text(encoding="utf-8").splitlines()
        for entry in ("intake/", "cards/", "evidence/", "runs/", "run.json"):
            self.assertIn(entry, lines)


if __name__ == "__main__":
    unittest.main()
