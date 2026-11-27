"""Tests for openclaude_runner.py: Claude starts many agents on OpenRouter
through OpenClaude (`oc`). A fanout runs several cards at once, one OpenClaude
agent each, every card charged to the session budget first; a team is one
OpenClaude lead whose Agent tool starts up to N OpenRouter agents, held to N
and to the session budget by a PreToolUse hook. The child never sees the
Claude Code session it came from, and never bills the Claude plan.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402
import openclaude_runner as ocr  # noqa: E402
import session_budget  # noqa: E402

# These tests start no real recall: a card run here must not reach this machine's Hindsight,
# and a budget level set in this machine's environment must not change the models under test.
os.environ["FEATURE_LADDER_RECALL"] = "off"
os.environ.pop("FEATURE_LADDER_BUDGET", None)

KEY = "sk-or-test-key-1234567890"


def result(text, turns=4, is_error=False):
    return json.dumps({"type": "result", "result": text, "num_turns": turns, "is_error": is_error,
                       "usage": {"input_tokens": 0, "output_tokens": 0}})


def packet(tid, state="done"):
    return (f"TASK {tid} - {state}\nChanged: a.py:1\nChecks run: x -> exit 0\nClaims: ok\nRisks: none\n"
            "Memory candidates: none\nRetained: none - test")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        (self.root / ".feature-ladder").mkdir()
        self.calls = []
        self.lock = threading.Lock()
        self.env_patch = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        self.env_patch.start()
        for k in ("FEATURE_LADDER_WORKERS", "FEATURE_LADDER_WORKER_MODEL", "FEATURE_LADDER_NO_BS"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def card(self, tid, scope="a.py"):
        f = self.root / f"{tid}.md"
        f.write_text(f"TASK {tid}: fix it\nScope: {scope}\nDone when: python -c 1\n", encoding="utf-8")
        return str(f)

    def spawner(self, reply):
        """reply(stdin) -> (code, stdout, stderr); every call is recorded."""
        def fake(cmd, cwd, env, stdin, timeout):
            files = {}
            for flag in ("--append-system-prompt-file", "--settings"):
                if flag in cmd:
                    p = Path(cmd[cmd.index(flag) + 1])
                    files[flag] = p.read_text(encoding="utf-8") if p.is_file() else None
            with self.lock:
                self.calls.append({"cmd": cmd, "env": env, "stdin": stdin, "files": files, "timeout": timeout})
            return reply(stdin)
        return fake

    def run_cli(self, argv, reply, key=(KEY, "test"), oc=("openclaude",), usage=(None, None)):
        out = io.StringIO()
        usage_it = iter(usage)
        with mock.patch.object(ocr, "openclaude_cmd", return_value=list(oc) if oc else None), \
                mock.patch.object(ew, "get_key", return_value=key), \
                mock.patch.object(ew, "usage_usd", side_effect=lambda *a: next(usage_it, None)), \
                mock.patch.object(ew, "_spawn", side_effect=self.spawner(reply)), \
                contextlib.redirect_stdout(out):
            rc = ocr.main(argv + ["--cwd", str(self.root)])
        return rc, out.getvalue()


class Fanout(Base):
    def test_cards_run_together_and_packets_come_back_in_order(self):
        def reply(stdin):
            tid = stdin.split()[1].rstrip(":")
            return 0, result(packet(tid)), ""
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1", "--rung", "mechanic",
                                "--card", self.card("T2"), "--id", "T2", "--rung", "implementer"], reply,
                               usage=(1.0, 1.25))
        self.assertEqual(rc, 0, out)
        self.assertLess(out.index("TASK T1 - done"), out.index("TASK T2 - done"))
        self.assertIn("2 of 2 cards ran", out)
        self.assertIn("$0.2500", out)
        self.assertEqual(len(self.calls), 2)
        for c in self.calls:
            cmd = c["cmd"]
            self.assertEqual(cmd[0], "openclaude")
            self.assertIn("-p", cmd)
            self.assertIn("--strict-mcp-config", cmd)
            self.assertIn("Agent", cmd[cmd.index("--disallowedTools") + 1].split(","))
            self.assertNotIn("Agent", cmd[cmd.index("--tools") + 1].split(","))
            self.assertIn("Output contract", c["stdin"])  # the card carries the contract inline
            self.assertIn("Bounds:", c["stdin"])
            self.assertIn("Evidence Packet", c["files"]["--append-system-prompt-file"])
        for tid in ("T1", "T2"):
            rec = ladder_state.read_json(self.root / ".feature-ladder" / "evidence" / f"{tid}-openclaude.json")
            self.assertEqual(rec["returned"], 0)
            self.assertEqual(rec["provider"], "OpenRouter (OpenClaude)")

    def test_child_gets_openrouter_and_never_the_claude_session(self):
        os.environ.update({"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli", "ANTHROPIC_API_KEY": "sk-ant-x",
                           "ANTHROPIC_BASE_URL": "https://x", "OPENAI_API_KEY": "sk-openai-other",
                           "OPENGATEWAY_API_KEY": "og"})
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1", "--rung", "implementer"],
                               lambda s: (0, result(packet("T1")), ""))
        self.assertEqual(rc, 0, out)
        env = self.calls[0]["env"]
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                  "OPENGATEWAY_API_KEY"):
            self.assertNotIn(k, env)
        self.assertEqual(env["CLAUDE_CODE_USE_OPENAI"], "1")
        self.assertEqual(env["OPENAI_BASE_URL"], "https://openrouter.ai/api/v1")
        self.assertEqual(env["OPENAI_API_KEY"], KEY)
        self.assertEqual(env["OPENAI_MODEL"], "z-ai/glm-5.3-flash:nitro")
        self.assertEqual(env["FEATURE_LADDER_CHILD"], "1")

    def test_models_follow_the_rung(self):
        rc, out = self.run_cli(["fanout",
                                "--card", self.card("T1"), "--id", "T1", "--rung", "evidence-collector",
                                "--card", self.card("T2"), "--id", "T2", "--rung", "implementer-high",
                                "--card", self.card("T3"), "--id", "T3", "--rung", "general",
                                "--card", self.card("T4"), "--id", "T4", "--rung", "implementer",
                                "--model", "moonshotai/kimi-k3"],
                               lambda s: (0, result(packet(s.split()[1].rstrip(":"))), ""))
        self.assertEqual(rc, 0, out)
        by_id = {c["stdin"].split()[1].rstrip(":"): c for c in self.calls}
        model = {t: c["cmd"][c["cmd"].index("--model") + 1] for t, c in by_id.items()}
        self.assertEqual(model, {"T1": "thinkingmachines/inkling-2-small", "T2": "z-ai/glm-5.3", "T3": "z-ai/glm-5.3",
                                 "T4": "moonshotai/kimi-k3"})
        t1 = by_id["T1"]["cmd"]
        self.assertIn("WebFetch", t1[t1.index("--tools") + 1])  # read only, web allowed
        self.assertIn("Edit", t1[t1.index("--disallowedTools") + 1])

    def test_budget_decides_before_anything_starts(self):
        session_budget.set_size(str(self.root), "S")  # 2 subagents
        rc, out = self.run_cli(["fanout"] + sum((["--card", self.card(t), "--id", t, "--rung", "implementer"]
                                                 for t in ("T1", "T2", "T3")), []),
                               lambda s: (0, result(packet(s.split()[1].rstrip(":"))), ""))
        self.assertEqual(rc, 4, out)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("TASK T3 - blocked", out)
        self.assertIn("subagent budget is spent", out)
        spawned = session_budget.load(str(self.root))["spawned"]
        self.assertEqual([e["via"] for e in spawned], ["openclaude", "openclaude"])

    def test_one_failure_does_not_lose_the_other_packet(self):
        def reply(stdin):
            if "TASK T2" in stdin:
                return 1, "", "boom: model crashed"
            return 0, result(packet("T1")), ""
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1",
                                "--card", self.card("T2"), "--id", "T2"], reply)
        self.assertEqual(rc, 1)
        self.assertIn("TASK T1 - done", out)
        self.assertIn("TASK T2 - partial", out)
        self.assertIn("boom", out)

    def test_refused_key_is_unavailable_and_refunds_the_slot(self):
        session_budget.set_size(str(self.root), "S")
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1"],
                               lambda s: (1, result("401 invalid api key", turns=0, is_error=True), ""))
        self.assertEqual(rc, 3, out)
        self.assertIn("TASK T1 - blocked", out)
        self.assertEqual(session_budget.load(str(self.root))["spawned"], [])

    def test_no_openclaude_means_unavailable_with_install_hint(self):
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1"],
                               lambda s: (0, "", ""), oc=None)
        self.assertEqual(rc, 3)
        self.assertEqual(self.calls, [])
        self.assertIn("npm install -g @gitlawb/openclaude", out)

    def test_no_bs_runs_stay_on_external_worker(self):
        os.environ["FEATURE_LADDER_NO_BS"] = "1"
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1"], lambda s: (0, "", ""))
        self.assertEqual(rc, 3)
        self.assertEqual(self.calls, [])
        self.assertIn("external_worker.py", out)

    def test_key_never_reaches_the_output(self):
        rc, out = self.run_cli(["fanout", "--card", self.card("T1"), "--id", "T1"],
                               lambda s: (0, result(packet("T1") + f"\nleaked {KEY}"), ""))
        self.assertNotIn(KEY, out)
        rec = (self.root / ".feature-ladder" / "evidence" / "T1-openclaude.json").read_text(encoding="utf-8")
        self.assertNotIn(KEY, rec)

    def test_bad_arguments(self):
        for argv in (["fanout"], ["fanout", "--id", "T1"], ["fanout", "--card", self.card("T1"), "--rung", "fable"],
                     ["fanout", "--card", self.card("T1"), "--id", "X", "--card", self.card("T2"), "--id", "X"],
                     ["fanout", "--card", self.card("T1"), "--bogus", "1"]):
            with contextlib.redirect_stderr(io.StringIO()):
                rc, _ = self.run_cli(argv, lambda s: (0, "", ""))
            self.assertEqual(rc, 1, argv)
        self.assertEqual(self.calls, [])


class Team(Base):
    def prompt(self):
        f = self.root / "goal.md"
        f.write_text("Fix both bugs with two agents.\n", encoding="utf-8")
        return str(f)

    def test_lead_gets_the_agent_tool_routes_and_the_gate(self):
        rc, out = self.run_cli(["team", "--prompt-file", self.prompt(), "--agents", "3"],
                               lambda s: (0, result("Done: both fixed."), ""), usage=(2.0, 2.5))
        self.assertEqual(rc, 0, out)
        self.assertIn("Done: both fixed.", out)
        self.assertIn("$0.5000", out)
        cmd = self.calls[0]["cmd"]
        self.assertIn("Agent", cmd[cmd.index("--tools") + 1].split(","))
        self.assertEqual(cmd[cmd.index("--model") + 1], "z-ai/glm-5.3")
        agents = json.loads(cmd[cmd.index("--agents") + 1])
        self.assertEqual(set(agents), {"mechanic", "implementer", "evidence-collector"})
        self.assertNotIn("Agent", agents["implementer"]["tools"])
        settings = json.loads(self.calls[0]["files"]["--settings"])
        self.assertEqual(settings["agentRouting"]["evidence-collector"], "fl-evidence-collector")
        self.assertEqual(settings["agentModels"]["fl-evidence-collector"]["model"], "thinkingmachines/inkling-2-small")
        self.assertEqual(settings["agentModels"]["fl-implementer"]["model"], "z-ai/glm-5.3-flash:nitro")
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "Agent|Task")
        self.assertIn("openclaude_runner.py\" gate --team", hook["hooks"][0]["command"])
        self.assertIn("at most 3 in all", self.calls[0]["files"]["--append-system-prompt-file"])

    def test_agents_is_required_and_capped(self):
        for n in (None, "0", "9"):
            argv = ["team", "--prompt-file", self.prompt()] + (["--agents", n] if n else [])
            with contextlib.redirect_stderr(io.StringIO()):
                rc, _ = self.run_cli(argv, lambda s: (0, "", ""))
            self.assertEqual(rc, 1, n)
        self.assertEqual(self.calls, [])

    def test_unavailable_without_openclaude(self):
        rc, out = self.run_cli(["team", "--prompt-file", self.prompt(), "--agents", "2"],
                               lambda s: (0, "", ""), oc=None)
        self.assertEqual(rc, 3)
        self.assertIn("unavailable", out)


class Gate(Base):
    def payload(self, agent="implementer"):
        return {"tool_name": "Agent", "tool_input": {"subagent_type": agent, "prompt": "x"}, "cwd": str(self.root)}

    def test_team_cap_holds(self):
        state = self.root / ".feature-ladder" / "openclaude" / "team.json"
        ladder_state.write_json(state, {"max": 2, "spawned": []})
        self.assertIsNone(ocr.gate_decision(self.payload(), state, str(self.root)))
        self.assertIsNone(ocr.gate_decision(self.payload("mechanic"), state, str(self.root)))
        denied = ocr.gate_decision(self.payload(), state, str(self.root))
        self.assertEqual(denied["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("2 agents", denied["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(len(ladder_state.read_json(state)["spawned"]), 2)

    def test_session_budget_also_holds(self):
        session_budget.set_size(str(self.root), "S")
        state = self.root / ".feature-ladder" / "openclaude" / "team.json"
        ladder_state.write_json(state, {"max": 8, "spawned": []})
        for _ in range(2):
            self.assertIsNone(ocr.gate_decision(self.payload(), state, str(self.root)))
        denied = ocr.gate_decision(self.payload(), state, str(self.root))
        self.assertIn("subagent budget is spent", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_other_tools_pass(self):
        self.assertIsNone(ocr.gate_decision({"tool_name": "Bash"}, self.root / "none.json", str(self.root)))

    def test_cli_gate_denies_when_state_is_missing_cap(self):
        state = self.root / ".feature-ladder" / "openclaude" / "team.json"
        ladder_state.write_json(state, {"max": 0})
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(self.payload()))), contextlib.redirect_stdout(out):
            rc = ocr.main(["gate", "--team", str(state), "--cwd", str(self.root)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny")


class Command(unittest.TestCase):
    def test_windows_npm_shim_runs_the_bin_script_with_node(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "openclaude.cmd"
            shim.write_text("@echo off\n", encoding="utf-8")
            js = Path(d) / "node_modules" / "@gitlawb" / "openclaude" / "bin" / "openclaude"
            js.parent.mkdir(parents=True)
            js.write_text("// bin\n", encoding="utf-8")
            which = {"openclaude": str(shim), "node": "C:/node/node.exe"}
            with mock.patch.object(ocr.shutil, "which", side_effect=lambda n: which.get(n)):
                self.assertEqual(ocr.openclaude_cmd(), ["C:/node/node.exe", str(js)])

    def test_missing(self):
        with mock.patch.object(ocr.shutil, "which", return_value=None):
            self.assertIsNone(ocr.openclaude_cmd())

    def test_endpoint_is_openai_compatible(self):
        self.assertEqual(ocr.base_url({"openrouter": {"base_url": "https://openrouter.ai/api"}}),
                         "https://openrouter.ai/api/v1")
        self.assertEqual(ocr.base_url({"openrouter": {"base_url": "https://x/v1/"}}), "https://x/v1")


if __name__ == "__main__":
    unittest.main()
