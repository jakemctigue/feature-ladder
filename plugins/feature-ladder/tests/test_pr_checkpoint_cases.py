"""Decision cases for pr_checkpoint.py (branch naming, stack note, PR fallback
and the checkpoint -> finish stack order). Everything that would touch git,
gh or the network is replaced by a fake; files are written only to a temp dir.
The cases live in fixtures/pr_checkpoint/cases.json, whose header lists what
is deliberately not covered (a refused merge is coordinator behaviour, not
code in pr_checkpoint.py).

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import pr_checkpoint as pc  # noqa: E402

CASES = json.loads((PLUGIN / "tests" / "fixtures" / "pr_checkpoint" / "cases.json").read_text(encoding="utf-8"))


class FakeGit:
    """Stands in for pr_checkpoint.sh: records every command, answers from a table."""

    def __init__(self, taken=(), origin="ok", gh="ok", push_error=None, ahead="1",
                 gh_url="https://github.com/o/r/pull/7"):
        self.calls, self.taken, self.origin, self.gh = [], set(taken), origin, gh
        self.push_error, self.ahead, self.gh_url = push_error, ahead, gh_url

    def __call__(self, args, cwd, check=True):
        self.calls.append(list(args))
        if args[:3] == ["git", "rev-parse", "--verify"]:
            name = args[-1][len("refs/heads/"):]
            return (0, "abc1234") if name in self.taken else (1, "")
        if args[:3] == ["git", "remote", "get-url"]:
            return (0, "git@example:o/r.git") if self.origin == "ok" else (1, "")
        if args[:3] == ["gh", "auth", "status"]:
            if self.gh == "not-installed":
                raise FileNotFoundError("gh")
            return (0, "") if self.gh == "ok" else (1, "")
        if args[:2] == ["git", "push"] and self.push_error:
            raise RuntimeError(self.push_error)
        if args[:3] == ["gh", "pr", "create"]:
            return 0, self.gh_url
        if args[:3] == ["git", "rev-list", "--count"]:
            return 0, self.ahead
        if args[:3] == ["git", "rev-parse", "--short"]:
            return 0, "abc1234"
        return 0, ""


class BranchNaming(unittest.TestCase):
    def test_free_branch(self):
        for c in CASES["free_branch"]:
            with self.subTest(case=c["name"]):
                with mock.patch.object(pc, "sh", FakeGit(taken=c["taken"])):
                    self.assertEqual(pc.free_branch(Path("."), c["ask"]), c["expect"])


class PrReady(unittest.TestCase):
    def test_pr_ready(self):
        for c in CASES["pr_ready"]:
            with self.subTest(case=c["name"]):
                with mock.patch.object(pc, "sh", FakeGit(origin=c["origin"], gh=c["gh"])):
                    self.assertEqual(list(pc.pr_ready(Path("."))), c["expect"])


class StackNote(unittest.TestCase):
    def test_stack_note(self):
        for c in CASES["stack_note"]:
            with self.subTest(case=c["name"]):
                self.assertEqual(pc.stack_note({"checkpoints": c["checkpoints"]}), c["expect"])


class OpenPr(unittest.TestCase):
    def test_open_pr(self):
        for c in CASES["open_pr"]:
            with self.subTest(case=c["name"]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                body = root / "body.md"
                body.write_text("the body\n", encoding="utf-8")
                fake = FakeGit(push_error=c["push_error"])
                run = {"pr_enabled": c["pr_enabled"], "base": "main"}
                with mock.patch.object(pc, "sh", fake):
                    url = pc.open_pr(root, run, "main", "feature-ladder/x", "Title", str(body), c["draft"])
                e = c["expect"]
                self.assertEqual(url, e["url"])
                saved = root / ".feature-ladder" / "checkpoints" / "feature-ladder__x.md"
                self.assertEqual(saved.is_file(), e["saved_locally"])
                if e["saved_locally"]:
                    text = saved.read_text(encoding="utf-8")
                    self.assertIn("# Title", text)
                    self.assertIn("base: main\nhead: feature-ladder/x", text)
                    self.assertIn("the body", text)
                self.assertEqual(len(run.get("warnings", [])), e["warnings"])
                creates = [a for a in fake.calls if a[:3] == ["gh", "pr", "create"]]
                if e["gh_draft"] is None:
                    self.assertEqual(creates, [])
                else:
                    self.assertEqual("--draft" in creates[0], e["gh_draft"])
                if not c["pr_enabled"]:
                    self.assertEqual([a for a in fake.calls if a[:2] == ["git", "push"]], [])


class Flow(unittest.TestCase):
    def test_checkpoint_then_finish_stack(self):
        for c in CASES["flow"]:
            with self.subTest(case=c["name"]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                body = root / "body.md"
                body.write_text("body\n", encoding="utf-8")
                state = {"slug": "x", "base": "main", "branch": "feature-ladder/x", "stack_base": "main",
                         "pr_enabled": False, "active": True, "checkpoints": []}
                with mock.patch.object(pc, "sh", FakeGit(ahead=c["ahead"])), \
                        mock.patch.object(pc, "commit_all", return_value=True), \
                        mock.patch.object(pc, "load", lambda r: state), \
                        mock.patch.object(pc, "save", lambda r, run: None), \
                        mock.patch.object(pc, "now", return_value="T"):
                    for _ in range(c["checkpoints"]):
                        pc.cmd_checkpoint(root, {"--title": "Decide", "--body-file": str(body)})
                    out = pc.cmd_finish(root, {"--title": "Feature x", "--body-file": str(body)})
                stack = out["stack"]
                self.assertEqual([s["branch"] for s in stack], c["expect"]["branches"])
                self.assertEqual([s["base"] for s in stack], c["expect"]["bases"])
                self.assertEqual([s["kind"] for s in stack], c["expect"]["kinds"])
                self.assertFalse(state["active"])


class GithubRetarget(unittest.TestCase):
    def test_later_prs_are_retargeted_to_the_original_base_and_queued_ones_do_not_stop(self):
        calls = []

        def fake(args, cwd, check=True):
            calls.append(list(args))
            if args[:3] == ["gh", "pr", "merge"] and "--auto" not in args and args[3].endswith("/1"):
                return 1, "checks pending"
            return 0, ""

        run = {"pr_enabled": True, "base": "main", "checkpoints": [
            {"branch": "f/a", "base": "main", "pr": "https://x/pull/1"},
            {"branch": "f/b", "base": "f/a", "pr": "https://x/pull/2"}]}
        with mock.patch.object(pc, "sh", fake):
            res = pc.merge_stack(Path("."), run)
        self.assertEqual(len(res), 2)
        self.assertTrue(res[0]["result"].startswith("queued"))
        self.assertEqual(res[1]["result"], "merged")
        self.assertIn(["gh", "pr", "edit", "https://x/pull/2", "--base", "main"], calls)
        self.assertNotIn("edit", [c[2] for c in calls if c[:2] == ["gh", "pr"] and "/1" in c[3]])
        self.assertTrue(any("pending" in w for w in run["warnings"]))


if __name__ == "__main__":
    unittest.main()
