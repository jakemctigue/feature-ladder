"""From a concluded brainstorm to built features, with the architecture in Hindsight.

Pins: the brainstorm asks as many questions as it takes (no cap on the first
round; follow-up rounds for details still open or newly needed, bounded by
MAX_ROUNDS, all before anything is built); a concluded sheet hands its queue
to /feature-ladder:feature --from-brainstorm (by slug, by feature id, and a
plan-only sheet can be built later); `remember` turns the settled
architecture into one gate-passing fact per detail, queued apart from the
per-session cap and tagged `architecture`; and recall gives a feature the
architecture decisions that bear on it even when the request shares no word
with them, without crowding out its other memories.

No test touches the network.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PLUGIN = HERE.parent
REPO = PLUGIN.parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "hindsight-memory" / "scripts"))

import test_brainstorm as tb  # noqa: E402
from test_brainstorm import ANSWERS, REQUEST, brainstorm, opt, scout_sheet  # noqa: E402
import confidence as conf  # noqa: E402
import memory_gate  # noqa: E402

HM = PLUGIN / "skills" / "hindsight-memory" / "scripts"


class Base(tb.Base):
    def app_path(self, app):
        return app["written_to"]

    def load(self, app):
        return json.loads(Path(app["written_to"]).read_text(encoding="utf-8"))


class AsManyQuestionsAsItTakes(Base):
    def test_the_first_round_has_no_question_cap(self):
        sheet = scout_sheet()
        for i in range(45):   # an app with far more details than the old 40-question budget
            sheet["dimensions"][f"rule_{i}"] = {"group": "application", "header": f"Rule {i}",
                                                "question": f"Which pricing rule {i}?", "value": f"rule {i}",
                                                "confidence": "low", "options": [opt(f"rule {i}"), opt("other")]}
        dec = json.loads(self.run_cli("decide", self.sheet_file(sheet)).stdout)
        asked = {q["detail"] for q in dec["questions"]} | {q["detail"] for q in dec["open_text"]}
        self.assertTrue({f"rule_{i}" for i in range(45)} <= asked)
        self.assertFalse([a for a in dec["assumed"] if a.get("note") == "question budget used up"])
        capped = json.loads(self.run_cli("decide", self.sheet_file(sheet), "--max", "5").stdout)
        self.assertEqual(len(capped["questions"]) + len(capped["open_text"]), 5)

    def test_open_details_lead_to_a_follow_up_round_before_building(self):
        app = self.confirm(ANSWERS)
        self.assertIn("success", app["followup"])           # left blank in the first round
        self.assertEqual(app["next_step"], "revise")
        self.assertEqual(app["rounds_left"], brainstorm.MAX_ROUNDS)
        self.assertTrue(any("household-model" in r for r in app["revise_because"]))   # entities changed

    def test_follow_up_asks_the_proposals_and_new_details_then_settles_them(self):
        app = self.confirm(ANSWERS)
        path = self.app_path(app)
        details = {"details": {
            "success": {"value": "Two phones see the same list within two seconds.", "confidence": "low",
                        "options": [opt("Two-phone sync demo"), opt("Unit tests only")]},
            "sharing_invites": {"group": "application", "header": "Invites", "question": "How does someone join?",
                                "value": "An invite link per household, valid seven days.", "confidence": "medium",
                                "options": [opt("Invite link"), opt("Email invite")]},
            "hosting": {"value": "Firebase Hosting", "confidence": "high"},   # repo-detected: not asked again
            "error_copy": {"group": "application", "header": "Errors", "value": "Plain one-line messages.",
                           "confidence": "high"},                       # high: settled without a question
        }}
        out = json.loads(self.run_cli("followup", path, self.write("f.json", details)).stdout)
        self.assertTrue(out["asking"])
        self.assertEqual(out["round"], 1)
        self.assertEqual([q["detail"] for q in out["questions"]], ["success", "sharing_invites"])
        self.assertTrue(out["questions"][0]["options"][0]["label"].endswith("(Recommended)"))
        mid = self.load(app)
        self.assertEqual(mid["dimensions"]["hosting"]["status"], "detected")
        self.assertEqual(mid["dimensions"]["error_copy"]["status"], "assumed")

        blanks = [q["detail"] for q in out["open_text"]]           # nothing proposed: own words
        self.assertIn("surfaces", blanks)
        answers = dict({k: "Not needed" for k in blanks}, sharing_invites="Email invite")
        done = json.loads(self.run_cli("refine", path, self.write("a.json", {"answers": answers})).stdout)
        d = done["dimensions"]
        self.assertEqual((d["success"]["status"], d["success"]["source"]), ("assumed", "scout:low"))
        self.assertEqual((d["sharing_invites"]["status"], d["sharing_invites"]["value"]), ("decided", "Email invite"))
        self.assertEqual(done["round"], 1)
        self.assertEqual(done["followup"], [])
        self.assertEqual(done["next_step"], "revise")      # the new detail needs a feature
        self.assertTrue(any("sharing_invites" in r for r in done["revise_because"]))

        feats = [dict(f, covers=f["covers"]) for f in scout_sheet()["features"] if f["id"] != "push"]
        feats.append({"id": "invites", "title": "Email invites", "depends_on": ["household-model"],
                      "request": "Send an email invite that adds the recipient to the household when accepted.",
                      "covers": ["sharing_invites"]})
        built = json.loads(self.run_cli("features", path, self.write("ff.json", {"features": feats})).stdout)
        self.assertEqual(built["next_step"], "build", built["revise_because"])
        self.assertEqual(built["handoff"]["command"], f"/feature-ladder:feature --from-brainstorm {self.slug}")

    def test_follow_up_rounds_are_bounded_then_proposals_stand(self):
        app = self.confirm(ANSWERS)
        path = self.app_path(app)
        for i in range(brainstorm.MAX_ROUNDS):
            out = json.loads(self.run_cli("followup", path, self.write("f.json", {"details": {}})).stdout)
            self.assertTrue(out["asking"])
            self.assertIn("success", [q["detail"] for q in out["open_text"]])   # still asked in own words
            self.run_cli("refine", path)                                              # left blank again
        last = self.load(app)
        self.assertEqual((last["round"], last["followup"], last["rounds_left"]), (brainstorm.MAX_ROUNDS, [], 0))
        out = json.loads(self.run_cli("followup", path, self.write("f.json", {"details": {
            "success": {"value": "A two-phone demo.", "confidence": "low"}}})).stdout)
        self.assertFalse(out["asking"])
        self.assertEqual(out["questions"] + out["open_text"], [])
        self.assertEqual(self.load(app)["dimensions"]["success"]["status"], "assumed")


class HandOff(Base):
    def next(self, *args):
        return json.loads(self.run_cli("next", *args).stdout)

    def test_a_plan_only_brainstorm_is_built_from_the_feature_command_by_slug(self):
        self.confirm(dict(ANSWERS, decisions=dict(ANSWERS["decisions"], build="Plan only")))
        self.assertIsNone(self.next()["feature"])
        n = self.next("--app", self.slug, "--build", "all")
        self.assertEqual((n["build"], n["slug"], n["feature"]["id"]), ("all", self.slug, "household-model"))
        self.assertIn("context", n["feature"]["brainstorm"])

    def test_one_feature_by_id_and_its_dependencies_are_respected(self):
        self.confirm(ANSWERS)
        waits = self.next("--feature", "list-screen")
        self.assertIsNone(waits["feature"])
        self.assertIn("waits on household-model, sign-in", waits["note"])
        self.assertEqual(self.next("--feature", "sign-in")["feature"]["id"], "sign-in")
        bad = self.run_cli("next", "--app", "no-such-app", ok=False)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("no confirmed app sheet", bad.stderr)

    def test_the_readable_sheet_says_how_to_continue(self):
        app = self.confirm(ANSWERS)
        md = Path(app["readable"]).read_text(encoding="utf-8")
        self.assertIn(f"`/feature-ladder:feature --from-brainstorm {self.slug}`", md)


class ArchitectureMemory(Base):
    def remember(self, *args):
        return json.loads(self.run_cli("remember", "--no-flush", *args).stdout)

    def test_the_settled_architecture_becomes_one_fact_per_detail(self):
        self.confirm(ANSWERS)
        out = self.remember()
        facts = out["facts"]
        ids = [f.split("`arch:")[1].split("`")[0] for f in facts]
        self.assertIn("auth", ids)                 # proposed by the scout and kept
        self.assertIn("entities", ids)             # the user changed it
        self.assertIn("roles", ids)
        self.assertNotIn("frontend", ids)          # the repo already shows it
        self.assertNotIn("users", ids)             # application, not architecture
        self.assertNotIn("notifications", ids)     # not needed
        for f in facts:
            self.assertTrue(f.startswith("[decision] `arch:"), f)
            memory_gate.parse_line(f)              # passes the gate: one anchored fact <= 280 chars
        q = self.root / ".feature-ladder" / "session-memory-architecture.md"
        self.assertEqual(len(q.read_text(encoding="utf-8").splitlines()), len(facts))
        self.assertEqual(self.remember()["queued"], 0)   # queued once
        self.assertIn("Architecture stored in Hindsight memory",
                      Path(out["sheet"]).with_suffix(".md").read_text(encoding="utf-8"))

    def test_a_long_value_is_cut_to_one_fact(self):
        app = {"dimensions": {"backend": {"group": "architecture", "header": "Backend", "status": "decided",
                                          "value": "One. Two. Three. " + "x" * 400}}}
        (fact,) = brainstorm.architecture_facts(app)
        kind, text = memory_gate.parse_line(fact)
        self.assertLessEqual(len(text), memory_gate.MAX_CHARS)

    def test_flush_writes_the_architecture_outside_the_session_cap_tagged_architecture(self):
        self.confirm(ANSWERS)
        arch = self.remember()["facts"]
        fl = self.root / ".feature-ladder"
        lines = [f"- [convention] Item rows use `ItemRow{i}` in src/list.ts for list rendering." for i in range(14)]
        (fl / "session-memory.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = memory_gate.flush(self.root, emit=True, project=["grocer"])
        self.assertEqual(out["architecture"], arch)
        self.assertEqual(len(out["would_write"]), len(arch) + memory_gate.SESSION_RETAINS)
        self.assertEqual(len(out["deferred"]), 14 - memory_gate.SESSION_RETAINS)
        tags = {c["content"]: c["tags"] for c in out["retain_calls"]}
        first = memory_gate.parse_line(arch[0])[1]
        self.assertEqual(tags[first], ["project:grocer", "architecture", "decision"])
        self.assertIn("feature:session", next(t for c, t in tags.items() if "ItemRow0" in c))
        again = memory_gate.flush(self.root, emit=True, project=["grocer"])
        self.assertEqual(again["architecture"], [])          # recorded: never written twice
        self.assertFalse([r for r in again["rejected"] if "arch:" in r["line"]])

    def test_the_gate_keeps_the_architecture_tag(self):
        ok, _, args = memory_gate.gate("retain", {"content": "`arch:auth` Sign-in: Firebase Auth with Google.",
                                                  "tags": ["architecture", "decision"]}, self.root, "grocer")
        self.assertTrue(ok)
        self.assertEqual(args["tags"], ["project:grocer", "architecture", "decision"])

    def test_an_unattended_flush_starts_for_an_architecture_queue_alone(self):
        self.confirm(ANSWERS)
        self.remember()
        self.assertNotEqual(memory_gate.auto_skip(self.root), "nothing queued")


def arch_mems(*lines):
    return conf.parse_memories("\n".join(lines))


ARCH = [
    "[decision] `arch:auth` Sign-in: Firebase Auth with Google sign-in; sessions kept by the SDK.",
    "[decision] `arch:data_store` Data store: Firestore, one collection group per household.",
    "[decision] `arch:hosting` Hosting: Firebase Hosting, preview channels per PR.",
    "[decision] `arch:files_media` Files: none; no uploads.",
    "[decision] `arch:stack` Stack: TypeScript 5, React 19, Vite 7.",
]


class RecallTheArchitecture(unittest.TestCase):
    def test_a_feature_sees_the_decisions_in_its_area_even_without_shared_words(self):
        kept, dropped = conf.select_relevant(arch_mems(*ARCH), "add a sign-up page with an account screen", [])
        texts = [m["text"] for m in kept]
        self.assertTrue(any("arch:auth" in t for t in texts))
        self.assertTrue(any("arch:stack" in t for t in texts))          # the stack bears on every card
        self.assertFalse(any("arch:files_media" in t for t in texts))   # nothing about files here
        self.assertGreater(dropped["off_topic"], 0)

    def test_architecture_has_its_own_allowance_and_comes_first(self):
        others = [f"[pitfall] The sign-up page in src/signup{i}.ts double-submits the form." for i in range(6)]
        kept, dropped = conf.select_relevant(arch_mems(*(others + ARCH)), "fix the sign-up page form submit",
                                             ["src/signup0.ts"])
        arch = [m for m in kept if conf.arch_detail(m)]
        plain = [m for m in kept if not conf.arch_detail(m)]
        self.assertEqual(len(plain), conf.MAX_RELEVANT)
        self.assertLessEqual(len(arch), conf.ARCH_CAP)
        self.assertTrue(arch)
        self.assertTrue(conf.arch_detail(kept[0]))

    def test_the_newest_decision_per_detail_wins(self):
        raw = json.dumps({"results": [
            {"text": "[decision] `arch:auth` Sign-in: email links.", "mentioned_at": "2026-01-01T00:00:00Z"},
            {"text": "[decision] `arch:auth` Sign-in: Google sign-in only.", "mentioned_at": "2026-06-01T00:00:00Z"},
        ]})
        kept, dropped = conf.select_relevant(conf.parse_memories(raw), "add a login button", [])
        self.assertIn("Google", kept[0]["text"])
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped["duplicate"], 1)

    def test_recall_py_can_leave_the_architecture_out_or_show_it_all(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "recall.json"
            src.write_text("\n".join(ARCH), encoding="utf-8")

            def run(*extra):
                r = subprocess.run([sys.executable, str(HM / "recall.py"), "--query", "add a sign-up page",
                                    "--from", str(src), "--cwd", tmp, *extra],
                                   capture_output=True, text=True, encoding="utf-8", timeout=60)
                self.assertEqual(r.returncode, 0, r.stderr)
                return json.loads(r.stdout)
            self.assertGreater(run()["architecture"], 0)
            self.assertEqual(run("--no-architecture")["architecture"], 0)
            whole = run("--query", "build the whole app: sign-in, data, hosting, files", "--arch-cap", "40")
            self.assertEqual(whole["architecture"], len(ARCH))


class Wiring(unittest.TestCase):
    def read(self, *parts):
        return REPO.joinpath(*parts).read_text(encoding="utf-8")

    def test_the_feature_command_takes_a_concluded_brainstorm(self):
        feature = self.read("plugins", "feature-ladder", "commands", "feature.md")
        self.assertIn("| `--from-brainstorm", feature)
        self.assertIn("[--from-brainstorm", feature.split("---")[1])
        for step in ("B followup", "B refine", "B remember", "--from-brainstorm"):
            self.assertIn(step, feature)
        app = self.read("claude-app", "model-ladder-router", "SKILL.md")
        for step in ("B followup", "B refine", "B remember", "--from-brainstorm"):
            self.assertIn(step, app)

    def test_guide_covers_follow_up_rounds(self):
        r = subprocess.run([sys.executable, str(tb.ROUTER / "brainstorm.py"), "guide"], capture_output=True,
                           text=True, encoding="utf-8", timeout=60)
        for word in ("followup", "floor, not a ceiling", "no limit on questions"):
            self.assertIn(word, r.stdout)
        self.assertNotIn("{{", r.stdout)


if __name__ == "__main__":
    unittest.main()
