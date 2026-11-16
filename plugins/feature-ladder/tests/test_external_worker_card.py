"""Tests for the queue card external_worker.py hands the OpenRouter worker:
Memory from the saved recall (the file confidence.py scores), Bounds and the
output contract built in, for the Jev preset and the default GLM queue; and
for what `run` reports when the child fails, times out or cannot start.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import confidence as conf  # noqa: E402
import external_worker as ew  # noqa: E402
import session_budget  # noqa: E402

PITFALL = "[pitfall] server/mapRoutes.ts writes must await the Firestore batch commit or the emulator test times out"
CONVENTION = "[convention] npx vitest run server/mapRoutes is the gate for map route changes; it passes green"
OTHER = "[decision] the docs site builds with mkdocs and deploys from the docs branch"
RECALL = {"results": [{"text": OTHER}, {"text": PITFALL}, {"text": CONVENTION}]}

CARD = """TASK T1: save the map name to Firestore
Rung: implementer (sonnet/medium)   Router score: 4   Flags: none
Scope: server/mapRoutes.ts, server/mapRoutes.test.ts
Design: N/A - the request is the design
Confidence: 0.78 proceed (steady) - 3 memories
Done when: npx vitest run server/mapRoutes
Do not: touch other routes
Secrets: none
Return: Evidence Packet
"""
GOOD = json.dumps({"type": "result", "num_turns": 3, "usage": {},
                   "result": "TASK T1 - done\nChanged: server/mapRoutes.ts\nRetained: none - n/a"})


class Temp(unittest.TestCase):
    """Every folder a test makes is removed again."""

    def tmpdir(self):
        d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    def repo(self, recall=RECALL, slug="map-name", preset=None, app=False):
        d = self.tmpdir()
        fl = d / ".feature-ladder"
        if app:
            fl.mkdir(parents=True)
            (fl / "recall.json").write_text(json.dumps(recall), encoding="utf-8")
        else:
            (fl / "runs" / slug).mkdir(parents=True)
            (fl / "run.json").write_text(json.dumps({"slug": slug, "active": True}), encoding="utf-8")
            (fl / "runs" / slug / "recall.json").write_text(json.dumps(recall), encoding="utf-8")
        if preset:
            (fl / "providers.json").write_text(json.dumps({"openrouter": {"preset": preset}}), encoding="utf-8")
        return d


class PrepareCard(Temp):
    def test_missing_memory_filled_from_recall_scope_first(self):
        root = self.repo()
        card, added = ew.prepare_card(CARD, root, "T1", 40, 1500)
        mem = ew._card_line(card, "Memory")
        self.assertIn("memory", added)
        self.assertTrue(mem.startswith("[pitfall] server/mapRoutes.ts"), mem)
        self.assertIn("[convention] npx vitest", mem)
        self.assertLess(mem.index("[convention]"), mem.index("[decision]"))

    def test_bounds_and_contract_built_in(self):
        card, added = ew.prepare_card(CARD, self.repo(), "T1", 12, 600, {"left": 3, "max": 4})
        bounds = ew._card_line(card, "Bounds")
        self.assertIn("12 turns", bounds)
        self.assertIn("600s", bounds)
        self.assertIn("3 of 4 left", bounds)
        self.assertIn("TASK T1 - <status", card)
        self.assertIn("Retained:", card)
        self.assertEqual({"memory", "bounds", "contract"}, set(added))

    def test_coordinator_memory_kept_verbatim(self):
        mine = CARD + "Memory: [pitfall] something the coordinator chose\n"
        card, added = ew.prepare_card(mine, self.repo(), "T1", 40, 1500)
        self.assertEqual("[pitfall] something the coordinator chose", ew._card_line(card, "Memory"))
        self.assertNotIn("memory", added)

    def test_memory_none_filled_only_when_recall_touches_scope(self):
        card, _ = ew.prepare_card(CARD + "Memory: none\n", self.repo(), "T1", 40, 1500)
        self.assertTrue(ew._card_line(card, "Memory").startswith("[pitfall]"))
        self.assertEqual(1, card.count("Memory:"))
        card, _ = ew.prepare_card(CARD + "Memory: none\n", self.repo({"results": [{"text": OTHER}]}), "T1", 40, 1500)
        self.assertEqual("none", ew._card_line(card, "Memory"))

    def test_no_recall_says_none(self):
        card, _ = ew.prepare_card(CARD, self.tmpdir(), "T1", 40, 1500)
        self.assertEqual("none", ew._card_line(card, "Memory"))

    def test_app_layout_recall(self):
        card, _ = ew.prepare_card(CARD, self.repo(app=True), "T1", 40, 1500)
        self.assertTrue(ew._card_line(card, "Memory").startswith("[pitfall]"))

    def test_existing_bounds_and_contract_not_duplicated(self):
        full = CARD + "Memory: none\nBounds: 5 turns\nRetained: none - n/a\n"
        card, added = ew.prepare_card(full, self.repo({"results": []}), "T1", 40, 1500)
        self.assertEqual([], added)
        self.assertEqual(1, card.count("Bounds:"))

    def test_memory_is_the_recall_the_sentiment_step_scores(self):
        """The card's Memory comes from the same recall.json confidence.py reads,
        so the worker sees exactly the facts behind the Confidence: line."""
        root = self.repo()
        f = ew.recall_file(root)
        scored = conf.decide("card", "save the map name", ["server/mapRoutes.ts"], "S",
                             memories=conf.parse_memories(f.read_text(encoding="utf-8")))
        card, _ = ew.prepare_card(CARD, root, "T1", 40, 1500)
        mem = ew._card_line(card, "Memory")
        for item in scored["surface"]["items"]:
            self.assertIn(item["text"][:80], mem)


class Run(Temp):
    """Drives run() with the child process replaced: (exit code or None on timeout, stdout, stderr)."""

    def drive(self, child, preset=None, task_id="T1", budget=True, args=None):
        root = self.repo(preset=preset)
        card_file = root / ".feature-ladder" / "cards" / "T1.md"
        card_file.parent.mkdir(parents=True)
        card_file.write_text(CARD, encoding="utf-8")
        seen = {}

        def fake_spawn(cmd, cwd, env, card, timeout):
            seen.update(cmd=list(cmd), input=card, env=env, timeout=timeout)
            if "--append-system-prompt-file" in cmd:
                f = Path(cmd[cmd.index("--append-system-prompt-file") + 1])
                seen["contract"] = f.read_text(encoding="utf-8") if f.is_file() else None
            return child

        home = str(self.tmpdir())
        out = io.StringIO()
        patches = [mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}),
                   mock.patch.object(ew, "claude_bin", return_value="claude"),
                   mock.patch.object(ew, "get_key", return_value=("k", "test")),
                   mock.patch.object(ew, "usage_usd", return_value=None),
                   mock.patch.object(ew, "_spawn", side_effect=fake_spawn, create=True)]
        if budget:
            patches += [mock.patch.object(session_budget, "check", return_value=(True, "")),
                        mock.patch.object(session_budget, "status", return_value={"left": 2, "max": 4})]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = ew.run({"--card": str(card_file), "--id": task_id, "--cwd": str(root),
                               "--max-turns": "9", **(args or {})})
        return code, seen, root, out.getvalue()

    def record(self, root, name="T1"):
        return json.loads((root / ".feature-ladder" / "evidence" / f"{name}-external.json").read_text(encoding="utf-8"))


class RunHandsTheCard(Run):
    """run() hands the prepared card to the child claude -p, for both presets."""

    def _assert_card(self, seen, record):
        card = seen["input"]
        self.assertTrue(ew._card_line(card, "Memory").startswith("[pitfall] server/mapRoutes.ts"))
        self.assertIn("at most 9 turns", ew._card_line(card, "Bounds"))
        self.assertIn("2 of 4 left", ew._card_line(card, "Bounds"))
        self.assertIn("Output contract", card)
        self.assertEqual("9", seen["cmd"][seen["cmd"].index("--max-turns") + 1])
        self.assertEqual(card, record["card"])

    def test_jev_preset(self):
        code, seen, root, _ = self.drive((0, GOOD, ""), preset="jev")
        self.assertEqual(0, code)
        self.assertTrue(self.record(root)["model"].startswith("typesafe/jev-router"))
        self._assert_card(seen, self.record(root))

    def test_default_glm_queue(self):
        code, seen, root, _ = self.drive((0, GOOD, ""))
        self.assertEqual(0, code)
        self.assertTrue(self.record(root)["model"].startswith("z-ai/glm-5.3-flash"))
        self._assert_card(seen, self.record(root))

    def test_the_contract_travels_in_a_file_not_in_a_multi_line_argument(self):
        # An npm .cmd shim hands its arguments to cmd.exe, which cuts one at the first newline.
        _, seen, _, _ = self.drive((0, GOOD, ""))
        self.assertEqual([a for a in seen["cmd"] if "\n" in a], [])
        self.assertIn("Evidence Packet", seen["contract"])
        contract_file = Path(seen["cmd"][seen["cmd"].index("--append-system-prompt-file") + 1])
        self.assertFalse(contract_file.exists(), "the temporary contract file was left behind")


class RunOutcomes(Run):
    def test_a_child_that_failed_without_a_packet_exits_1_and_says_why(self):
        code, _, root, out = self.drive((1, "", "boom\nAPI Error: 500 upstream exploded\n"))
        self.assertEqual(code, 1)
        self.assertIn("TASK T1 - partial", out)
        self.assertIn("API Error: 500 upstream exploded", out)
        self.assertEqual(self.record(root)["exit"], 1)

    def test_a_rejected_key_or_empty_credits_exits_3_so_the_card_falls_back(self):
        for stderr in ("API Error: 401 {\"error\":{\"message\":\"No auth credentials found\"}}",
                       "API Error: 402 Insufficient credits"):
            with self.subTest(stderr):
                code, _, _, out = self.drive((1, "", stderr))
                self.assertEqual(code, 3)
                self.assertIn("TASK T1 - blocked", out)

    def test_an_error_result_is_a_failure_even_with_exit_0(self):
        body = json.dumps({"type": "result", "is_error": True, "num_turns": 0, "result": "Invalid API key"})
        code, _, _, _ = self.drive((0, body, ""))
        self.assertEqual(code, 3)

    def test_a_real_packet_wins_over_a_non_zero_exit(self):
        code, _, _, out = self.drive((1, GOOD, "warning: max turns"))
        self.assertEqual(code, 0)
        self.assertIn("TASK T1 - done", out)

    def test_timeout_exits_1_and_still_writes_evidence(self):
        code, seen, root, out = self.drive((None, "", ""))
        self.assertEqual(code, 1)
        self.assertIn("timed out", out)
        self.assertEqual(self.record(root)["timed_out"], True)
        self.assertEqual(seen["timeout"], ew.DEFAULTS["openrouter"]["timeout"])

    def test_the_timeout_flag_reaches_the_child(self):
        _, seen, _, _ = self.drive((0, GOOD, ""), args={"--timeout": "540"})
        self.assertEqual(seen["timeout"], 540)

    def test_an_id_with_a_path_separator_cannot_lose_the_packet(self):
        code, _, root, out = self.drive((0, GOOD, ""), task_id="T/1")
        self.assertEqual(code, 0)
        self.assertIn("TASK T1 - done", out)
        self.assertTrue((root / ".feature-ladder" / "evidence" / "T_1-external.json").is_file())

    def test_a_worker_that_never_ran_does_not_cost_a_budget_slot(self):
        def spend(root_dir):
            return len((session_budget.load(root_dir) or {}).get("spawned", []))

        for child, kept in (((1, "", "API Error: 401 No auth credentials found"), 0), ((0, GOOD, ""), 1)):
            with self.subTest(kept=kept):
                root = self.repo()
                session_budget.save(root, {"size": "S", "max_agents": 2, "spawned": []})
                card_file = root / "card.md"
                card_file.write_text(CARD, encoding="utf-8")
                home = str(self.tmpdir())
                with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), \
                        mock.patch.object(ew, "claude_bin", return_value="claude"), \
                        mock.patch.object(ew, "get_key", return_value=("k", "test")), \
                        mock.patch.object(ew, "usage_usd", return_value=None), \
                        mock.patch.object(ew, "_spawn", return_value=child, create=True), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    ew.run({"--card": str(card_file), "--id": "T1", "--cwd": str(root)})
                self.assertEqual(spend(root), kept)


class Arguments(Temp):
    def test_a_misspelt_flag_is_named_instead_of_ignored(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = ew.main(["run", "--card", "x.md", "--idd", "T1"])
        self.assertEqual(code, 1)
        self.assertIn("--idd", err.getvalue())

    def test_a_number_flag_without_a_number_is_an_error(self):
        root = self.repo()
        card = root / "card.md"
        card.write_text(CARD, encoding="utf-8")
        err = io.StringIO()
        with mock.patch.object(ew, "claude_bin", return_value="claude"), \
                mock.patch.object(ew, "get_key", return_value=("k", "test")), \
                mock.patch.object(ew, "_spawn", side_effect=AssertionError("must not start a worker")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = ew.main(["run", "--card", str(card), "--cwd", str(root), "--timeout", "soon"])
        self.assertEqual(code, 1)
        self.assertIn("--timeout", err.getvalue())

    def test_the_contract_fallback_still_carries_the_packet_shape(self):
        with mock.patch.object(ew, "CONTRACT_FILES", [Path("no-such-contract.md")], create=True):
            text = ew.contract_text()
        self.assertIn("Retained:", text)
        self.assertIn("Changed:", text)


class Ledger(Temp):
    def test_an_unparsable_line_does_not_stop_the_row_being_written(self):
        root = self.repo()
        ledger = root / ".feature-ladder" / "runs" / "map-name" / "usage.jsonl"
        ledger.write_text('{"key": "other", "output": 1}\nnot json at all\n', encoding="utf-8")
        ew.record_ledger(root, "T1", "implementer", "m", {"num_turns": 2, "usage": {"output_tokens": 5}})
        rows = []
        for line in ledger.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
        self.assertEqual([r["key"] for r in rows], ["other", "external-T1"])
        self.assertEqual(rows[1]["output"], 5)

    def test_a_second_run_of_the_same_card_replaces_its_row(self):
        root = self.repo()
        for out in (5, 7):
            ew.record_ledger(root, "T1", "implementer", "m", {"usage": {"output_tokens": out}})
        ledger = root / ".feature-ladder" / "runs" / "map-name" / "usage.jsonl"
        rows = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["output"] for r in rows], [7])


class StateRoot(Temp):
    def test_a_linked_worktree_uses_the_main_checkout(self):
        main = self.tmpdir() / "main"
        main.mkdir()
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(g + ["init", "-q"], cwd=main, check=True, capture_output=True)
        subprocess.run(g + ["commit", "-q", "--allow-empty", "-m", "init"], cwd=main, check=True, capture_output=True)
        wt = main.parent / "wt"
        subprocess.run(g + ["worktree", "add", "-q", str(wt)], cwd=main, check=True, capture_output=True)
        self.assertEqual(Path(ew.repo_root(str(wt))), main)


FAKE_CLAUDE = r'''
import json, os, sys
args = sys.argv[1:]
card = sys.stdin.read()
contract = ""
if "--append-system-prompt-file" in args:
    contract = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
with open(os.environ["FAKE_CLAUDE_SEEN"], "w", encoding="utf-8") as fh:
    json.dump({"args": args, "contract": contract, "card": card}, fh)
if os.environ.get("FAKE_CLAUDE_MODE") == "401":
    sys.stderr.write("API Error: 401 No auth credentials found\n")
    sys.exit(1)
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps({"type": "result", "num_turns": 2, "usage": {"output_tokens": 9},
                  "result": "TASK T1 - done\nChanged: a.py → b.py ✔\nRetained: none - n/a"}, ensure_ascii=False))
'''


class CommandLine(Temp):
    """The whole script as a process, with a fake `claude` on PATH (a .cmd shim on
    Windows, which is how npm installs it) and a legacy console encoding."""

    def cli(self, mode="ok"):
        root = self.repo()
        (root / ".feature-ladder" / "providers.json").write_text(
            json.dumps({"openrouter": {"base_url": "http://127.0.0.1:9", "key_env": "FAKE_OPENROUTER_KEY"}}),
            encoding="utf-8")
        card = root / "card.md"
        card.write_text(CARD, encoding="utf-8")
        bindir = self.tmpdir()
        (bindir / "fake_claude.py").write_text(FAKE_CLAUDE, encoding="utf-8")
        if sys.platform == "win32":
            (bindir / "claude.cmd").write_text(f'@echo off\r\n"{sys.executable}" "%~dp0fake_claude.py" %*\r\n',
                                               encoding="utf-8")
        else:
            sh = bindir / "claude"
            sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{bindir}/fake_claude.py" "$@"\n', encoding="utf-8")
            sh.chmod(0o755)
        home = str(self.tmpdir())
        seen = bindir / "seen.json"
        env = dict(os.environ, HOME=home, USERPROFILE=home, FAKE_OPENROUTER_KEY="k", FAKE_CLAUDE_MODE=mode,
                   FAKE_CLAUDE_SEEN=str(seen), PYTHONIOENCODING="cp1252", PYTHONUTF8="0",
                   PATH=str(bindir) + os.pathsep + os.environ.get("PATH", ""))
        for k in ("FEATURE_LADDER_WORKERS", "FEATURE_LADDER_WORKER_MODEL", "OPENROUTER_API_KEY"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, str(SCRIPTS / "external_worker.py"), "run", "--card", str(card),
                            "--id", "T1", "--cwd", str(root)], capture_output=True, env=env, timeout=120)
        return r, (json.loads(seen.read_text(encoding="utf-8")) if seen.is_file() else None), root

    def test_a_packet_with_non_cp1252_characters_prints_and_exits_0(self):
        r, seen, root = self.cli()
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out + r.stderr.decode("utf-8", "replace"))
        self.assertIn("TASK T1 - done", out)
        self.assertIn("a.py → b.py ✔", out)
        self.assertTrue((root / ".feature-ladder" / "evidence" / "T1-external.json").is_file())

    def test_the_whole_contract_reaches_a_cmd_shim(self):
        _, seen, _ = self.cli()
        self.assertGreater(len(seen["contract"]), 500)
        self.assertIn("Retained:", seen["contract"])
        self.assertIn("TASK T1", seen["card"])

    def test_a_rejected_key_exits_3(self):
        r, _, _ = self.cli(mode="401")
        self.assertEqual(r.returncode, 3, r.stdout.decode("utf-8", "replace"))


if __name__ == "__main__":
    unittest.main()
