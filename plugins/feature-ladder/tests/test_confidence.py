"""Tests for confidence.py, the memory-backed surface that replaced the gates.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "skills" / "orchestration-ladder" / "scripts" / "confidence.py"
sys.path.insert(0, str(SCRIPT.parent))

import confidence as conf  # noqa: E402

PITFALL = "[pitfall] firebase deploy to bossforgedev timed out twice; retry with --only hosting"
CONVENTION = "[convention] map routes use requireOwnerPreviewAccess before resolveMapOwner; tests pass with npx vitest run server/mapRoutes"
TEST_STRATEGY = "[test-strategy] npm test plus npm run build is the maps gate; both pass green on the run branch"


def mems(*lines):
    return conf.parse_memories("\n".join(lines))


class Lexicon(unittest.TestCase):
    def test_polarity_direction(self):
        self.assertGreater(conf.polarity("the deploy works and the tests pass"), 0)
        self.assertLess(conf.polarity("the deploy timed out and failed"), 0)
        self.assertEqual(conf.polarity("update the readme"), 0.0)

    def test_negation_flips(self):
        self.assertGreater(conf.polarity("it does not fail any more"), 0)
        self.assertLess(conf.polarity("this never works"), 0)


class Parsing(unittest.TestCase):
    def test_lines_and_kinds(self):
        m = mems(PITFALL, "- " + CONVENTION, "plain note without a kind")
        self.assertEqual([x["kind"] for x in m], ["pitfall", "convention", "note"])
        self.assertTrue(m[0]["text"].startswith("firebase deploy"))

    def test_recall_json_of_any_shape(self):
        payload = {"results": [{"text": PITFALL, "tags": ["project:x", "pitfall"]},
                               {"nested": {"content": "map sessions are stored in Firestore", "metadata": {"kind": "interface"}}}]}
        m = conf.parse_memories(json.dumps(payload))
        self.assertEqual([x["kind"] for x in m], ["pitfall", "interface"])


class Decisions(unittest.TestCase):
    def test_small_card_in_unknown_territory_proceeds(self):
        r = conf.decide("card", "add a /healthz endpoint", ["server/routes.ts"], size="S")
        self.assertEqual(r["decision"], "proceed")
        self.assertEqual(r["surface"]["mood"], "unknown")
        self.assertGreaterEqual(r["confidence"], 0.60)

    def test_large_card_in_unknown_territory_reshards(self):
        r = conf.decide("card", "rebuild the maps workflow", ["a.ts", "b.ts", "c.ts", "d.ts"], size="L")
        self.assertEqual(r["decision"], "reshard")
        self.assertGreaterEqual(r["reshard"]["shards"], 2)
        self.assertIn("split the card", r["reshard"]["advice"])

    def test_a_relevant_pitfall_makes_the_surface_wary(self):
        r = conf.decide("card", "deploy the maps hosting to firebase", ["firebase.json"], size="S",
                        memories=mems(PITFALL))
        self.assertEqual(r["surface"]["mood"], "wary")
        self.assertEqual(r["surface"]["relevant_pitfalls"], 1)
        self.assertEqual(r["decision"], "reshard")
        self.assertIn("touches this", r["surface"]["texture"])

    def test_an_unrelated_pitfall_weighs_less(self):
        near = conf.decide("card", "deploy the maps hosting to firebase", ["firebase.json"], size="M", memories=mems(PITFALL))
        far = conf.decide("card", "rename a React prop", ["src/App.tsx"], size="M", memories=mems(PITFALL))
        self.assertLess(near["confidence"], far["confidence"])

    def test_conventions_and_test_strategies_raise_confidence(self):
        r = conf.decide("card", "add owner checks to map routes", ["server/mapRoutes.ts"], size="M",
                        memories=mems(CONVENTION, TEST_STRATEGY))
        self.assertEqual(r["decision"], "proceed")
        self.assertEqual(r["surface"]["mood"], "confident")
        self.assertGreater(r["confidence"], conf.SIZE_PRIOR["M"])

    def test_evidence_moves_the_number(self):
        base = conf.decide("card", "x", [], size="S")["confidence"]
        self.assertLess(conf.decide("card", "x", [], size="S", failed=2)["confidence"], base)
        self.assertGreater(conf.decide("card", "x", [], size="S", passed=2)["confidence"], base)
        self.assertEqual(conf.decide("card", "x", [], size="S", failed=2)["decision"], "reshard")

    def test_action_mode_uses_the_tier_prior_and_advises_a_preview(self):
        never = conf.decide("action", "gh pr merge 3", tier="never")
        self.assertEqual(never["decision"], "reshard")
        self.assertTrue(never["reshard"]["preview_first"])
        self.assertEqual(conf.decide("action", "firebase deploy --project dev", tier="mundane")["decision"], "proceed")

    def test_threshold_is_configurable(self):
        r = conf.decide("card", "x", [], size="S", threshold=0.9)
        self.assertEqual((r["threshold"], r["decision"]), (0.9, "reshard"))

    def test_remote_scorer_falls_back_to_the_lexicon(self):
        r = conf.decide("card", "add owner checks to map routes", ["server/mapRoutes.ts"], size="S",
                        memories=mems(CONVENTION), scorer="jev",
                        cfg={"key_env": "FEATURE_LADDER_TEST_NO_SUCH_KEY"})
        self.assertEqual(r["scorer"], "lexicon")
        self.assertTrue(any("jev scorer unavailable" in n for n in r.get("notes", [])))

    def test_it_never_blocks(self):
        # Every outcome is advice: the result always carries a decision and reasons, never a denial.
        for size in ("S", "M", "L"):
            r = conf.decide("card", "x", [], size=size, memories=mems(PITFALL, PITFALL, PITFALL), failed=3)
            self.assertIn(r["decision"], ("proceed", "reshard"))
            self.assertGreaterEqual(r["confidence"], 0.05)
            self.assertTrue(r["reasons"])


JEV = Path(__file__).resolve().parent / "fixtures" / "jev"


def replay(name):
    """A transport that returns a recorded response body; it never touches the network."""
    f = JEV / f"{name}.json"
    f = f if f.exists() else JEV / f"{name}.txt"
    body = f.read_text(encoding="utf-8").replace(chr(13) + chr(10), chr(10))
    return lambda payload: body


class JevScorer(unittest.TestCase):
    MEMS = [{"kind": "pitfall", "text": "firebase deploy timed out twice"},
            {"kind": "convention", "text": "routes use requireOwnerPreviewAccess"},
            {"kind": "decision", "text": "the map owner is resolved server side"}]

    def run_replay(self, name):
        return conf.sentiments(self.MEMS, "jev", {}, replay(name))

    def test_full_valid(self):
        scores, used, note = self.run_replay("full_valid")
        self.assertEqual((scores, used, note), ([-0.8, 0.7, 0.0], "jev", None))

    def test_partial_falls_back_per_fact(self):
        scores, used, note = self.run_replay("partial")
        self.assertEqual(used, "jev")
        self.assertEqual(scores[0], -0.8)
        self.assertEqual(scores[1], conf.polarity(self.MEMS[1]["text"]))
        self.assertEqual(scores[2], 0.5)
        self.assertIn("1 of 3", note)

    def test_bad_and_duplicate_indexes_are_ignored(self):
        scores, used, _ = self.run_replay("bad_index")
        self.assertEqual(used, "jev")
        self.assertEqual(scores[:2], [-0.6, 0.4])
        self.assertEqual(scores[2], conf.polarity(self.MEMS[2]["text"]))  # duplicate index: neither entry trusted

    def test_unusable_bodies_fall_back_for_every_fact(self):
        lex = [conf.polarity(m["text"]) for m in self.MEMS]
        for name in ("malformed", "wrong_shape", "scores_not_indexed", "not_json"):
            with self.subTest(name):
                scores, used, note = self.run_replay(name)
                self.assertEqual((scores, used), (lex, "lexicon"))
                self.assertIn("jev scorer unavailable", note)

    def test_scores_are_clamped_and_non_numeric_falls_back(self):
        scores, used, _ = self.run_replay("out_of_range_score")
        self.assertEqual(used, "jev")
        self.assertEqual(scores[:2], [-1.0, 1.0])
        self.assertEqual(scores[2], conf.polarity(self.MEMS[2]["text"]))

    def test_nan_and_inf_are_rejected(self):
        body = '{"scores": [{"i": 0, "score": NaN}, {"i": 1, "score": Infinity}, {"i": 2, "score": 0.2}]}'
        self.assertEqual(conf.remote_sentiments(self.MEMS, {}, lambda p: body), [None, None, 0.2])

    def test_transport_error_falls_back(self):
        def boom(payload):
            raise OSError("HTTP 500")
        scores, used, note = conf.sentiments(self.MEMS, "jev", {}, boom)
        self.assertEqual(used, "lexicon")
        self.assertIn("HTTP 500", note)

    def test_dict_body_is_accepted(self):
        self.assertEqual(conf.remote_sentiments(self.MEMS, {}, lambda p: {"scores": [{"i": 1, "score": 1}]}),
                         [None, 1.0, None])

    def test_request_carries_anchors_index_and_kind(self):
        seen = []
        conf.remote_sentiments(self.MEMS, {}, lambda p: seen.append(p) or "{}")
        system, user = seen[0]["messages"][0]["content"], json.loads(seen[0]["messages"][1]["content"])
        self.assertTrue(all(w in system for w in ("-1", "0", "+1", "failure", "neutral", "success")))
        self.assertEqual([(u["i"], u["kind"]) for u in user], [(0, "pitfall"), (1, "convention"), (2, "decision")])
        self.assertEqual(seen[0]["model"], "typesafe/jev-router")


class Anchors(unittest.TestCase):
    """Which memories count as "touching these files"."""
    UNRELATED = ["[pitfall] the Firestore composite index build fails on deploy",
                 "[convention] components live in src/components",
                 "[pitfall] a json body over the limit fails with 413; the main thread hangs"]

    def relevant(self, paths, lines=None, action="Rename the settings page title"):
        surf = conf.surface(mems(*(lines or self.UNRELATED)), action, paths)
        return [i["text"] for i in surf["items"] if i["relevant"]]

    def test_generic_file_names_and_extensions_do_not_make_a_memory_relevant(self):
        for path in ("src/index.ts", "package.json", "src/main.ts", "a.ts"):
            with self.subTest(path):
                self.assertEqual(self.relevant([path]), [])

    def test_the_path_alone_does_not_change_the_decision(self):
        a = conf.decide("card", "Rename the settings page title", ["src/index.ts"], "M", memories=mems(*self.UNRELATED))
        b = conf.decide("card", "Rename the settings page title", ["src/title.ts"], "M", memories=mems(*self.UNRELATED))
        self.assertEqual((a["confidence"], a["decision"]), (b["confidence"], b["decision"]))

    def test_the_file_its_path_and_a_distinctive_name_still_count(self):
        lines = ["[pitfall] server/mapRoutes.ts writes must await the batch commit",
                 "[pitfall] mapRoutes.ts fails when the owner is missing",
                 "[convention] npx vitest run server/mapRoutes is the gate",
                 "[pitfall] the routes folder has stale fixtures"]
        hits = self.relevant(["server/mapRoutes.ts"], lines)
        self.assertEqual(len(hits), 3, hits)
        self.assertFalse(any("stale fixtures" in h for h in hits))

    def test_a_file_name_is_matched_whole(self):
        self.assertEqual(self.relevant(["src/a.ts"], ["[pitfall] data.ts fails on empty input"]), [])
        self.assertEqual(len(self.relevant(["src/data.ts"], ["[pitfall] data.ts fails on empty input"])), 1)

    def test_words_of_the_action_still_count(self):
        self.assertEqual(len(self.relevant([], ["[pitfall] firebase deploy timed out twice"],
                                           action="deploy hosting to firebase")), 1)


class Cli(unittest.TestCase):
    def setUp(self):
        # A home of its own: no real ~/.claude/feature-ladder/providers.json (and so
        # no real scorer key or paid request) can reach these runs.
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)

    def run_cli(self, *args, cwd=None):
        env = dict(os.environ, HOME=self._home.name, USERPROFILE=self._home.name)
        for k in ("OPENROUTER_API_KEY", "FEATURE_LADDER_CONFIDENCE_THRESHOLD"):
            env.pop(k, None)
        r = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                           encoding="utf-8", timeout=60, cwd=cwd or self._home.name, env=env)
        return r.returncode, r.stdout, r.stderr

    def test_the_real_home_is_not_read(self):
        (Path(self._home.name) / ".claude" / "feature-ladder").mkdir(parents=True)
        (Path(self._home.name) / ".claude" / "feature-ladder" / "providers.json").write_text(
            json.dumps({"confidence": {"scorer": "jev", "key_env": "FL_TEST_NO_SUCH_KEY"}}), encoding="utf-8")
        code, out, err = self.run_cli("card", "--action", "add healthz endpoint", "--size", "S", "--memories",
                                      self._lines("[convention] the healthz endpoint passes green"))
        self.assertEqual(code, 0, err)
        self.assertIn("FL_TEST_NO_SUCH_KEY", " ".join(json.loads(out).get("notes", [])))

    def _lines(self, *lines):
        f = Path(self._home.name) / "mem.txt"
        f.write_text("\n".join(lines), encoding="utf-8")
        return str(f)

    def test_a_missing_recall_file_is_no_memories_with_a_note(self):
        code, out, err = self.run_cli("card", "--action", "Add export", "--paths", "src/x.py", "--size", "S",
                                      "--memories", str(Path(self._home.name) / "runs" / "s" / "recall.json"))
        self.assertEqual(code, 0, err)
        r = json.loads(out)
        self.assertEqual(r["surface"]["memories"], 0)
        self.assertIn("recall.json", " ".join(r["notes"]))

    def test_bad_numbers_are_an_error_message_not_a_traceback_or_a_silent_default(self):
        for bad in (("--threshold",), ("--threshold", "high"), ("--threshold", "1.5"),
                    ("--checks-passed", "two"), ("--checks-failed", "-1")):
            with self.subTest(bad):
                code, out, err = self.run_cli("card", "--action", "Add export", "--size", "S", *bad)
                self.assertEqual(code, 1, out)
                self.assertNotIn("Traceback", err)
                self.assertIn(bad[0], err)

    def test_action_tier_is_the_same_from_a_subfolder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
            (root / ".feature-ladder").mkdir()
            (root / ".feature-ladder" / "run.json").write_text(
                json.dumps({"active": True, "base": "develop", "branch": "feature-ladder/x"}), encoding="utf-8")
            (root / "src").mkdir()
            tiers = []
            for cwd in (root, root / "src"):
                code, out, err = self.run_cli("action", "--action", "git push origin develop", cwd=str(cwd))
                self.assertIn(code, (0, 5), err)
                tiers.append(json.loads(out)["tier"])
            self.assertEqual(tiers[0], tiers[1])
            self.assertNotEqual(tiers[0], "none")

    def test_exit_codes_and_json(self):
        code, out, err = self.run_cli("card", "--action", "add healthz", "--paths", "server/routes.ts", "--size", "S")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["decision"], "proceed")
        code, out, _ = self.run_cli("card", "--action", "rebuild everything", "--size", "L")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out)["decision"], "reshard")

    def test_memories_file_and_out_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            mf = Path(tmp) / "recall.json"
            mf.write_text(json.dumps({"results": [{"text": PITFALL}]}), encoding="utf-8")
            out_f = Path(tmp) / "confidence.json"
            code, out, err = self.run_cli("card", "--action", "deploy maps hosting to firebase", "--paths", "firebase.json",
                                          "--size", "S", "--memories", str(mf), "--out", str(out_f))
            self.assertEqual(code, 5, err)
            self.assertEqual(json.loads(out_f.read_text(encoding="utf-8"))["surface"]["pitfalls"], 1)

    def test_action_mode_classifies_the_command_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = self.run_cli("action", "--action", "gh pr merge 3", cwd=tmp)
            self.assertEqual(code, 5, err)
            self.assertEqual(json.loads(out)["tier"], "never")

    def test_score_debug(self):
        code, out, _ = self.run_cli("score", "it works", "it failed")
        self.assertEqual(code, 0)
        pols = [x["polarity"] for x in json.loads(out)]
        self.assertGreater(pols[0], 0)
        self.assertLess(pols[1], 0)


if __name__ == "__main__":
    unittest.main()
