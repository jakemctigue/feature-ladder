"""A small Firestore-write feature must plan as one cheap worker and nothing else.

Pins the ceilings behind the "at most ~4% of a week" target for the run that
used 17 agents / 470 turns: one implementer on GLM or Sonnet medium, at most
one mechanic, no domain expert, no second opinion, no Codex, no scribe, a
coordinator budget of at most 12 turns and a hard cap of at most 24. Also
covers the session subagent budget (S 1-2, M 3-4, L up to 8) and the spawn
hook that enforces it.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
ROUTER = PLUGIN / "skills" / "complexity-router" / "scripts"
LADDER = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(LADDER))

import intake  # noqa: E402
import route as router  # noqa: E402
import session_budget  # noqa: E402

REQUEST = "save the maps workflow status to firestore"
FABLE_OR_REVIEW = {"second-opinion", "deep-verifier", "architect-escalation", "ultra-investigator",
                   "codex-reviewer", "memory-scribe", "domain-expert", "domain-expert-high"}


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(tmp):
    root = Path(tmp)
    git(root, "init", "-q")
    (root / "src").mkdir()
    (root / "src" / "maps_workflow.py").write_text("def status(db, wf):\n    return wf.state\n", encoding="utf-8")
    (root / "src" / "firestore_client.py").write_text("from google.cloud import firestore\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_maps_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / ".firebaserc").write_text(json.dumps({"projects": {"default": "bossforge-prod"}}), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    return root


def scoper_fields(scan, **over):
    """What a strict scoper settles for a one-function Firestore write."""
    fields = dict(scan["fields"])
    vals = {"kind": "code", "files": 2, "loc": 40, "modules": 1, "persistence": True, "irreversible": False,
            "cross_system": False, "security": False, "concurrency": False, "public_api": False,
            "new_dependency": False, "no_tests": False, "ambiguity": 1, "novelty": 0, "blast_radius": 1,
            "design_agreed": False}
    vals.update(over)
    for k, v in vals.items():
        fields[k] = {"value": v, "confidence": "high", "evidence": ["test fixture"]}
    return fields


class SmallFirestoreWrite(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(self.tmp.name)
        self.scan = intake.scan(REQUEST, str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def plan(self, fields=None, decisions=None):
        prop = dict(self.scan, fields=fields or scoper_fields(self.scan))
        dec = intake.decide(prop)
        answers = {"decisions": decisions or {"session_agents": "Small (1-2 subagents)"}}
        return dec, intake.apply(prop, answers)

    def assert_one_cheap_worker(self, final, size="S"):
        r, b = final["route"], final["budget"]
        self.assertEqual(r["agent"], "implementer")
        self.assertEqual((r["model"], r["effort"]), ("sonnet", "medium"))
        self.assertIn(r["provider"], ("glm", "claude"))
        self.assertEqual(r["size"], size)
        self.assertFalse(r["design_first"])
        # S: exactly one worker. M allows a second only for two independent file sets.
        self.assertEqual(b["workers"], 1 if size == "S" else 2)
        self.assertLessEqual(b["mechanic_max"], 1)
        for k in ("design", "codex", "adversarial", "scribe", "second_opinion", "fable", "independent_check"):
            self.assertFalse(b[k], k)
        self.assertLessEqual(b["coordinator_turns"], 12 if size == "S" else 20)
        self.assertFalse(final["codex"]["review"])

    def test_strict_scoper_plan_is_one_glm_worker(self):
        dec, final = self.plan()
        self.assert_one_cheap_worker(final)
        self.assertEqual(final["route"]["provider"], "glm")  # score 5: T2 small -> GLM tier
        self.assertEqual(final["session_agents"], "S")
        self.assertEqual(final["outside_policy"], "none")  # code only unless the user picks more

    def test_cautious_flags_still_plan_one_sonnet_medium_worker(self):
        # The scoper over-flags (cross_system, irreversible, a 3-file guess): size M by
        # score, still Sonnet medium, no design, no review, no Fable.
        fields = scoper_fields(self.scan, files=3, loc=80, modules=2, cross_system=True, irreversible=True)
        _, final = self.plan(fields)
        self.assert_one_cheap_worker(final, size="M")
        self.assertEqual(final["route"]["provider"], "claude")

    def test_short_request_is_not_a_hard_flag(self):
        self.assertLess(len(REQUEST.split()), 8)
        self.assertEqual(self.scan["fields"]["ambiguity"]["value"], 1)
        self.assertEqual(router.describe(REQUEST)["ambiguity"], 1)

    def test_hard_flag_adds_one_design_step_and_nothing_else(self):
        _, final = self.plan(scoper_fields(self.scan, ambiguity=2))
        r, b = final["route"], final["budget"]
        self.assertEqual(r["size"], "S")  # by score (7), not forced to L
        self.assertTrue(r["design_first"] and b["design"])
        self.assertEqual(r["agent"], "implementer")
        for k in ("codex", "scribe", "second_opinion", "fable"):
            self.assertFalse(b[k], k)

    def test_session_budget_is_asked_once_up_front(self):
        prop = dict(self.scan, fields=scoper_fields(self.scan))
        dec = intake.decide(prop)
        ids = [d["id"] for d in dec["decisions"]]
        self.assertEqual(ids[0], "session_agents")
        labels = " ".join(o["label"] for o in dec["decisions"][0]["options"])
        for want in ("1-2", "3-4", "up to 8"):
            self.assertIn(want, labels)
        # `intake.py apply` stores the answer; the next feature in the session is not asked again.
        pf, af = self.root / "p.json", self.root / "a.json"
        pf.write_text(json.dumps(prop), encoding="utf-8")
        af.write_text(json.dumps({"decisions": {"session_agents": "Medium (3-4 subagents)"}}), encoding="utf-8")
        r = subprocess.run([sys.executable, str(ROUTER / "intake.py"), "apply", str(pf), str(af)],
                           cwd=str(self.root), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(session_budget.status(self.root)["max"], 4)
        dec2 = intake.decide(dict(self.scan, fields=scoper_fields(self.scan)))
        self.assertNotIn("session_agents", [d["id"] for d in dec2["decisions"]])

    def test_router_never_picks_fable(self):
        for score_fields in ({}, {"files": 8, "loc": 400, "modules": 3, "security": True, "concurrency": True,
                                  "cross_system": True, "novelty": 3, "blast_radius": 3, "ambiguity": 3}):
            r = router.route(dict({"kind": "code"}, **score_fields))
            self.assertNotEqual(r["model"], "fable")
            self.assertNotEqual(r["agent"], "escalate")


class CoordinatorCeilings(unittest.TestCase):
    def front(self, name):
        text = (PLUGIN / "agents" / f"{name}.md").read_text(encoding="utf-8")
        return text.split("---")[1]

    def test_coordinator_hard_cap_and_no_preloads(self):
        fm = self.front("coordinator")
        self.assertLessEqual(int(re.search(r"maxTurns:\s*(\d+)", fm).group(1)), 24)
        self.assertNotIn("skills:", fm)
        self.assertLessEqual(max(intake.COORDINATOR_TURNS.values()), 24)

    def test_scoper_has_no_preloads(self):
        self.assertNotIn("skills:", self.front("scoper"))


class SpawnHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_repo(self.tmp.name)
        (self.root / ".feature-ladder").mkdir()
        session_budget.set_size(self.root, "S")

    def tearDown(self):
        self.tmp.cleanup()

    def start_run(self, budget=None, switches=()):
        run = {"active": True, "slug": "maps", "budget": budget or {"size": "S", "workers": 1, "design": False},
               "switches": list(switches)}
        (self.root / ".feature-ladder" / "run.json").write_text(json.dumps(run), encoding="utf-8")

    def spawn(self, agent, model=None):
        payload = {"tool_name": "Agent", "cwd": str(self.root),
                   "tool_input": {"subagent_type": f"feature-ladder:{agent}", "model": model, "prompt": "x"}}
        r = subprocess.run([sys.executable, str(LADDER / "session_budget.py"), "--pretooluse"],
                           input=json.dumps(payload), capture_output=True, text=True, timeout=60)
        return not r.stdout.strip(), r.stdout

    def test_feature_run_denies_reviews_scribes_experts_and_fable(self):
        self.start_run()
        for agent in sorted(FABLE_OR_REVIEW):
            ok, out = self.spawn(agent)
            self.assertFalse(ok, agent)
            self.assertIn("deny", out)
        ok, _ = self.spawn("general-purpose", model="fable")
        self.assertFalse(ok)
        self.assertEqual(session_budget.status(self.root)["used"], 0)

    def test_one_worker_one_mechanic_and_the_coordinator_is_free(self):
        self.start_run()
        self.assertTrue(self.spawn("coordinator", "fable")[0])
        self.assertTrue(self.spawn("scoper", "sonnet")[0])
        self.assertTrue(self.spawn("implementer", "sonnet")[0])
        self.assertTrue(self.spawn("mechanic", "haiku")[0])
        self.assertFalse(self.spawn("mechanic", "haiku")[0])  # one mechanic per feature
        self.assertEqual(session_budget.status(self.root)["used"], 2)

    def test_session_budget_is_a_hard_stop(self):
        self.start_run({"size": "M", "workers": 2, "design": False})
        self.assertTrue(self.spawn("implementer")[0])
        self.assertTrue(self.spawn("implementer")[0])
        ok, out = self.spawn("implementer")
        self.assertFalse(ok)
        self.assertIn("budget", out)

    def test_external_workers_count_too(self):
        self.start_run({"size": "M", "workers": 2, "design": False})
        self.assertTrue(session_budget.check(self.root, "implementer", "z-ai/glm-5.3-flash:nitro", via="external")[0])
        self.assertTrue(session_budget.check(self.root, "implementer", "z-ai/glm-5.3-flash:nitro", via="external")[0])
        self.assertFalse(session_budget.check(self.root, "mechanic", "z-ai/glm-5.3-flash:nitro", via="external")[0])

    def test_design_step_only_when_planned_and_switches_unlock_fable(self):
        self.start_run({"size": "M", "workers": 1, "design": True}, switches=["second_opinion"])
        session_budget.set_size(self.root, "L")
        self.assertTrue(self.spawn("domain-expert", "opus")[0])
        self.assertFalse(self.spawn("domain-expert", "opus")[0])
        self.assertTrue(self.spawn("second-opinion", "fable")[0])
        self.assertFalse(self.spawn("deep-verifier", "fable")[0])

    def test_wrap_runs_reviewer_and_scribe_outside_a_run(self):
        self.assertTrue(self.spawn("codex-reviewer", "haiku")[0])
        self.assertTrue(self.spawn("memory-scribe", "haiku")[0])
        self.assertFalse(self.spawn("implementer")[0])  # S = 2, spent

    def test_new_session_resets_the_budget(self):
        payload = json.dumps({"source": "startup", "session_id": "s2", "cwd": str(self.root)})
        env = {k: v for k, v in os.environ.items() if k != "FEATURE_LADDER_CHILD"}
        subprocess.run([sys.executable, str(LADDER / "session_budget.py"), "--session-start"],
                       input=payload, text=True, timeout=60, env=env)
        self.assertEqual(session_budget.status(self.root)["budget"], "unset")

    def test_external_child_session_does_not_reset_it(self):
        payload = json.dumps({"source": "startup", "session_id": "child", "cwd": str(self.root)})
        subprocess.run([sys.executable, str(LADDER / "session_budget.py"), "--session-start"],
                       input=payload, text=True, timeout=60, env=dict(os.environ, FEATURE_LADDER_CHILD="1"))
        self.assertEqual(session_budget.status(self.root)["budget"], "S")


class ProductionWritesWithoutFable(unittest.TestCase):
    def test_risky_write_is_left_for_the_user_without_the_switch(self):
        from irreversible import gate
        run = {"active": True, "outside_policy": "all",
               "environments": {"production": ["bossforge-prod"], "nonproduction": []}}
        ok, reason, _ = gate("firebase firestore:delete /users --project bossforge-prod", run)
        self.assertFalse(ok)
        self.assertIn("left", reason)
        self.assertNotIn("second-opinion (fable", reason)
        run["switches"] = ["second_opinion"]
        ok, reason, _ = gate("firebase firestore:delete /users --project bossforge-prod", run)
        self.assertFalse(ok)
        self.assertIn("VERDICT", reason)


if __name__ == "__main__":
    unittest.main()
