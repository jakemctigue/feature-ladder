"""Queue-cleanup decision corpus (fixtures/pr_checkpoint/queue_cleanup.json):
PR queue states a coordinator may find after a run - stale checkpoint PRs,
PRs with unresolved review threads, closed PRs worth reopening and abandoned
stacks worth salvaging - each with the expected cleanup decision from the
fixed enum close | keep-open | reopen | salvage | needs-human.

The schema check validates every case's state fields and decision. Cases
whose state has a real analog in pr_checkpoint.py (merge_entry / merge_stack)
additionally assert the merge, retarget, queued and refused results over the
same FakeGit-style mock used by test_pr_checkpoint_cases.py; cases without
an analog get only the schema check (the decision is coordinator policy,
not code in pr_checkpoint.py). Nothing here touches git, gh or the network.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import pr_checkpoint as pc  # noqa: E402

# fixtures/** are pinned to LF but readers normalise CRLF before parsing.
_text = (PLUGIN / "tests" / "fixtures" / "pr_checkpoint" / "queue_cleanup.json").read_text(encoding="utf-8")
CORPUS = json.loads(_text.replace("\r\n", "\n"))

DECISIONS = {"close", "keep-open", "reopen", "salvage", "needs-human"}
CATEGORIES = {"stale", "review-thread", "reopen", "salvage"}
STATE_SPEC = {
    "pr": int, "state": str, "head": str, "base": str, "labels": list,
    "age_days": int, "unresolved_threads": int, "checkpoint": bool,
    "stack_entries": int,
}


def make_run(entries, run_base, pr_enabled=True):
    run = {"base": run_base, "pr_enabled": pr_enabled, "checkpoints": list(entries)}
    if pr_enabled:
        for e in entries:
            e.setdefault("pr", None)
    return run


class GhFake:
    """Answers gh/git for the queue scenarios; records every command."""

    def __init__(self, mode):
        self.mode, self.calls = mode, []

    def __call__(self, args, cwd, check=True):
        self.calls.append(list(args))
        url = args[3] if len(args) > 3 else ""
        if args[:3] == ["gh", "pr", "merge"]:
            auto = "--auto" in args
            if self.mode == "gh-refused":
                return 1, "checks pending"
            if self.mode == "gh-queued":
                if url.endswith("/15") and not auto:
                    return 1, "checks pending"
                return 0, ""
            return 0, ""  # gh-merges-retarget
        if args[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return 0, "main"
        if args[:2] == ["git", "merge"] and self.mode == "local-merge-partial" \
                and "merge feature-ladder/e " in " ".join(args):
            raise RuntimeError("conflict")
        return 0, ""


def run_stack(fake, entries, run_base, pr_enabled=True):
    run = make_run(entries, run_base, pr_enabled)
    with mock.patch.object(pc, "sh", fake):
        results = pc.merge_stack(Path("."), run)
    return results, run


class Schema(unittest.TestCase):
    def test_every_case_has_valid_state_and_decision(self):
        ids = set()
        for case in CORPUS["cases"]:
            with self.subTest(case=case["id"]):
                self.assertIn(case["category"], CATEGORIES)
                self.assertIn(case["decision"], DECISIONS)
                self.assertNotIn(case["id"], ids)
                ids.add(case["id"])
                self.assertIsInstance(case["state"], dict)
                for key, kind in STATE_SPEC.items():
                    self.assertIn(key, case["state"])
                    self.assertIsInstance(case["state"][key], kind)
                    if kind is int:
                        self.assertGreaterEqual(case["state"][key], 0)
                if case["state"]["state"] != "open":
                    self.assertNotEqual(case["decision"], "keep-open")
                if "analog" in case:
                    self.assertIsInstance(case["analog"], dict)
                    self.assertIn("mode", case["analog"])
                    self.assertIn("expect", case["analog"])

    def test_decision_enum_is_the_fixed_five(self):
        self.assertEqual(sorted(CORPUS["decision_enum"]), sorted(DECISIONS))


class Analogs(unittest.TestCase):
    def _case(self, case_id):
        return next(c for c in CORPUS["cases"] if c["id"] == case_id)

    def test_stale_and_blocked_prs_are_refused_and_stop_the_stack(self):
        for cid in ("stale-draft-no-activity", "review-thread-blocks-merge",
                    "closed-checkpoint-worth-reopening"):
            c = self._case(cid)
            with self.subTest(case=cid):
                fake = GhFake("gh-refused")
                results, run = run_stack(fake, [dict(c["analog"]["entry"])], c["analog"]["run_base"])
                e = c["analog"]["expect"]
                self.assertTrue(results[0]["result"].startswith(e["merged_prefix"]))
                self.assertEqual(len(run["warnings"]), e["warnings"])
                self.assertEqual(len(results), 1)  # stack stops at the refusal
                if e["retarget_call"]:
                    self.assertIn(["gh", "pr", "edit", c["analog"]["entry"]["pr"], "--base", c["analog"]["run_base"]], fake.calls)
                else:
                    self.assertNotIn(["gh", "pr", "edit", c["analog"]["entry"]["pr"], "--base", c["analog"]["run_base"]], fake.calls)

    def test_already_merged_entries_are_skipped(self):
        c = self._case("stale-superseded-checkpoint")
        fake = GhFake("gh-merges-retarget")
        entries = [dict(e) for e in c["analog"]["entries"]]
        results, _ = run_stack(fake, entries, c["analog"]["run_base"])
        e = c["analog"]["expect"]
        self.assertEqual([r["branch"] for r in results], e["attempted_branches"])
        self.assertEqual(len([x for x in fake.calls if x[:3] == ["gh", "pr", "merge"]]), e["merge_calls"])

    def test_pending_checks_queue_the_pr_and_later_ones_retarget(self):
        c = self._case("review-thread-auto-queued")
        fake = GhFake("gh-queued")
        entries = [dict(e) for e in c["analog"]["entries"]]
        results, run = run_stack(fake, entries, c["analog"]["run_base"])
        e = c["analog"]["expect"]
        self.assertEqual([r["result"] for r in results], e["results"])
        for url in e["retarget_urls"]:
            self.assertIn(["gh", "pr", "edit", url, "--base", c["analog"]["run_base"]], fake.calls)
        self.assertTrue(any(e["warnings_substring"] in w for w in run["warnings"]))

    def test_reopened_pr_is_retargeted_to_the_original_base_then_merged(self):
        c = self._case("closed-final-pr-rebases-onto-original-base")
        fake = GhFake("gh-merges-retarget")
        results, _ = run_stack(fake, [dict(c["analog"]["entry"])], c["analog"]["run_base"])
        e = c["analog"]["expect"]
        self.assertEqual(results[0]["result"], e["merged"])
        self.assertIn(e["retarget_args"], fake.calls)

    def test_abandoned_stack_salvages_locally(self):
        c = self._case("abandoned-stack-salvage-local")
        fake = GhFake("local-merge")
        results, _ = run_stack(fake, [dict(c["analog"]["entry"])], c["analog"]["run_base"], pr_enabled=False)
        e = c["analog"]["expect"]
        self.assertEqual(results[0]["result"], e["merged"])
        self.assertEqual([x for x in fake.calls if x[:2] == ["gh", "pr"]], [])

    def test_abandoned_stack_salvages_until_a_local_refusal(self):
        c = self._case("abandoned-stack-salvage-partial")
        fake = GhFake("local-merge-partial")
        entries = [dict(e) for e in c["analog"]["entries"]]
        results, run = run_stack(fake, entries, c["analog"]["run_base"], pr_enabled=False)
        e = c["analog"]["expect"]
        got = [r["result"] for r in results]
        self.assertEqual(len(got), len(e["results"]))
        for g, want in zip(got, e["results"]):
            self.assertTrue(g.startswith(want))
        self.assertEqual(len(run["warnings"]), e["warnings"])
        self.assertEqual(len(results), len(e["results"]))  # stack stops at the refusal


if __name__ == "__main__":
    unittest.main()
