"""Tests for --no-bs: the cheap tier on Silk Compute, only when the run asked for it.

- Off by default; on only for an active run whose switches carry no_bs (or
  FEATURE_LADDER_NO_BS=1), never from a saved providers.json preset.
- Each tier gets a capable Silk model: DeepSeek V4.1 Flash 1017 Fast for T0,
  DeepSeek V4.1 Flash 1017 for small T2, Kimi K3.1 Pro for the intermediate T2
  band (score 7-14); T3 and up stay on Claude.
- The key comes from SILK_API_KEY, else the repo's .env; cost from /v1/balance.
- A model that is busy before doing any work hands the card to its fallback once;
  both busy means exit 3, so the card runs on its Claude rung.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(PLUGIN / "skills" / "complexity-router" / "scripts"))

import external_worker as ew  # noqa: E402
import intake  # noqa: E402
import session_budget  # noqa: E402

KIMI = "kimi-k3.1-pro-uncensored"
DS = "deepseek-v4.1-flash-1017-uncensored"
DS_FAST = "deepseek-v4.1-flash-1017-fast-uncensored"
HY4 = "hy4-uncensored"
CARD = "TASK T1: wire the map name\nScope: a.py\nDone when: pytest\n"
GOOD = json.dumps({"type": "result", "num_turns": 3, "usage": {},
                   "result": "TASK T1 - done\nChanged: a.py\nRetained: none - n/a"})
BUSY = (1, "", 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"The model is busy right now."}}')


class Temp(unittest.TestCase):
    def setUp(self):
        home = self.tmpdir()
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("FEATURE_LADDER_NO_BS", "FEATURE_LADDER_WORKERS", "FEATURE_LADDER_WORKER_MODEL",
                              "SILK_API_KEY", "OPENROUTER_API_KEY")}
        clean.update(HOME=str(home), USERPROFILE=str(home))
        p = mock.patch.dict(os.environ, clean, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def tmpdir(self):
        d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def repo(self, switches=None, active=True, providers=None):
        d = self.tmpdir()
        fl = d / ".feature-ladder"
        (fl / "runs" / "s").mkdir(parents=True)
        run = {"slug": "s", "active": active}
        if switches is not None:
            run["switches"] = switches
        (fl / "run.json").write_text(json.dumps(run), encoding="utf-8")
        if providers:
            (fl / "providers.json").write_text(json.dumps(providers), encoding="utf-8")
        return d


class Switch(Temp):
    def test_off_by_default_and_without_the_switch(self):
        for root in (self.repo(), self.repo(switches=["second_opinion"])):
            cfg = ew.load_config(str(root))
            self.assertFalse(cfg["no_bs"])
            self.assertEqual(cfg["openrouter"]["base_url"], "https://openrouter.ai/api")
            self.assertNotIn("reach", cfg["openrouter"])

    def test_a_saved_preset_cannot_turn_it_on(self):
        cfg = ew.load_config(str(self.repo(providers={"openrouter": {"preset": "no-bs"}})))
        self.assertFalse(cfg["no_bs"])
        self.assertEqual(cfg["openrouter"]["base_url"], "https://openrouter.ai/api")

    def test_the_run_switch_moves_the_tier_to_silk(self):
        cfg = ew.load_config(str(self.repo(switches=["no_bs"])))
        p = cfg["openrouter"]
        self.assertTrue(cfg["no_bs"])
        self.assertEqual(p["base_url"], "https://api.silkcompute.com")
        self.assertEqual(p["key_env"], "SILK_API_KEY")
        self.assertEqual(p["models"]["implementer-mid"], KIMI)

    def test_a_finished_run_does_not_keep_it(self):
        self.assertFalse(ew.load_config(str(self.repo(switches=["no_bs"], active=False)))["no_bs"])

    def test_env_turns_it_on_for_a_manual_run(self):
        with mock.patch.dict(os.environ, {"FEATURE_LADDER_NO_BS": "1"}):
            self.assertTrue(ew.load_config(str(self.repo()))["no_bs"])

    def test_the_run_limits_carry_over_and_a_silk_block_adjusts_a_tier(self):
        root = self.repo(switches=["no_bs"], providers={"openrouter": {"timeout": 540},
                                                         "silk": {"models": {"mechanic": "glm-5.3-flash-uncensored"}}})
        p = ew.load_config(str(root))["openrouter"]
        self.assertEqual(p["timeout"], 540)
        self.assertEqual(p["models"]["mechanic"], "glm-5.3-flash-uncensored")
        self.assertEqual(p["models"]["implementer-mid"], KIMI)

    def test_workers_claude_gives_way_to_the_explicit_switch(self):
        root = self.repo(switches=["no_bs"], providers={"workers": "claude"})
        self.assertEqual(ew.load_config(str(root))["workers"], "auto")


class Tiers(Temp):
    def cfg(self):
        return ew.load_config(str(self.repo(switches=["no_bs"])))

    def test_each_tier_gets_its_capable_model(self):
        cfg = self.cfg()
        self.assertEqual(ew.model_for(cfg, "mechanic", score=2), DS_FAST)
        self.assertEqual(ew.model_for(cfg, "implementer", "medium", score=5), DS)
        self.assertEqual(ew.model_for(cfg, "implementer", "medium", score=7), KIMI)
        self.assertEqual(ew.model_for(cfg, "implementer", "medium", score="14"), KIMI)
        self.assertEqual(ew.model_for(cfg, "implementer", "medium"), DS)  # no score: the small tier

    def test_the_intermediate_band_is_taken_and_t3_stays_on_claude(self):
        cfg = self.cfg()
        route = {"agent": "implementer", "effort": "medium", "provider": "claude"}
        self.assertTrue(ew.takes(cfg, dict(route, score=10)))
        self.assertFalse(ew.takes(cfg, dict(route, score=15)))
        self.assertFalse(ew.takes(cfg, {"agent": "implementer", "effort": "high", "provider": "claude", "score": 16}))
        self.assertFalse(ew.takes(cfg, {"agent": "senior-implementer", "effort": "medium", "provider": "claude",
                                        "score": 9}))
        self.assertTrue(ew.takes(cfg, {"agent": "mechanic", "effort": "low", "provider": "glm", "score": 1}))

    def test_without_the_switch_the_glm_tier_is_unchanged(self):
        cfg = ew.load_config(str(self.repo()))
        self.assertFalse(ew.takes(cfg, {"agent": "implementer", "effort": "medium", "provider": "claude", "score": 10}))
        self.assertEqual(ew.model_for(cfg, "implementer", "medium", score=10), "z-ai/glm-5.3-flash:nitro")

    def test_busy_fallbacks_are_capable_models(self):
        cfg = self.cfg()
        self.assertEqual(ew.fallback_for(cfg, KIMI), HY4)
        self.assertEqual(ew.fallback_for(cfg, DS), HY4)
        self.assertEqual(ew.fallback_for(cfg, DS_FAST), "glm-5.3-flash-uncensored")
        self.assertNotIn("glm-5.3-uncensored", json.dumps(cfg["openrouter"]))  # text only, and Hy4 matches it

    def test_child_env_sets_the_window_instead_of_a_1m_model_id(self):
        cfg = self.cfg()
        env = ew.child_env(cfg, "k", KIMI)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.silkcompute.com")
        self.assertEqual(env["ANTHROPIC_MODEL"], KIMI)
        self.assertEqual(env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"], "1048576")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "k")

    def test_runner_command_carries_the_score(self):
        cmd = intake.runner_command(Path("x") / "external_worker.py", Path("c") / "T1.md", "T1", "implementer", 9)
        self.assertTrue(cmd.endswith("--rung implementer --score 9"), cmd)


class Intake(Temp):
    def test_apply_keeps_the_no_bs_switch_for_the_run(self):
        root = self.repo()
        prop = {"request": "add a thing", "slug": "x", "repo_root": str(root),
                "fields": {"files": 2, "loc": 40, "kind": "code"}}
        with mock.patch.object(intake, "session_status",
                               return_value={"budget": "S", "orchestrator": {"chosen": True}, "left": 2}):
            final = intake.apply(prop, {"switches": ["--no-bs", "second-opinion", "made-up"]})
        self.assertEqual(final["switches"], ["no_bs", "second_opinion"])


class Key(Temp):
    def test_the_key_is_read_from_the_repo_dotenv(self):
        root = self.repo(switches=["no_bs"])
        (root / ".env").write_text('OTHER=x\nexport SILK_API_KEY="sk-from-dotenv"\n', encoding="utf-8")
        cfg = ew.load_config(str(root))
        key, source = ew.get_key(cfg, str(root))
        self.assertEqual(key, "sk-from-dotenv")
        self.assertIn(".env", source)
        self.assertNotIn("sk-from-dotenv", source)
        with mock.patch.object(ew, "claude_bin", return_value="claude"):
            self.assertTrue(ew.available(cfg, str(root))[0])

    def test_the_environment_wins_over_dotenv(self):
        root = self.repo(switches=["no_bs"])
        (root / ".env").write_text("SILK_API_KEY=sk-file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"SILK_API_KEY": "sk-env"}):
            self.assertEqual(ew.get_key(ew.load_config(str(root)), str(root)), ("sk-env", "env SILK_API_KEY"))

    def test_the_balance_meter_counts_spend_upwards(self):
        cfg = ew.load_config(str(self.repo(switches=["no_bs"])))
        with mock.patch.object(ew, "key_status", return_value={"valid": True, "balance_usd": 19.5}):
            self.assertEqual(ew.usage_usd(cfg, "k"), -19.5)  # balance 20.0 -> 19.5 reads as +0.5 spent


class Run(Temp):
    def drive(self, children, args=None):
        root = self.repo(switches=["no_bs"])
        card = root / "card.md"
        card.write_text(CARD, encoding="utf-8")
        seen = []
        queue = list(children)

        def fake_spawn(cmd, cwd, env, card_text, timeout):
            seen.append(env["ANTHROPIC_MODEL"])
            return queue.pop(0)

        out = io.StringIO()
        with mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "get_key", return_value=("k", "test")), \
                mock.patch.object(ew, "usage_usd", return_value=None), \
                mock.patch.object(ew, "_spawn", side_effect=fake_spawn, create=True), \
                mock.patch.object(session_budget, "check", return_value=(True, "")), \
                mock.patch.object(session_budget, "status", return_value={"left": 2, "max": 4}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = ew.run({"--card": str(card), "--id": "T1", "--cwd": str(root), "--rung": "implementer",
                           **(args or {})})
        rec = json.loads((root / ".feature-ladder" / "evidence" / "T1-external.json").read_text(encoding="utf-8"))
        return code, seen, rec, out.getvalue()

    def test_an_intermediate_card_runs_on_kimi_via_silk(self):
        code, seen, rec, out = self.drive([(0, GOOD, "")], {"--score": "9"})
        self.assertEqual((code, seen), (0, [KIMI]))
        self.assertEqual((rec["model"], rec["provider"], rec["no_bs"]), (KIMI, "Silk Compute", True))
        self.assertIn(f"{KIMI} via Silk Compute", out)

    def test_a_busy_model_hands_the_card_to_its_fallback(self):
        code, seen, rec, out = self.drive([BUSY, (0, GOOD, "")], {"--score": "9"})
        self.assertEqual((code, seen), (0, [KIMI, HY4]))
        self.assertEqual((rec["model"], rec["busy_models"]), (HY4, [KIMI]))
        self.assertIn("fallback", out)

    def test_both_busy_means_exit_3_so_the_card_runs_on_claude(self):
        code, seen, rec, out = self.drive([BUSY, BUSY], {"--score": "5"})
        self.assertEqual((code, seen), (3, [DS, HY4]))
        self.assertIn("TASK T1 - blocked", out)

    def test_busy_after_real_work_is_not_retried(self):
        worked = (1, json.dumps({"type": "result", "is_error": True, "num_turns": 6, "result": "API Error: 429"}), "")
        code, seen, _, _ = self.drive([worked], {"--score": "9"})
        self.assertEqual((code, seen), (1, [KIMI]))

    def test_a_pinned_model_gets_no_fallback(self):
        code, seen, _, _ = self.drive([BUSY], {"--score": "9", "--model": KIMI})
        self.assertEqual((code, seen), (3, [KIMI]))


if __name__ == "__main__":
    unittest.main()
