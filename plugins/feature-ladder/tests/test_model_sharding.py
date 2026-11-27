"""Model sharding: every card conforms to the Hindsight recall, each rung runs on
the best model its budget level affords, and the session summary prices every
thread and shows what is left on the accounts.

Pins:
  recall     a card with no Memory: line gets one from the feature's saved recall
             on every path a card can take: the OpenRouter worker, a Claude rung
             the GLM orchestrator dispatches, an OpenClaude fanout and the lead of
             an OpenClaude team. With no saved recall the card recalls once
             (recall.py), and a card that still has no memory says why.
  budget     the level table picks the models; a model the user named, the jev
             preset and FEATURE_LADDER_WORKER_MODEL win over it; each model has a
             next-best one for when OpenRouter no longer serves it.
  cost       tokens are priced at the model's listed price; one charge shared by
             several threads is split by those prices; the coordinator's own
             share leaves out what OpenClaude cards were charged.
  summary    one row per thread with where it was billed and what it cost, the
             OpenRouter balance, and the Hindsight status (with the plain note
             that Hindsight Cloud has no balance endpoint).

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import external_coordinator as ec  # noqa: E402
import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402
import model_catalog as mc  # noqa: E402
import openclaude_runner as ocr  # noqa: E402
import session_budget  # noqa: E402
import session_summary as ss  # noqa: E402

PITFALL = "[pitfall] server/mapRoutes.ts writes must await the Firestore batch commit or the emulator test times out"
OTHER = "[decision] the docs site builds with mkdocs and deploys from the docs branch"
RECALL = {"results": [{"text": OTHER}, {"text": PITFALL}]}
CARD = ("TASK T1: save the map name to Firestore\nScope: server/mapRoutes.ts\n"
        "Done when: npx vitest run server/mapRoutes\nReturn: Evidence Packet\n")
ROWS = {"z-ai/glm-5.3-flash": {"in": 0.15, "out": 0.5, "cache_read": None, "tools": True},
        "z-ai/glm-5.3": {"in": 0.07, "out": 1.4, "cache_read": 0.01, "tools": True},
        "thinkingmachines/inkling-2-small": {"in": 0.3, "out": 1.0, "cache_read": None, "tools": True}}


class Temp(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = self.root / "home"
        self.home.mkdir()
        self.fl = self.root / ".feature-ladder"
        self.fl.mkdir()
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for k in ("FEATURE_LADDER_WORKERS", "FEATURE_LADDER_WORKER_MODEL", "FEATURE_LADDER_NO_BS",
                  "FEATURE_LADDER_BUDGET", "FEATURE_LADDER_RECALL"):
            os.environ.pop(k, None)
        home = mock.patch.object(Path, "home", lambda: self.home)
        home.start()
        self.addCleanup(home.stop)
        ladder_state._ROOTS.clear()

    def saved_recall(self, data=RECALL, age=0):
        f = self.fl / "recall.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        if age:
            os.utime(f, (time.time() - age, time.time() - age))
        return f


# ------------------------------------------------------------------ recall --
class RecallConformance(Temp):
    def test_a_card_gets_only_the_facts_about_its_files(self):
        self.saved_recall()
        card, added = ew.with_memory(CARD, self.root)
        self.assertTrue(added)
        self.assertIn("Memory: " + PITFALL, card)
        self.assertNotIn("mkdocs", card)

    def test_a_card_that_already_has_memory_is_left_alone(self):
        self.saved_recall()
        card, added = ew.with_memory(CARD + "Memory: [convention] already here\n", self.root)
        self.assertFalse(added)
        self.assertNotIn("Firestore batch", card)

    def test_no_saved_recall_recalls_once_and_then_fills_the_card(self):
        def live(cwd, title, paths):
            self.assertEqual(paths, ["server/mapRoutes.ts"])
            self.assertIn("save the map name", title)
            self.saved_recall()
            return None
        with mock.patch.object(ew, "_live_recall", side_effect=live) as m:
            card, added = ew.with_memory(CARD, self.root, cwd=self.root)
            again, _ = ew.with_memory(CARD, self.root, cwd=self.root)   # the saved recall is fresh now
        self.assertEqual(m.call_count, 1)
        self.assertIn(PITFALL, card)
        self.assertIn(PITFALL, again)

    def test_a_card_with_no_memory_says_why(self):
        with mock.patch.object(ew, "_live_recall", return_value="Hindsight is off: no HINDSIGHT_API_KEY"):
            card, added = ew.with_memory(CARD, self.root, cwd=self.root)
        self.assertTrue(added)
        self.assertIn("Memory: none (Hindsight is off: no HINDSIGHT_API_KEY)", card)
        with mock.patch.object(ew, "_live_recall", side_effect=RuntimeError("boom")):
            card, _ = ew.with_memory(CARD, self.root, cwd=self.root)
        self.assertIn("Memory: none (recall failed (RuntimeError))", card)   # memory never stops a card

    def test_the_switch_turns_the_recall_off_and_an_active_run_keeps_its_own(self):
        with mock.patch.dict(os.environ, {"FEATURE_LADDER_RECALL": "off"}), \
                mock.patch.object(ew, "_live_recall") as m:
            self.assertIn("recall is off", ew.ensure_recall(self.root, self.root, "x", []))
            m.assert_not_called()
        (self.fl / "runs" / "s").mkdir(parents=True)
        (self.fl / "run.json").write_text(json.dumps({"slug": "s", "active": True}), encoding="utf-8")
        (self.fl / "runs" / "s" / "recall.json").write_text(json.dumps(RECALL), encoding="utf-8")
        old = time.time() - 10 * ew.RECALL_FRESH
        os.utime(self.fl / "runs" / "s" / "recall.json", (old, old))
        with mock.patch.object(ew, "_live_recall") as m:
            self.assertIsNone(ew.ensure_recall(self.root, self.root, "x", []))   # the coordinator's recall stands
            m.assert_not_called()

    def test_outside_a_run_a_stale_recall_is_made_again(self):
        self.saved_recall(age=ew.RECALL_FRESH + 60)
        with mock.patch.object(ew, "_live_recall", return_value=None) as m:
            ew.ensure_recall(self.root, self.root, "x", [])
        m.assert_called_once()

    def test_the_openrouter_worker_passes_its_folder_so_the_card_can_recall(self):
        seen = {}

        def prepare(card, root, task_id, max_turns, timeout, budget=None, web=False, cwd=None):
            seen["cwd"] = cwd
            return card, []
        f = self.root / "T1.md"
        f.write_text(CARD, encoding="utf-8")
        good = json.dumps({"type": "result", "num_turns": 2, "usage": {},
                           "result": "TASK T1 - done\nChanged: x\nRetained: none - n/a"})
        with mock.patch.object(ew, "prepare_card", side_effect=prepare), \
                mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "get_key", return_value=("sk-or-test-key-123456", "test")), \
                mock.patch.object(ew, "usage_usd", return_value=None), \
                mock.patch.object(ew, "_spawn", return_value=(0, good, "")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ew.run({"--card": str(f), "--id": "T1", "--cwd": str(self.root)}), 0)
        self.assertEqual(seen["cwd"], str(self.root))

    def test_a_claude_rung_the_orchestrator_dispatches_reads_the_same_recall(self):
        self.saved_recall()
        f = self.root / "T1.md"
        f.write_text(CARD, encoding="utf-8")
        seen = {}

        def spawn(cmd, cwd, env, stdin, timeout):
            seen["stdin"] = stdin
            return 0, json.dumps({"type": "result", "num_turns": 3, "usage": {},
                                  "result": "TASK T1 - done\nChanged: x\nRetained: none - n/a"}), ""
        out = io.StringIO()
        with mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "_spawn", side_effect=spawn), \
                mock.patch.object(session_budget, "check", return_value=(True, "")), \
                contextlib.redirect_stdout(out):
            rc = ec.dispatch({"--card": str(f), "--id": "T1", "--agent": "implementer", "--cwd": str(self.root)})
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn("Memory: " + PITFALL, seen["stdin"])
        rec = ladder_state.read_json(self.fl / "evidence" / "T1-plan.json")
        self.assertEqual(rec["card_added"], ["memory"])

    def test_an_integrators_prompt_is_not_given_a_memory_line(self):
        self.saved_recall()
        f = self.root / "I1.md"
        f.write_text("Integrate these packets: ...\nserver/mapRoutes.ts\n", encoding="utf-8")
        seen = {}

        def spawn(cmd, cwd, env, stdin, timeout):
            seen["stdin"] = stdin
            return 0, json.dumps({"type": "result", "num_turns": 3, "usage": {},
                                  "result": "INTEGRATION: coherent\nRetained: none - n/a"}), ""
        with mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "_spawn", side_effect=spawn), \
                mock.patch.object(session_budget, "check", return_value=(True, "")), \
                contextlib.redirect_stdout(io.StringIO()):
            ec.dispatch({"--card": str(f), "--id": "I1", "--agent": "integrator", "--cwd": str(self.root)})
        self.assertNotIn("Memory:", seen["stdin"])

    def test_a_team_lead_is_handed_the_recall_to_copy_onto_its_cards(self):
        self.saved_recall()
        text = ocr.team_memory(self.root, self.root, "Save the map name\nFiles: server/mapRoutes.ts and web/Map.tsx\n")
        self.assertEqual(text, "- " + PITFALL)
        note = ocr.TEAM_MEMORY.format(memory=text)
        self.assertIn("carries a `Memory:` line", note)
        with mock.patch.object(ew, "_live_recall", return_value="Hindsight is off: no key"):
            os.remove(self.fl / "recall.json")
            self.assertEqual(ocr.team_memory(self.root, self.root, "Rename a flag\n"),
                             "Memory: none (Hindsight is off: no key)")

    def test_the_coordinator_is_told_to_recall_before_any_card(self):
        text = (PLUGIN / "agents" / "coordinator.md").read_text(encoding="utf-8")
        self.assertIn("Recall once, with no model", text)
        self.assertIn("Never skip this step", text)


# ------------------------------------------------------------------ budget --
class BudgetLevel(Temp):
    def test_the_standard_level_is_the_default_and_names_a_model_for_every_rung(self):
        cfg = ew.load_config(self.root, no_bs=False)
        self.assertEqual(cfg["budget"], "standard")
        self.assertEqual(cfg["openrouter"]["models"], mc.LEVELS["standard"])
        for level, table in mc.LEVELS.items():
            self.assertEqual(set(table), set(mc.RUNGS), level)

    def test_each_level_costs_no_less_than_the_one_below_for_research(self):
        self.assertEqual([mc.LEVELS[x]["evidence-collector"] for x in ("low", "standard", "high")],
                         [mc.INKLING_MINI, mc.INKLING_SMALL, mc.INKLING])
        self.assertEqual(mc.ORCHESTRATOR, session_budget.ORCHESTRATORS["glm"]["model_id"])  # GLM 5.3 at every level

    def test_providers_json_sets_the_level_and_the_environment_overrides_it(self):
        (self.fl / "providers.json").write_text(json.dumps({"budget": "high"}), encoding="utf-8")
        cfg = ew.load_config(self.root, no_bs=False)
        self.assertEqual(cfg["openrouter"]["models"]["evidence-collector"], mc.INKLING)
        self.assertEqual(cfg["openrouter"]["models"]["implementer"], mc.GLM)
        with mock.patch.dict(os.environ, {"FEATURE_LADDER_BUDGET": "low"}):
            self.assertEqual(ew.load_config(self.root, no_bs=False)["openrouter"]["models"]["mechanic"], mc.INKLING_MINI)
        (self.fl / "providers.json").write_text(json.dumps({"budget": "platinum"}), encoding="utf-8")
        self.assertEqual(ew.load_config(self.root, no_bs=False)["budget"], "standard")   # an unknown level

    def test_a_model_the_user_named_wins_for_its_rung_only(self):
        (self.fl / "providers.json").write_text(json.dumps(
            {"budget": "low", "openrouter": {"models": {"implementer": "moonshotai/kimi-k3.1-pro"}}}), encoding="utf-8")
        p = ew.load_config(self.root, no_bs=False)["openrouter"]
        self.assertEqual(p["models"]["implementer"], "moonshotai/kimi-k3.1-pro")
        self.assertEqual(p["models"]["evidence-collector"], mc.INKLING_MINI)
        self.assertEqual(p["pinned"], ["implementer"])

    def test_the_jev_preset_takes_the_coding_rungs_and_research_follows_the_level(self):
        (self.fl / "providers.json").write_text(json.dumps({"openrouter": {"preset": "jev"}}), encoding="utf-8")
        m = ew.load_config(self.root, no_bs=False)["openrouter"]["models"]
        self.assertEqual((m["mechanic"], m["implementer"]), ("typesafe/jev-router", "typesafe/jev-router"))
        self.assertEqual(m["evidence-collector"], mc.INKLING_SMALL)

    def test_silk_is_untouched_by_the_level(self):
        with mock.patch.dict(os.environ, {"FEATURE_LADDER_BUDGET": "high"}):
            cfg = ew.load_config(self.root, no_bs=True)
        self.assertEqual(cfg["openrouter"]["models"], ew.SILK["models"])
        self.assertEqual(cfg["openrouter"]["fallbacks"], ew.SILK["fallbacks"])

    def test_every_model_but_the_last_has_a_next_best_one(self):
        p = ew.load_config(self.root, no_bs=False)["openrouter"]
        self.assertEqual(p["fallbacks"][mc.INKLING_SMALL], mc.INKLING_MINI)
        self.assertEqual(ew.fallback_for({"openrouter": p}, mc.GLM), mc.FLASH)
        for start in mc.NEXT:            # no loop: every chain ends
            seen, m = set(), start
            while m in mc.NEXT:
                self.assertNotIn(m, seen)
                seen.add(m)
                m = mc.NEXT[m]

    def test_a_model_openrouter_no_longer_serves_moves_the_card_on(self):
        gone = json.dumps({"type": "result", "is_error": True, "num_turns": 1, "usage": {"output_tokens": 0},
                           "result": "API Error: 404 No endpoints found for thinkingmachines/inkling-2-small."})
        self.assertTrue(ew.busy_before_work(1, gone, ""))
        worked = json.dumps({"type": "result", "is_error": True, "num_turns": 6, "usage": {"output_tokens": 900},
                             "result": "No endpoints found"})
        self.assertFalse(ew.busy_before_work(1, worked, ""))   # it already did work: never retried elsewhere


# -------------------------------------------------------------------- cost --
class Prices(Temp):
    def test_ids_are_matched_without_routing_suffixes(self):
        self.assertEqual(mc.bare("z-ai/glm-5.3-flash:nitro[1m]"), "z-ai/glm-5.3-flash")
        self.assertEqual(mc.bare("moonshotai/kimi-k3.1-pro:batch"), "moonshotai/kimi-k3.1-pro:batch")  # its own price
        self.assertEqual(mc.claude_family("claude-sonnet-5-5"), "sonnet")
        self.assertIsNone(mc.claude_family("z-ai/glm-5.3"))

    def test_tokens_are_priced_at_the_listed_price(self):
        row = {"input": 1_000_000, "cache_read": 2_000_000, "cache_write": 0, "output": 100_000}
        self.assertAlmostEqual(mc.estimate_usd("z-ai/glm-5.3", row, ROWS), 0.07 + 0.02 + 0.14)
        # no cache price listed: cached input is charged as input
        self.assertAlmostEqual(mc.estimate_usd("z-ai/glm-5.3-flash:nitro", row, ROWS), 0.15 * 3 + 0.05)
        self.assertAlmostEqual(mc.estimate_usd("claude-opus-5-5", {"input_tokens": 1_000_000, "output_tokens": 0}, {}), 4.0)
        self.assertIsNone(mc.estimate_usd("unknown/model", row, ROWS))

    def test_one_charge_is_split_by_what_each_thread_used(self):
        self.assertEqual(mc.share(0.30, [0.01, 0.02]), [0.1, 0.2])
        self.assertEqual(mc.share(0.30, [None, None, None]), [0.1, 0.1, 0.1])
        self.assertEqual(mc.share(None, [0.01, 0.02]), [None, None])

    def test_the_catalogue_is_cached_and_a_stale_copy_survives_a_dead_network(self):
        data = {"data": [{"id": "z-ai/glm-5.3", "pricing": {"prompt": "0.00000007", "completion": "0.0000014"},
                          "supported_parameters": ["tools"], "context_length": 1048576}]}
        with mock.patch.object(mc, "_fetch", return_value=data) as f:
            rows, at = mc.catalog()
            mc.catalog()
        self.assertEqual(f.call_count, 1)                      # the second read came from the cache
        self.assertAlmostEqual(rows["z-ai/glm-5.3"]["in"], 0.07)
        self.assertTrue(rows["z-ai/glm-5.3"]["tools"])
        with mock.patch.object(mc, "_fetch", side_effect=OSError("down")):
            rows, _ = mc.catalog(refresh=True)
        self.assertIn("z-ai/glm-5.3", rows)
        self.assertEqual(mc.catalog(offline=True)[0], rows)

    def test_check_reports_a_model_that_is_no_longer_listed(self):
        with mock.patch.object(mc, "catalog", return_value=(ROWS, time.time())):
            report, ok = mc.check("standard")
            self.assertTrue(ok)
            report, ok = mc.check("high")                      # Kimi and Inkling are not in ROWS
        self.assertFalse(ok)
        self.assertFalse(report["models"]["evidence-collector"]["listed"])

    def test_the_balance_is_what_was_bought_minus_what_was_used(self):
        def fetch(url, key, timeout=20):
            if url.endswith("/v1/credits"):
                return {"data": {"total_credits": 29.2, "total_usage": 1.25}}
            return {"data": {"usage": 1.2, "limit": 25, "limit_remaining": 23.8}}
        b = mc.openrouter_balance("k", fetch=fetch)
        self.assertEqual((b["account_left_usd"], b["key_left_usd"]), (27.95, 23.8))

        def no_credits(url, key, timeout=20):
            if url.endswith("/v1/credits"):
                raise OSError("403")
            return {"data": {"usage": 1.2, "limit": None, "limit_remaining": None}}
        b = mc.openrouter_balance("k", fetch=no_credits)
        self.assertIsNone(b["account_left_usd"])
        self.assertEqual(b["key_used_usd"], 1.2)
        self.assertIn("error", mc.openrouter_balance(""))

    def test_the_coordinators_own_share_leaves_out_openclaude_cards_too(self):
        ev = self.fl / "evidence"
        ev.mkdir()
        (ev / "T1-external.json").write_text(json.dumps({"openrouter_cost_usd": 0.02}), encoding="utf-8")
        (ev / "T2-openclaude.json").write_text(json.dumps({"openrouter_cost_usd": 0.03}), encoding="utf-8")
        (ev / "fanout-1-openclaude.json").write_text(json.dumps({"openrouter_cost_usd": 0.05}), encoding="utf-8")
        (ev / "T9-openclaude.json").write_text(json.dumps({"openrouter_cost_usd": None}), encoding="utf-8")
        self.assertAlmostEqual(ec._workers_cost_since(self.root, time.time() - 60), 0.10)
        self.assertEqual(ec._workers_cost_since(self.root, time.time() + 60), 0.0)


# ----------------------------------------------------------------- summary --
def row(**over):
    base = {"at": "2026-11-26T10:00:00+00:00", "key": "k", "slug": "add-map", "agent": "implementer (openrouter)",
            "rung": "implementer", "model": "z-ai/glm-5.3-flash:nitro", "task": "T1", "turns": 9,
            "input": 100_000, "cache_read": 0, "cache_write": 0, "output": 2_000, "total": 102_000, "cost_usd": 0.0175}
    return dict(base, **over)


class Summary(Temp):
    def ledger(self, slug, rows):
        d = self.fl / "runs" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def build(self, **kw):
        balance = {"account_left_usd": 27.5, "account_bought_usd": 29.2, "account_used_usd": 1.7,
                   "key_left_usd": 23.3, "key_limit_usd": 25, "key_used_usd": 1.7}
        hs = {"connected": True, "bank": "coding-agent::acme-widgets", "memories": 48, "written_this_session": 2,
              "balance_usd": None, "balance_note": "Hindsight Cloud's API has no balance endpoint; see Billing"}
        with mock.patch.object(mc, "catalog", return_value=(ROWS, time.time())), \
                mock.patch.object(mc, "openrouter_balance", return_value=balance), \
                mock.patch.object(ew, "get_key", return_value=("sk-or-test-key-123456", "test")), \
                mock.patch.object(ss, "hindsight_status", return_value=hs):
            return ss.build(str(self.root), **kw)

    def test_each_thread_says_where_it_was_billed_and_on_what_basis(self):
        # The key's counter trails a request, so a thread is priced from its own tokens
        # whatever the difference around it said (0.0175 here, zero or another thread's cost elsewhere).
        for charged in (0.0175, 0.0, None):
            est = ss.thread_cost(row(cost_usd=charged), ROWS)
            self.assertEqual(est["basis"], "estimate")
            self.assertAlmostEqual(est["usd"], 0.016)
        # no tokens reported (an OpenClaude agent): the key's difference is all there is
        blind = dict(input=0, output=0, total=0)
        self.assertEqual(ss.thread_cost(row(cost_usd=0.0094, **blind), ROWS), {"usd": 0.0094, "basis": "charged"})
        self.assertEqual(ss.thread_cost(row(cost_usd=0.004, cost_shared=True, cost_group="g", **blind), ROWS),
                         {"usd": 0.004, "basis": "share"})
        self.assertEqual(ss.thread_cost(row(cost_usd=0.0, **blind), ROWS), {"usd": None, "basis": "unknown"})
        self.assertEqual(ss.thread_cost(row(agent="mechanic (silk)", model="deepseek-x", cost_usd=0.02), ROWS),
                         {"usd": 0.02, "basis": "charged"})
        plan = ss.thread_cost(row(agent="implementer", model="claude-sonnet-5-5", cost_usd=None), ROWS)
        self.assertEqual(plan["basis"], "plan")
        self.assertAlmostEqual(plan["usd"], 0.2 + 0.02)
        self.assertEqual(ss.billed_to(row(agent="implementer (claude plan)", model="sonnet")), "Claude plan")
        self.assertEqual(ss.billed_to(row(agent="mechanic (silk)", model="deepseek-x")), "Silk")
        self.assertEqual(ss.billed_to(row(agent="team (openclaude)")), "OpenRouter")

    def test_a_session_summary_has_every_thread_both_balances_and_the_totals(self):
        (self.fl / "session.json").write_text(json.dumps(
            {"started": "2026-11-26T09:00:00+00:00", "size": "M", "max_agents": 4,
             "spawned": [{"agent": "implementer"}, {"agent": "mechanic"}]}), encoding="utf-8")
        self.ledger("add-map", [
            row(at="2026-11-25T08:00:00+00:00", key="old", task="OLD"),            # an earlier session
            row(key="c", task="coordinator", rung="coordinator", agent="coordinator (openrouter)",
                model="z-ai/glm-5.3", cost_usd=0.031, seconds=412),
            row(key="t1"),
            row(key="t2", task="T2", agent="implementer", model="claude-sonnet-5-5", cost_usd=None)])
        self.ledger("rename-flag", [row(key="m", slug="rename-flag", task="T1", rung="mechanic",
                                        agent="mechanic (openclaude)", cost_usd=0.0)])
        (self.fl / "openrouter-usage.json").write_text(json.dumps(
            {"session_started": "2026-11-26T09:00:00+00:00", "usage_usd": 1.5}), encoding="utf-8")
        s = self.build()
        self.assertEqual(s["features"], ["add-map", "rename-flag"])
        self.assertEqual([t["thread"] for t in s["threads"]], ["coordinator", "T1", "T2", "T1"])
        self.assertEqual(s["totals"]["OpenRouter"]["threads"], 3)
        coordinator = 100_000 * 0.07 / 1e6 + 2_000 * 1.4 / 1e6
        self.assertAlmostEqual(s["totals"]["OpenRouter"]["usd"], coordinator + 0.016 + 0.016, places=4)
        self.assertAlmostEqual(s["totals"]["OpenRouter"]["estimated_usd"], coordinator + 0.016 + 0.016, places=4)
        self.assertEqual(s["openrouter"]["session_charged_usd"], 0.2)   # 1.7 used now, 1.5 at the first thread
        text = ss.render(s)
        self.assertIn("| add-map / coordinator | coordinator | z-ai/glm-5.3 | OpenRouter | 9 | 412s |", text)
        self.assertIn("$0.0160 est.", text)
        self.assertIn("plan (~$0.2200 at API prices)", text)
        self.assertIn("Subagents: 2 of 4", text)
        self.assertIn("Budget level: standard", text)
        self.assertIn("OpenRouter charged this key $0.2000 since the session's first OpenRouter thread", text)
        self.assertIn("OpenRouter balance: $27.50 left on the account ($29.20 bought, $1.70 used); "
                      "this key has $23.30 left of its $25.00 limit", text)
        self.assertIn("Hindsight: connected, bank coding-agent::acme-widgets, 48 memories stored, 2 written this session", text)
        self.assertIn("no balance endpoint", text)
        self.assertNotIn("OLD", text)

    def test_the_first_external_thread_of_a_session_records_what_the_key_had_used(self):
        (self.fl / "session.json").write_text(json.dumps({"started": "S1", "spawned": []}), encoding="utf-8")
        ew.note_usage_baseline(self.root, 1.5)
        ew.note_usage_baseline(self.root, 1.9)                       # a later thread: the baseline stays
        self.assertEqual(ladder_state.read_json(self.fl / "openrouter-usage.json")["usage_usd"], 1.5)
        (self.fl / "session.json").write_text(json.dumps({"started": "S2", "spawned": []}), encoding="utf-8")
        ew.note_usage_baseline(self.root, 2.4)                       # a new session starts its own
        self.assertEqual(ladder_state.read_json(self.fl / "openrouter-usage.json"),
                         {"session_started": "S2", "usage_usd": 2.4,
                          "at": ladder_state.read_json(self.fl / "openrouter-usage.json")["at"]})
        ew.note_usage_baseline(self.root, None)
        ew.note_usage_baseline(self.root, -3.0, provider="silk")     # a balance, not a usage counter
        self.assertEqual(ladder_state.read_json(self.fl / "openrouter-usage.json")["usage_usd"], 2.4)

    def test_one_run_can_be_summarised_on_its_own(self):
        self.ledger("add-map", [row(key="t1")])
        self.ledger("other", [row(key="x", slug="other", task="X9")])
        s = self.build(slug="add-map")
        self.assertEqual((s["scope"], [t["thread"] for t in s["threads"]]), ("run add-map", ["T1"]))
        self.assertEqual(len(self.build(everything=True)["threads"]), 2)

    def test_nothing_on_the_ledger_still_prints_the_balances(self):
        text = ss.render(self.build(everything=True))
        self.assertIn("No thread is on the ledger", text)
        self.assertIn("OpenRouter balance: $27.50", text)
        self.assertIn("Hindsight:", text)

    def test_hindsight_status_reads_the_bank_and_says_there_is_no_balance(self):
        sys.path.insert(0, str(PLUGIN / "skills" / "hindsight-memory" / "scripts"))
        import resolve_bank
        (self.fl / "memory-ledger.jsonl").write_text(
            json.dumps({"at": "2026-11-26T10:00:00+00:00", "hash": "a"}) + "\n"
            + json.dumps({"at": "2026-11-20T10:00:00+00:00", "hash": "b"}) + "\n", encoding="utf-8")
        seen = []

        def get(url, key, timeout=15):
            seen.append(url)
            return {"total_nodes": 48, "last_memory_write_at": "2026-10-24T18:15:03+00:00"}
        with mock.patch.object(resolve_bank, "key_status", return_value=(True, "environment")), \
                mock.patch.object(resolve_bank, "setting", side_effect=lambda n, c=None: (
                    {"HINDSIGHT_API_KEY": "hsk_test"}.get(n), "environment")), \
                mock.patch.object(resolve_bank, "resolve", return_value={"bank_id": "coding-agent::acme-widgets"}):
            h = ss.hindsight_status(self.root, self.root, since="2026-11-26T09:00:00+00:00", get=get)
        self.assertEqual((h["connected"], h["memories"], h["written_this_session"]), (True, 48, 1))
        self.assertIsNone(h["balance_usd"])
        self.assertIn("no balance endpoint", h["balance_note"])
        self.assertEqual(seen, ["https://api.hindsight.vectorize.io/v1/default/banks/coding-agent%3A%3Aacme-widgets/stats"])
        with mock.patch.object(resolve_bank, "key_status", return_value=(False, "no HINDSIGHT_API_KEY")):
            self.assertIn("Hindsight is off", ss.hindsight_status(self.root, self.root, get=get)["error"])

    def test_the_commands_print_the_summary(self):
        wrap = (PLUGIN / "commands" / "wrap.md").read_text(encoding="utf-8")
        self.assertIn("session_summary.py", wrap)
        self.assertLess(wrap.index("session_summary.py"), wrap.index("session_budget.py\" close"))  # before the session ends
        self.assertIn("cost of every thread", wrap)
        self.assertIn("session_summary.py\" --slug", (PLUGIN / "commands" / "feature.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
