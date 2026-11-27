"""Tests for the GLM 5.3 orchestrator (external_coordinator.py) and the Inkling
T1 worker: the coordinator child runs on OpenRouter with no Agent tool, the
Claude rungs it dispatches go back to the user's own sign-in under the same
budget and spawn rules, a missing key falls back to the Opus coordinator, and
T1 evidence cards route to Inkling (Haiku when OpenRouter cannot take them).

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
ROUTER = PLUGIN / "skills" / "complexity-router" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROUTER))

import external_coordinator as ec  # noqa: E402
import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402
import route as router  # noqa: E402
import session_budget  # noqa: E402

# These tests start no real recall: a card run here must not reach this machine's Hindsight,
# and a budget level set in this machine's environment must not change the models under test.
os.environ["FEATURE_LADDER_RECALL"] = "off"
os.environ.pop("FEATURE_LADDER_BUDGET", None)

KEY = "sk-or-test-key-1234567890"


def result(text, turns=5, is_error=False):
    return json.dumps({"type": "result", "result": text, "num_turns": turns, "is_error": is_error,
                       "usage": {"input_tokens": 1000, "cache_read_input_tokens": 5000, "output_tokens": 300}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / ".feature-ladder").mkdir()
        self.calls = []
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        self.env_patch.start()
        for k in ("FEATURE_LADDER_WORKERS", "FEATURE_LADDER_WORKER_MODEL", "FEATURE_LADDER_NO_BS", ec.PLAN_ENV):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def spawner(self, stdout, code=0, stderr=""):
        def fake(cmd, cwd, env, stdin, timeout):
            sysfile = cmd[cmd.index("--append-system-prompt-file") + 1]
            self.calls.append({"cmd": cmd, "env": env, "stdin": stdin,
                               "system": Path(sysfile).read_text(encoding="utf-8")})
            return code, stdout, stderr
        return fake

    def call(self, fn, args, stdout, code=0, stderr="", key=(KEY, "test")):
        out = io.StringIO()
        with mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "get_key", return_value=key), \
                mock.patch.object(ew, "usage_usd", return_value=None), \
                mock.patch.object(ew, "_spawn", side_effect=self.spawner(stdout, code, stderr)), \
                contextlib.redirect_stdout(out):
            rc = fn(dict(args, **{"--cwd": str(self.root)}))
        return rc, out.getvalue()


class Coordinator(Base):
    def prompt(self):
        f = self.root / "prompt.md"
        f.write_text("Feature request from the user: save the map name\nConfirmed intake (binding): x.json\n",
                     encoding="utf-8")
        return {"--prompt-file": str(f)}

    def test_runs_on_glm_with_no_agent_tool_and_the_coordinator_instructions(self):
        ladder_state.write_json(self.root / ".feature-ladder" / "run.json", {"active": True, "slug": "map-name"})
        rc, out = self.call(ec.run, self.prompt(), result("Report: done. PR #3 merged."))
        self.assertEqual(rc, 0, out)
        self.assertIn("Report: done. PR #3 merged.", out)
        self.assertIn("external coordinator: z-ai/glm-5.3 via OpenRouter", out)
        c = self.calls[0]
        disallowed = c["cmd"][c["cmd"].index("--disallowedTools") + 1].split(",")
        self.assertIn("Agent", disallowed)
        self.assertIn("Task", disallowed)
        self.assertNotIn("Agent", c["cmd"][c["cmd"].index("--allowedTools") + 1])
        self.assertEqual(c["env"]["ANTHROPIC_BASE_URL"], "https://openrouter.ai/api")
        for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
            self.assertTrue(c["env"][f"ANTHROPIC_DEFAULT_{tier}_MODEL"].startswith("z-ai/glm-5.3"))
        self.assertEqual(c["env"]["BASH_MAX_TIMEOUT_MS"], ec.BASH_TIMEOUT_MS)
        self.assertNotIn(KEY, c["env"][ec.PLAN_ENV])           # the restore list never carries a secret
        self.assertIn("# The loop", c["system"])                 # agents/coordinator.md, frontmatter stripped
        self.assertIn("Without the Agent tool", c["system"])
        self.assertIn("You are the GLM 5.3 orchestrator", c["system"])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", c["system"])
        self.assertIn(str(PLUGIN), c["system"])
        self.assertIn("save the map name", c["stdin"])
        self.assertFalse(c["system"].startswith("---"))
        rows = (self.root / ".feature-ladder" / "runs" / "map-name" / "usage.jsonl").read_text(encoding="utf-8")
        row = json.loads(rows.strip().splitlines()[-1])
        self.assertEqual((row["key"], row["agent"], row["model"]),
                         ("external-coordinator", "coordinator (openrouter)", "z-ai/glm-5.3"))

    def test_no_key_or_a_refused_key_falls_back_to_opus(self):
        rc, out = self.call(ec.run, self.prompt(), "", key=(None, "no OPENROUTER_API_KEY"))
        self.assertEqual(rc, 3)
        self.assertIn("Opus 5.5 xhigh coordinator", out)
        self.assertEqual(self.calls, [])
        rc, out = self.call(ec.run, self.prompt(), result("401 invalid api key", turns=0, is_error=True), code=1)
        self.assertEqual(rc, 3, out)
        self.assertIn("nothing ran", out)

    def test_a_failure_after_work_is_not_a_fallback(self):
        rc, out = self.call(ec.run, self.prompt(), result("dispatched T1, then crashed", turns=9, is_error=True),
                            code=1, stderr="boom")
        self.assertEqual(rc, 1)
        self.assertIn("dispatched T1, then crashed", out)
        self.assertIn("pr_checkpoint.py status", out)

    def test_model_comes_from_the_orchestrator_block(self):
        cfg = self.home / ".claude" / "feature-ladder"
        cfg.mkdir(parents=True)
        (cfg / "providers.json").write_text(json.dumps({"orchestrator": {"model": "z-ai/glm-5.3-prime"}}),
                                            encoding="utf-8")
        rc, out = self.call(ec.run, self.prompt(), result("ok"))
        self.assertEqual(rc, 0)
        self.assertTrue(self.calls[0]["env"]["ANTHROPIC_MODEL"].startswith("z-ai/glm-5.3-prime"))


class PlanEnv(unittest.TestCase):
    def test_dispatch_gets_the_users_sign_in_back(self):
        cfg = ew.load_config(os.getcwd(), no_bs=False)  # before the environment is cleared (Windows needs USERPROFILE)
        user = {"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5", "PATH": os.environ.get("PATH", "")}
        with mock.patch.dict(os.environ, user, clear=True):
            child = ec.coordinator_env(cfg, KEY, "z-ai/glm-5.3")
        with mock.patch.dict(os.environ, child, clear=True):
            env = ec.plan_env()
        for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                  "CLAUDE_CODE_SUBAGENT_MODEL", "BASH_MAX_TIMEOUT_MS", ec.PLAN_ENV):
            self.assertNotIn(k, env)
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-5-5")  # the user's own value
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertEqual(env["FEATURE_LADDER_CHILD"], "1")
        self.assertNotIn(KEY, json.dumps(env))


class Dispatch(Base):
    def card(self, text="TASK T1: save the map name\nScope: server/map.ts\n"):
        f = self.root / "T1.md"
        f.write_text(text, encoding="utf-8")
        return f

    def test_implementer_runs_on_its_own_model_and_effort_with_the_contract(self):
        f = self.card()
        rc, out = self.call(ec.dispatch, {"--card": str(f), "--id": "T1", "--agent": "feature-ladder:implementer"},
                            result("TASK T1 - done\nChanged: server/map.ts\nRetained: none - nothing"))
        self.assertEqual(rc, 0, out)
        self.assertIn("plan worker: implementer on sonnet/medium", out)
        cmd = self.calls[0]["cmd"]
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "medium")
        self.assertEqual(cmd[cmd.index("--max-turns") + 1], "40")
        self.assertIn("Agent", cmd[cmd.index("--disallowedTools") + 1].split(","))
        self.assertIn("# Worker contract", self.calls[0]["system"])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", self.calls[0]["system"])
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", self.calls[0]["env"])
        rec = ladder_state.read_json(self.root / ".feature-ladder" / "evidence" / "T1-plan.json")
        self.assertEqual((rec["agent"], rec["provider"]), ("implementer", "Claude plan"))

    def test_read_only_agents_get_no_edit_tools(self):
        f = self.card()
        self.call(ec.dispatch, {"--card": str(f), "--id": "E1", "--agent": "evidence-collector"},
                  result("TASK E1 - done\nRetained: none - x"))
        cmd = self.calls[0]["cmd"]
        allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
        self.assertNotIn("Edit", allowed)
        self.assertNotIn("Write", allowed)
        self.assertIn("WebFetch", allowed)
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")

    def test_the_spawn_rules_decide_first(self):
        f = self.card()
        for agent in ("coordinator", "codex-reviewer"):
            rc, out = self.call(ec.dispatch, {"--card": str(f), "--id": "T1", "--agent": agent}, result("x"))
            self.assertEqual(rc, 4, agent)
        # Inside a run: the integrator needs two coding agents first, and a Fable rung needs its switch.
        ladder_state.write_json(self.root / ".feature-ladder" / "run.json",
                                {"active": True, "slug": "s", "started": "t", "budget": {"workers": 2}})
        rc, out = self.call(ec.dispatch, {"--card": str(f), "--id": "I1", "--agent": "integrator"}, result("x"))
        self.assertEqual(rc, 4)
        self.assertIn("integrator", out)
        rc, _ = self.call(ec.dispatch, {"--card": str(f), "--id": "S1", "--agent": "second-opinion"}, result("x"))
        self.assertEqual(rc, 4)
        self.assertEqual(self.calls, [])
        # A session budget that is spent refuses a plain implementer too.
        session_budget.set_size(self.root, "S")
        for i in range(2):
            self.call(ec.dispatch, {"--card": str(f), "--id": f"T{i}", "--agent": "implementer"},
                      result(f"TASK T{i} - done\nRetained: none - x"))
        rc, out = self.call(ec.dispatch, {"--card": str(f), "--id": "T9", "--agent": "implementer"}, result("x"))
        self.assertEqual(rc, 4, out)

    def test_not_signed_in_gives_the_slot_back(self):
        session_budget.set_size(self.root, "S")
        f = self.card()
        rc, out = self.call(ec.dispatch, {"--card": str(f), "--id": "T1", "--agent": "implementer"},
                            result("Not logged in. Please run /login", turns=0, is_error=True), code=1)
        self.assertEqual(rc, 3, out)
        self.assertEqual(session_budget.status(self.root)["used"], 0)


class OrchestratorChoice(unittest.TestCase):
    def test_glm_is_the_default_and_opus_the_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            o = session_budget.orchestrator(d)
        self.assertEqual((o["key"], o["model_id"], o["runner"], o["chosen"]),
                         ("glm", "z-ai/glm-5.3", "openrouter", False))
        self.assertEqual(session_budget.ASKED, ("glm", "fable"))
        self.assertEqual(session_budget.FALLBACK_ORCHESTRATOR, "opus")
        for v in ("GLM 5.3", "GLM 5.3 (Recommended)", "glm", "z-ai/glm-5.3"):
            self.assertEqual(session_budget.orchestrator_key(v), "glm", v)
        self.assertEqual(session_budget.orchestrator_key("Opus 5.5 xhigh"), "opus")

    def test_glm_never_points_at_flash(self):
        self.assertNotIn("flash", session_budget.ORCHESTRATORS["glm"]["model_id"])
        self.assertEqual(ec.ORCH_DEFAULTS["model"], session_budget.ORCHESTRATORS["glm"]["model_id"])


class InklingT1(unittest.TestCase):
    def test_research_routes_to_inkling(self):
        r = router.route({"kind": "research", "ambiguity": 1})
        self.assertEqual((r["agent"], r["provider"], r["model"]), ("evidence-collector", "inkling", "haiku"))

    def test_openrouter_takes_t1_and_silk_does_not(self):
        r = router.route({"kind": "research"})
        with tempfile.TemporaryDirectory() as d:
            cfg = ew.load_config(d, no_bs=False)
            self.assertTrue(ew.takes(cfg, r))
            self.assertEqual(ew.model_for(cfg, "evidence-collector"), "thinkingmachines/inkling-2-small")  # standard level
            jev = json.loads(json.dumps(cfg))
            jev["openrouter"]["models"] = dict(ew.PRESETS["jev"])
            self.assertEqual(ew.model_for(jev, "evidence-collector"), "thinkingmachines/inkling-2-small")
            silk = ew.load_config(d, no_bs=True)
            self.assertFalse(ew.takes(silk, r))                   # no Silk model for T1: Haiku
            self.assertIsNone(ew.model_for(silk, "evidence-collector"))

    def test_the_inkling_worker_is_read_only_with_the_web(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            card = root / "E1.md"
            card.write_text("TASK E1: what does the Firestore batch API need\n", encoding="utf-8")
            seen = {}

            def fake(cmd, cwd, env, stdin, timeout):
                seen.update(cmd=cmd, env=env, stdin=stdin,
                            system=Path(cmd[cmd.index("--append-system-prompt-file") + 1]).read_text(encoding="utf-8"))
                return 0, result("TASK E1 - done\nClaims: x\nRetained: none - x"), ""
            out = io.StringIO()
            with mock.patch.dict(os.environ, {"HOME": d, "USERPROFILE": d}), \
                    mock.patch.object(ew, "claude_bin", return_value="claude"), \
                    mock.patch.object(ew, "get_key", return_value=(KEY, "test")), \
                    mock.patch.object(ew, "usage_usd", return_value=None), \
                    mock.patch.object(ew, "_spawn", side_effect=fake), \
                    mock.patch.object(session_budget, "check", return_value=(True, "")), \
                    contextlib.redirect_stdout(out):
                rc = ew.run({"--card": str(card), "--id": "E1", "--rung": "evidence-collector", "--cwd": d})
            self.assertEqual(rc, 0, out.getvalue())
            cmd = seen["cmd"]
            allowed = cmd[cmd.index("--allowedTools") + 1].split(",")
            self.assertEqual(sorted(allowed), sorted(ew.TOOLS_READ_ONLY.split(",")))
            self.assertIn("Edit", cmd[cmd.index("--disallowedTools") + 1])
            self.assertTrue(seen["env"]["ANTHROPIC_MODEL"].startswith("thinkingmachines/inkling"))
            self.assertIn("evidence-collector rung", seen["system"])   # its own instructions, not just the contract
            self.assertIn("read only, no edits", seen["stdin"])
            self.assertIn("web allowed", seen["stdin"])


if __name__ == "__main__":
    unittest.main()
