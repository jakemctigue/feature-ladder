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
            self.assertEqual(git(root, "rev-parse", "--abbrev-ref", "HEAD"), state["branch"])  # back on the run branch
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


if __name__ == "__main__":
    unittest.main()
