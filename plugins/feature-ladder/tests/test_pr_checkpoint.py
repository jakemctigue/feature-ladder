"""Tests for pr_checkpoint.py finish: the stack goes up (merged) and is labelled
review: pending; --no-merge leaves it open. Local-only mode (no remote, no gh).

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "skills" / "orchestration-ladder" / "scripts" / "pr_checkpoint.py"
PY = sys.executable


def git(root, *args):
    r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=str(root),
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    return r.stdout.strip()


def make_repo(tmp):
    root = Path(tmp)
    git(root, "init", "-q")
    git(root, "symbolic-ref", "HEAD", "refs/heads/main")  # the unborn branch is "main" on every git version
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(root, "add", "a.py")
    git(root, "commit", "-q", "-m", "init")
    return root


def run(root, *args):
    r = subprocess.run([PY, str(SCRIPT), *args], cwd=str(root), capture_output=True, text=True,
                       encoding="utf-8", timeout=120)
    return r.returncode, r.stdout, r.stderr


class Finish(unittest.TestCase):
    def start(self, root):
        code, out, err = run(root, "start", "--slug", "t")
        self.assertEqual(code, 0, err)
        state = json.loads(out)
        self.assertFalse(state["pr_enabled"])  # no remote: local branches
        self.assertEqual(state["outside_policy"], "all")
        (root / "b.py").write_text("y = 2\n", encoding="utf-8")
        body = root / "body.md"
        body.write_text("what changed\n", encoding="utf-8")
        return state, body

    def test_finish_merges_the_stack_locally_and_labels_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            state, body = self.start(root)
            # something the audit hook logged during the run
            run_json = root / ".feature-ladder" / "run.json"
            live = json.loads(run_json.read_text(encoding="utf-8"))
            live["outside_actions"] = [{"at": "2026-01-01T00:00:00+00:00", "cmd": "firebase deploy --project prod",
                                        "label": "firebase deploy", "environment": "prod", "tier": "mundane",
                                        "status": "ran", "review": "pending"}]
            run_json.write_text(json.dumps(live), encoding="utf-8")
            code, out, err = run(root, "finish", "--title", "add b", "--body-file", str(body))
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(res["merged"], [{"branch": state["branch"], "result": "merged locally"}])
            self.assertEqual(res["review"], "review: pending")
            self.assertIn("b.py", git(root, "ls-tree", "--name-only", "main"))
            self.assertIn("review: pending", git(root, "log", "-1", "--format=%s", "main"))
            self.assertFalse(json.loads(run_json.read_text(encoding="utf-8"))["active"])
            self.assertEqual(git(root, "rev-parse", "--abbrev-ref", "HEAD"), "main")  # back on the base, with the work
            saved = (root / ".feature-ladder" / "checkpoints" / "_body.md").read_text(encoding="utf-8")
            self.assertIn("review: pending", saved)
            self.assertIn("firebase deploy --project prod", saved)

    def test_two_checkpoints_land_every_entry_on_the_original_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            state, body = self.start(root)  # writes b.py
            self.assertEqual(run(root, "checkpoint", "--title", "one", "--body-file", str(body))[0], 0)
            (root / "c.py").write_text("z = 3\n", encoding="utf-8")
            self.assertEqual(run(root, "checkpoint", "--title", "two", "--body-file", str(body))[0], 0)
            (root / "d.py").write_text("w = 4\n", encoding="utf-8")
            code, out, err = run(root, "finish", "--title", "all", "--body-file", str(body))
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(len(res["merged"]), 3)
            files = git(root, "ls-tree", "--name-only", "main").split()
            for f in ("a.py", "b.py", "c.py", "d.py"):  # the final entry's work reached main
                self.assertIn(f, files)

    def test_no_merge_leaves_the_base_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            _, body = self.start(root)
            code, out, err = run(root, "finish", "--title", "add b", "--body-file", str(body), "--no-merge")
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual(res["merged"], [])
            self.assertIsNone(res["review"])
            self.assertNotIn("b.py", git(root, "ls-tree", "--name-only", "main"))

    def test_draft_implies_no_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            _, body = self.start(root)
            code, out, err = run(root, "finish", "--title", "add b", "--body-file", str(body), "--draft")
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["merged"], [])


def feature(root, slug, filename):
    """One whole feature: start, write a file, finish. Returns (start state, finish result)."""
    code, out, err = run(root, "start", "--slug", slug)
    assert code == 0, out + err
    state = json.loads(out)
    (root / filename).write_text("v = 1\n", encoding="utf-8")
    body = root / ".git" / "body.md"  # outside the work tree: never committed
    body.write_text("what changed\n", encoding="utf-8")
    code, out, err = run(root, "finish", "--title", f"add {filename}", "--body-file", str(body))
    assert code == 0, out + err
    return state, json.loads(out)


def head(root):
    return git(root, "rev-parse", "--abbrev-ref", "HEAD")


def run_json(root):
    return json.loads((root / ".feature-ladder" / "run.json").read_text(encoding="utf-8"))


class Session(unittest.TestCase):
    """Several features in one session: each starts from the base, each reaches it."""

    def test_the_second_feature_of_a_session_reaches_the_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            feature(root, "feat-a", "a2.py")
            self.assertEqual(head(root), "main")
            state_b, res_b = feature(root, "feat-b", "b2.py")
            self.assertEqual(state_b["base"], "main")
            self.assertEqual(res_b["merged"][0]["result"], "merged locally")
            files = git(root, "ls-tree", "--name-only", "main").split()
            self.assertIn("a2.py", files)
            self.assertIn("b2.py", files)

    def test_start_on_a_finished_runs_branch_re_anchors_on_its_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            state_a, _ = feature(root, "feat-a", "a2.py")
            git(root, "checkout", "-q", state_a["branch"])  # where every run before the fix was left
            code, out, err = run(root, "start", "--slug", "feat-b")
            self.assertEqual(code, 0, out + err)
            state_b = json.loads(out)
            self.assertEqual(state_b["base"], "main")
            self.assertTrue(any(state_a["branch"] in w for w in state_b.get("warnings", [])))
            self.assertEqual(git(root, "merge-base", "HEAD", "main"), git(root, "rev-parse", "main"))

    def test_start_refuses_while_a_run_is_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            self.assertEqual(run(root, "start", "--slug", "one")[0], 0)
            live = run_json(root)
            live["outside_actions"] = [{"cmd": "firebase deploy --project prod", "review": "pending"}]
            (root / ".feature-ladder" / "run.json").write_text(json.dumps(live), encoding="utf-8")
            code, out, err = run(root, "start", "--slug", "two")
            self.assertEqual(code, 1)
            self.assertIn("one", json.loads(out)["error"])
            self.assertIn("abort", json.loads(out)["error"])
            kept = run_json(root)
            self.assertEqual(kept["slug"], "one")              # not overwritten
            self.assertEqual(len(kept["outside_actions"]), 1)  # the pending row survives
            self.assertEqual(head(root), "feature-ladder/one")
            self.assertEqual(run(root, "abort")[0], 0)
            self.assertEqual(run(root, "start", "--slug", "two")[0], 0)

    def test_abort_returns_to_the_base_and_keeps_uncommitted_work_where_it_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            run(root, "start", "--slug", "one")
            code, out, _ = run(root, "abort")
            self.assertEqual(code, 0)
            self.assertEqual(head(root), "main")
            self.assertEqual(json.loads(out)["on"], "main")
            run(root, "start", "--slug", "two")
            (root / "half.py").write_text("x = 0\n", encoding="utf-8")
            code, out, _ = run(root, "abort")
            self.assertEqual(code, 0)
            self.assertEqual(head(root), "feature-ladder/two")  # not moved under uncommitted work
            self.assertIn("main", json.loads(out)["note"])

    def test_no_merge_then_finish_keeps_one_final_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            run(root, "start", "--slug", "x")
            (root / "b.py").write_text("y = 2\n", encoding="utf-8")
            body = root / ".git" / "body.md"
            body.write_text("what changed\n", encoding="utf-8")
            self.assertEqual(run(root, "finish", "--title", "t", "--body-file", str(body), "--no-merge")[0], 0)
            self.assertEqual(head(root), "main")
            code, out, err = run(root, "finish", "--title", "t", "--body-file", str(body))
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertEqual([(e["kind"], e["branch"]) for e in res["stack"]], [("final", "feature-ladder/x")])
            self.assertEqual(res["merged"], [{"branch": "feature-ladder/x", "result": "merged locally"}])
            self.assertIn("b.py", git(root, "ls-tree", "--name-only", "main").split())
            self.assertEqual(git(root, "status", "--porcelain"), "")  # nothing was committed onto main directly

    def test_a_detached_start_is_not_reported_as_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            git(root, "checkout", "-q", "--detach")
            code, out, err = run(root, "start", "--slug", "d")
            self.assertEqual(code, 0, err)
            (root / "b.py").write_text("y = 2\n", encoding="utf-8")
            body = root / ".git" / "body.md"
            body.write_text("what changed\n", encoding="utf-8")
            code, out, err = run(root, "finish", "--title", "t", "--body-file", str(body))
            self.assertEqual(code, 0, err)
            res = json.loads(out)
            self.assertTrue(res["merged"][0]["result"].startswith("not merged"), res["merged"])
            self.assertIn("detached", res["merged"][0]["result"])
            self.assertIsNone(res["review"])
            self.assertNotIn("b.py", git(root, "ls-tree", "--name-only", "main").split())
            self.assertIn("b.py", git(root, "ls-tree", "--name-only", "feature-ladder/d").split())  # the work is kept

    def test_start_records_where_the_run_and_the_session_began(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".feature-ladder").mkdir()
            session = root / ".feature-ladder" / "session.json"
            session.write_text(json.dumps({"started": "S1", "spawned": []}), encoding="utf-8")
            first = git(root, "rev-parse", "main")
            state_a, _ = feature(root, "feat-a", "a2.py")
            self.assertEqual((state_a["base_sha"], state_a["session_base_sha"], state_a["session_started"]),
                             (first, first, "S1"))
            second = git(root, "rev-parse", "main")
            self.assertNotEqual(first, second)
            state_b, _ = feature(root, "feat-b", "b2.py")
            self.assertEqual((state_b["base_sha"], state_b["session_base_sha"]), (second, first))  # same session
            session.write_text(json.dumps({"started": "S2", "spawned": []}), encoding="utf-8")
            third = git(root, "rev-parse", "main")
            state_c, _ = feature(root, "feat-c", "c2.py")
            self.assertEqual((state_c["base_sha"], state_c["session_base_sha"], state_c["session_started"]),
                             (third, third, "S2"))  # a new session starts its own range

    def test_a_run_started_in_a_linked_worktree_keeps_its_state_in_the_main_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "main").mkdir()
            root = make_repo(Path(tmp) / "main")
            wt = Path(tmp) / "wt"
            git(root, "worktree", "add", "-q", "-b", "side", str(wt))
            code, out, err = run(wt, "start", "--slug", "w")
            self.assertEqual(code, 0, out + err)
            self.assertTrue((root / ".feature-ladder" / "run.json").is_file())
            self.assertFalse((wt / ".feature-ladder" / "run.json").exists())
            code, out, _ = run(root, "status")
            self.assertEqual((code, json.loads(out)["slug"]), (0, "w"))

    def test_the_intake_a_run_starts_from_is_marked_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            intake = root / ".git" / "intake.json"
            intake.write_text(json.dumps({"confirmed_at": "T", "budget": {"size": "M", "workers": 2, "design": False}}),
                              encoding="utf-8")
            code, out, err = run(root, "start", "--slug", "i", "--intake", str(intake))
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["budget"]["size"], "M")
            self.assertTrue(json.loads(intake.read_text(encoding="utf-8")).get("used"))


class Options(unittest.TestCase):
    def test_a_misspelt_option_is_an_error_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            code, out, _ = run(root, "start", "--slgu", "x")
            self.assertEqual(code, 1)
            self.assertIn("--slgu", json.loads(out)["error"])
            self.assertIn("--slug", json.loads(out)["error"])  # says what is allowed
            self.assertFalse((root / ".feature-ladder" / "run.json").exists())
            self.assertEqual(head(root), "main")
            code, out, _ = run(root, "start", "--slug", "x", "--workspace", "keep")
            self.assertEqual(code, 1)
            self.assertIn("include", json.loads(out)["error"])
            self.assertEqual(head(root), "main")

    def test_start_learns_the_deploy_targets_without_a_full_scan(self):
        sys.path.insert(0, str(SCRIPT.parent))
        sys.path.insert(0, str(PLUGIN / "skills" / "complexity-router" / "scripts"))
        from unittest import mock
        import intake
        import pr_checkpoint as pc
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".firebaserc").write_text(json.dumps({"projects": {"default": "myapp-prod", "dev": "myapp-dev"}}),
                                              encoding="utf-8")
            git(root, "add", ".firebaserc")
            git(root, "commit", "-q", "-m", "firebase")
            with mock.patch.object(intake, "scan", side_effect=AssertionError("a whole-repo scan just to read .firebaserc")):
                state = pc.cmd_start(root, {"--slug": "e"})
            self.assertEqual(state["environments"]["production"], ["myapp-prod"])
            self.assertEqual(state["environments"]["nonproduction"], ["myapp-dev"])


class Workspace(unittest.TestCase):
    def test_untracked_secret_files_are_left_out_of_every_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            for name in (".env", ".env.local", "deploy.pem", "svc-credentials.json", "sub/.env"):
                (root / name).parent.mkdir(exist_ok=True)
                (root / name).write_text("SECRET=1\n", encoding="utf-8")
            (root / ".env.example").write_text("SECRET=\n", encoding="utf-8")
            (root / "notes.txt").write_text("mine\n", encoding="utf-8")
            code, out, err = run(root, "start", "--slug", "s")
            self.assertEqual(code, 0, err)
            state = json.loads(out)
            body = root / ".git" / "body.md"
            body.write_text("what changed\n", encoding="utf-8")
            code, out, err = run(root, "finish", "--title", "t", "--body-file", str(body))
            self.assertEqual(code, 0, err)
            committed = git(root, "ls-tree", "-r", "--name-only", "main").split()
            for name in ("notes.txt", ".env.example"):
                self.assertIn(name, committed)
            for name in (".env", ".env.local", "deploy.pem", "svc-credentials.json", "sub/.env"):
                self.assertNotIn(name, committed)
                self.assertTrue((root / name).is_file())  # still on disk, untouched
            self.assertTrue(any(".env" in w and "not committed" in w for w in state["warnings"]), state["warnings"])

    def test_a_tracked_file_with_a_secret_like_name_is_still_committed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / "ci.env").write_text("A=1\n", encoding="utf-8")
            git(root, "add", "ci.env")
            git(root, "commit", "-q", "-m", "track it")
            (root / "ci.env").write_text("A=2\n", encoding="utf-8")
            code, out, err = run(root, "start", "--slug", "s")
            self.assertEqual(code, 0, err)
            self.assertEqual(git(root, "diff", "--name-only", "HEAD"), "")  # the user's edit went into the commit
            self.assertIn("ci.env", git(root, "show", "--name-only", "--format=", "HEAD"))
            self.assertNotIn("warnings", json.loads(out))


class Errors(unittest.TestCase):
    def test_a_corrupt_run_json_is_a_one_line_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".feature-ladder").mkdir()
            (root / ".feature-ladder" / "run.json").write_text('{"active": true, "slug": "x', encoding="utf-8")
            for sub in (["status"], ["finish", "--title", "t", "--body-file", str(root / "a.py")], ["abort"]):
                code, out, err = run(root, *sub)
                self.assertEqual(code, 1, sub)
                self.assertNotIn("Traceback", out + err)
                self.assertIn("run.json", json.loads(out)["error"])

    def test_start_replaces_an_unreadable_run_json_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".feature-ladder").mkdir()
            (root / ".feature-ladder" / "run.json").write_text('{"active": true, "slug": "x', encoding="utf-8")
            code, out, err = run(root, "start", "--slug", "fresh")
            self.assertEqual(code, 0, out + err)
            state = json.loads(out)
            self.assertEqual(state["slug"], "fresh")
            self.assertTrue(any("unreadable" in w for w in state["warnings"]), state.get("warnings"))
            self.assertEqual(run_json(root)["slug"], "fresh")

    def test_a_run_json_from_an_older_version_is_an_error_not_the_usage_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_repo(tmp)
            (root / ".feature-ladder").mkdir()
            (root / ".feature-ladder" / "run.json").write_text('{"active": true, "slug": "x"}', encoding="utf-8")
            code, out, err = run(root, "finish", "--title", "t", "--body-file", str(root / "a.py"))
            self.assertEqual(code, 1)
            self.assertNotIn("checkpoint --title T --body-file F", out)  # not the docstring
            self.assertIn("branch", json.loads(out)["error"])

    def test_an_unknown_subcommand_prints_the_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run(make_repo(tmp), "frobnicate")
            self.assertEqual(code, 1)
            self.assertIn("checkpoint --title T --body-file F", out)


class Shell(unittest.TestCase):
    """sh(): the reason for a failure, a time limit, and no credential prompt."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPT.parent))
        import pr_checkpoint
        cls.pc = pr_checkpoint

    def test_a_failed_command_returns_what_it_wrote_to_stderr(self):
        code, out = self.pc.sh([PY, "-c", "import sys; sys.stderr.write('checks pending'); sys.exit(1)"], ".", check=False)
        self.assertEqual((code, out), (1, "checks pending"))

    def test_a_successful_command_returns_only_stdout(self):
        code, out = self.pc.sh([PY, "-c", "import sys; sys.stderr.write('warning'); print('value')"], ".", check=False)
        self.assertEqual((code, out), (0, "value"))

    def test_network_commands_get_a_time_limit_and_no_prompt(self):
        from unittest import mock
        seen = []

        def fake_spawn(args, cwd, env, limit):
            seen.append((list(args), env, limit))
            return 0, "", ""

        with mock.patch.object(self.pc, "_spawn", fake_spawn):
            for cmd in (["git", "push", "-q", "-u", "origin", "x"], ["gh", "pr", "create"], ["gh", "auth", "status"],
                        ["git", "pull", "-q", "--ff-only", "origin", "main"], ["git", "status"]):
                self.pc.sh(cmd, ".", check=False)
        self.assertEqual(len(seen), 5)
        for args, env, limit in seen:
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0", args)
            self.assertEqual(env["GH_PROMPT_DISABLED"], "1", args)
            self.assertTrue(limit and limit > 0, args)

    def test_a_missing_program_is_still_a_file_not_found(self):
        with self.assertRaises(FileNotFoundError):  # pr_ready() tells "gh not installed" from this
            self.pc.sh(["no-such-program-feature-ladder"], ".", check=False)

    def test_a_timeout_is_a_failure_with_a_reason_not_a_hang(self):
        code, out = self.pc.sh([PY, "-c", "import time; time.sleep(30)"], ".", check=False, timeout=1)
        self.assertNotEqual(code, 0)
        self.assertIn("timed out", out)
        with self.assertRaises(RuntimeError):
            self.pc.sh([PY, "-c", "import time; time.sleep(30)"], ".", timeout=1)


if __name__ == "__main__":
    unittest.main()
