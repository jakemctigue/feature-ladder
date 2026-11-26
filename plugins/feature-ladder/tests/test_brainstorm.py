"""The --brainstorm switch: every application and architecture detail of the
app is settled at the very front, in one round, before the work is narrowed
into features and built.

Pins: the repo scan settles what manifests settle (names only for secrets);
every applicable detail that is not high confidence is asked, application
first, with the session questions and the build choice in the same round;
the answers become a confirmed sheet and a feature queue in build order; each
feature's cards carry only the details it covers; and the whole phase adds no
agent beyond the scout.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
ROUTER = PLUGIN / "skills" / "complexity-router" / "scripts"
LADDER = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(ROUTER))
sys.path.insert(0, str(LADDER))

import brainstorm  # noqa: E402
import intake  # noqa: E402
import session_budget  # noqa: E402

REQUEST = "build a shared grocery list app for households"


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(tmp):
    root = Path(tmp).resolve()
    git(root, "init", "-q")
    (root / "package.json").write_text(json.dumps({
        "scripts": {"test": "vitest run"},
        "dependencies": {"react": "^19", "react-dom": "^19", "firebase": "^12", "@sentry/react": "^9"},
        "devDependencies": {"vite": "^7", "vitest": "^3", "typescript": "^5"}}), encoding="utf-8")
    (root / "firebase.json").write_text("{}", encoding="utf-8")
    (root / "firestore.rules").write_text("rules_version = '2';\n", encoding="utf-8")
    (root / ".firebaserc").write_text(json.dumps({"projects": {"default": "grocer-prod", "dev": "grocer-dev"}}),
                                      encoding="utf-8")
    (root / ".env.example").write_text("VITE_FIREBASE_API_KEY=\nSENTRY_DSN=\nAPP_NAME=grocer\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (root / "src").mkdir()
    for name in ("App.tsx", "list.ts", "list.test.ts"):
        (root / "src" / name).write_text("export const x = 1;\n", encoding="utf-8")
    (root / "README.md").write_text("# Grocer\nUses the STRIPE_SECRET_KEY secret one day.\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init")
    return root


def opt(label, description=""):
    return {"label": label, "description": description}


def scout_sheet():
    """What the scout writes: only what it settled or corrected, plus the draft features."""
    return {
        "request": REQUEST,
        "dimensions": {
            "purpose": {"value": "A household keeps one shared grocery list that stays in sync.",
                        "confidence": "high", "evidence": ["the request"]},
            "users": {"value": "Members of one household, a handful per list, public sign-up.", "confidence": "medium",
                      "options": [opt("Households, public sign-up"), opt("Invite-only beta")]},
            "workflows": {"value": "Add an item, tick it off in the shop, clear the ticked items.",
                          "confidence": "medium",
                          "options": [opt("Add, tick, clear"), opt("Add, tick, clear and reorder by aisle")]},
            "entities": {"value": "households, lists, items (name, quantity, ticked, addedBy).", "confidence": "low",
                         "options": [opt("One list per household", "households/{id}/items"),
                                     opt("Many lists per household", "households/{id}/lists/{id}/items")]},
            "roles": {"value": "Every member can edit; only the owner can remove members.", "confidence": "low"},
            "notifications": {"confidence": "low",
                              "options": [opt("None for now"), opt("Push when someone adds an item", "Needs FCM")]},
            "content": {"status": "not_applicable", "evidence": ["English only; nothing in the request"]},
            "edge_cases": {"confidence": "low"},   # nothing proposed: asked in the user's own words
            "auth": {"value": "Firebase Auth with Google sign-in.", "confidence": "medium",
                     "options": [opt("Google sign-in"), opt("Email link")]},
            "data_store": {"value": "Firestore, realtime listeners, one collection group per household.",
                           "confidence": "high", "evidence": ["firestore.rules:1"]},
        },
        "features": [
            {"id": "household-model", "title": "Household and list data model",
             "request": "Add the Firestore data model for households, lists and items with security rules and tests.",
             "depends_on": [], "covers": ["entities", "roles", "data_store"]},
            {"id": "sign-in", "title": "Google sign-in",
             "request": "Add Google sign-in with Firebase Auth and a signed-in route guard with tests.",
             "depends_on": [], "covers": ["auth"]},
            {"id": "list-screen", "title": "Shared list screen",
             "request": "Build the shared list screen where members add, tick and clear items in realtime.",
             "depends_on": ["household-model", "sign-in"], "covers": ["workflows", "surfaces"]},
            {"id": "push", "title": "Push on new item",
             "request": "Send a push notification to the household when a member adds an item.",
             "depends_on": ["list-screen"], "covers": ["notifications"]},
        ],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_repo(self._tmp.name)

    def run_cli(self, *args, ok=True):
        r = subprocess.run([sys.executable, str(ROUTER / "brainstorm.py"), *args], cwd=str(self.root),
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def write(self, name, data):
        d = self.root / ".feature-ladder" / "brainstorm"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(json.dumps(data), encoding="utf-8")
        return str(d / name)

    def sheet_file(self, sheet=None):
        """scan --save, then the scout's sheet beside it."""
        scan = json.loads(self.run_cli("scan", "--save", REQUEST).stdout)
        self.slug = scan["slug"]
        return self.write(f"{self.slug}-sheet.json", sheet or scout_sheet())

    def confirm(self, answers=None, sheet=None):
        path = self.sheet_file(sheet)
        args = ["apply", path] + ([self.write(f"{self.slug}-answers.json", answers)] if answers is not None else [])
        return json.loads(self.run_cli(*args).stdout)


class Scan(Base):
    def test_manifests_and_files_settle_the_architecture_they_name(self):
        dims = brainstorm.scan(REQUEST, str(self.root))["dimensions"]
        self.assertEqual(set(dims), {d[0] for d in brainstorm.DIMENSIONS})
        groups = [d[1] for d in brainstorm.DIMENSIONS]      # the README names these counts
        self.assertEqual((groups.count("application"), groups.count("architecture")), (12, 18))
        for dim, word in (("frontend", "React"), ("frontend", "Vite"), ("data_store", "Firestore"),
                          ("hosting", "Firebase Hosting"), ("hosting", "dev = grocer-dev"),
                          ("ci_cd", "GitHub Actions"), ("observability", "Sentry"), ("stack", "TypeScript"),
                          ("testing", "Vitest"), ("testing", "npm test"), ("platform", "web app"),
                          ("structure", "src")):
            self.assertIn(word, dims[dim]["value"], dim)
            self.assertEqual((dims[dim]["status"], dims[dim]["confidence"]), ("detected", "high"), dim)
            self.assertTrue(dims[dim]["evidence"], dim)

    def test_what_no_manifest_settles_stays_open_with_a_pointer(self):
        dims = brainstorm.scan(REQUEST, str(self.root))["dimensions"]
        for dim in ("users", "workflows", "entities", "non_goals", "success", "performance"):
            self.assertEqual((dims[dim]["status"], dims[dim]["value"]), ("open", None), dim)
        self.assertTrue(dims["purpose"]["evidence"][0].startswith("read: README.md"))

    def test_secrets_are_names_from_code_or_templates_never_values_or_docs(self):
        d = brainstorm.scan(REQUEST, str(self.root))["dimensions"]["config_secrets"]
        self.assertIn("VITE_FIREBASE_API_KEY", d["value"])
        self.assertIn("SENTRY_DSN", d["value"])
        self.assertNotIn("APP_NAME", d["value"])            # not a secret-shaped key
        self.assertNotIn("STRIPE_SECRET_KEY", d["value"])   # only a document mentions it
        self.assertNotIn("grocer", d["value"])

    def test_an_empty_folder_is_greenfield_and_everything_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = brainstorm.scan(REQUEST, tmp)
        self.assertTrue(s["greenfield"])
        self.assertEqual({d["status"] for d in s["dimensions"].values()}, {"open"})


class Decide(Base):
    def decide(self, mode="default", sheet=None):
        return json.loads(self.run_cli("decide", self.sheet_file(sheet), "--mode", mode).stdout)

    def test_every_uncertain_detail_is_asked_application_first(self):
        dec = self.decide()
        asked = [q["detail"] for q in dec["questions"]]
        # Not only what would change the model: every applicable detail that is not high.
        for dim in ("users", "workflows", "entities", "roles", "notifications", "auth"):
            self.assertIn(dim, asked)
        groups = [q["group"] for q in dec["questions"]]
        self.assertEqual(groups, sorted(groups))             # application before architecture
        self.assertLess(asked.index("users"), asked.index("entities"))  # catalog order inside a group
        for q in dec["questions"]:
            self.assertTrue(2 <= len(q["options"]) <= 4, q)
            self.assertTrue(q["options"][0]["label"].endswith("(Recommended)"), q)
            self.assertLessEqual(len(q["header"]), 12)

    def test_what_the_repo_or_the_request_settles_is_not_asked(self):
        dec = self.decide()
        asked = {q["detail"] for q in dec["questions"]} | {q["detail"] for q in dec["open_text"]}
        settled = {a["detail"]: a for a in dec["assumed"]}
        for dim in ("purpose", "data_store", "frontend", "hosting", "ci_cd", "stack"):
            self.assertNotIn(dim, asked)
            self.assertIn(dim, settled)
        self.assertEqual(settled["data_store"]["status"], "proposed")   # the scout corrected the detected value
        self.assertEqual(settled["frontend"]["status"], "detected")     # left out by the scout: the scan fills it in
        self.assertEqual([n["detail"] for n in dec["not_applicable"]], ["content"])
        self.assertNotIn("content", asked)

    def test_a_proposal_without_options_and_a_blank_are_still_asked(self):
        dec = self.decide()
        roles = next(q for q in dec["questions"] if q["detail"] == "roles")
        self.assertEqual([o["label"] for o in roles["options"]][1], "Not needed")
        self.assertIn("edge_cases", [q["detail"] for q in dec["open_text"]])   # nothing proposed: free text
        self.assertIn("success", [q["detail"] for q in dec["open_text"]])      # the scout left it out entirely

    def test_session_questions_and_the_build_choice_come_in_the_same_round(self):
        dec = self.decide()
        self.assertEqual([d["id"] for d in dec["decisions"]], ["orchestrator", "session_agents", "build"])
        build = dec["decisions"][-1]
        self.assertEqual([o["label"] for o in build["options"]],
                         ["Every feature, in order (Recommended)", "The first feature only", "Plan only"])
        n = len(dec["decisions"]) + len(dec["questions"])
        self.assertEqual(sum(dec["ask_batches"]), n)
        self.assertTrue(all(b <= 4 for b in dec["ask_batches"]))
        self.assertEqual(dec["counts"]["features_drafted"], 4)

    def test_auto_asks_nothing(self):
        dec = self.decide("auto")
        self.assertEqual((dec["decisions"], dec["questions"], dec["open_text"], dec["ask_batches"]), ([], [], [], []))

    def test_a_sheet_saved_under_another_name_still_gets_the_scan(self):
        self.sheet_file()
        other = self.write("my-sheet.json", scout_sheet())
        dec = json.loads(self.run_cli("decide", other).stdout)
        self.assertIn("frontend", {a["detail"] for a in dec["assumed"]})   # detected by the scan, not re-asked

    def test_the_scout_can_add_a_detail_the_catalog_lacks(self):
        sheet = scout_sheet()
        sheet["dimensions"]["barcode_scan"] = {"group": "application", "header": "Barcodes", "question": "Scan barcodes?",
                                               "value": "No barcode scanning in v1.", "confidence": "low"}
        q = next(q for q in self.decide(sheet=sheet)["questions"] if q["detail"] == "barcode_scan")
        self.assertEqual((q["group"], q["header"]), ("application", "Barcodes"))


ANSWERS = {
    "answers": {
        "users": "Households, public sign-up (Recommended)",   # confirms the proposal
        "entities": "Many lists per household",                 # changes it
        "roles": "you decide",                                  # defers to the scout
        "notifications": "Not needed",
        "edge_cases": "Offline edits must sync when the phone reconnects",   # typed
    },
    "decisions": {"orchestrator": "Opus 5.5 xhigh", "session_agents": "Medium (3-4 subagents)",
                  "build": "Every feature, in order (Recommended)"},
}


class Apply(Base):
    def test_answers_become_the_confirmed_sheet(self):
        app = self.confirm(ANSWERS)
        d = app["dimensions"]
        self.assertEqual((d["users"]["status"], d["users"]["source"]), ("decided", "user (confirmed)"))
        self.assertIn("handful per list", d["users"]["value"])           # the scout's fuller wording is kept
        self.assertEqual((d["entities"]["value"], d["entities"]["source"]), ("Many lists per household", "user"))
        self.assertEqual(d["entities"]["detail"], "households/{id}/lists/{id}/items")
        self.assertTrue(d["entities"]["changed"])
        self.assertEqual((d["roles"]["status"], d["roles"]["source"]), ("assumed", "scout:low"))
        self.assertEqual((d["notifications"]["status"], d["notifications"]["source"]), ("not_applicable", "user"))
        self.assertEqual((d["content"]["status"], d["content"]["source"]), ("not_applicable", "scout"))
        self.assertEqual((d["edge_cases"]["status"], d["edge_cases"]["source"]), ("decided", "user"))
        self.assertEqual((d["frontend"]["status"], d["frontend"]["source"]), ("detected", "repo"))
        self.assertEqual((d["success"]["status"], d["success"]["source"]), ("open", "none"))
        self.assertEqual(app["build"], "all")
        self.assertEqual(sum(app["counts"].values()), len(d))

    def test_the_readable_sheet_is_written_and_git_ignored(self):
        app = self.confirm(ANSWERS)
        md = Path(app["readable"]).read_text(encoding="utf-8")
        self.assertEqual(md.splitlines()[0], f"<!-- request: {REQUEST} -->")
        for part in ("## Application", "## Architecture", "## Not needed", "## Still open",
                     "## Features, in build order", "Many lists per household", "REVISE: you changed entities"):
            self.assertIn(part, md)
        self.assertIn("brainstorm/", (self.root / ".feature-ladder" / ".gitignore").read_text(encoding="utf-8"))
        self.assertEqual(self.run_cli("show").stdout, md)

    def test_session_answers_are_stored_so_no_feature_intake_asks_them_again(self):
        self.confirm(ANSWERS)
        status = session_budget.status(self.root)
        self.assertEqual((status["budget"], status["max"], status["orchestrator"]["key"]), ("M", 4, "opus"))
        self.assertTrue(status["orchestrator"]["chosen"])
        prop = intake.scan("add the household data model", str(self.root))
        self.assertEqual([d["id"] for d in intake.decide(prop)["decisions"]], [])
        self.assertEqual(brainstorm.decide(brainstorm.normalise(scout_sheet(), brainstorm.scan(REQUEST, str(self.root))))
                         ["decisions"][0]["id"], "build")

    def test_no_answers_file_means_the_scouts_values_stand(self):
        app = self.confirm()
        self.assertNotIn("decided", app["counts"])
        self.assertEqual(app["build"], "all")
        self.assertEqual(session_budget.status(self.root)["budget"], "unset")   # nothing stored for the session

    def test_an_answer_for_an_unknown_detail_is_refused(self):
        path = self.sheet_file()
        r = self.run_cli("apply", path, self.write("a.json", {"answers": {"colour": "blue"}}), ok=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("colour", r.stderr)

    def test_build_choice_maps_to_all_first_or_plan(self):
        for label, mode in (("Every feature, in order (Recommended)", "all"), ("The first feature only", "first"),
                            ("Plan only", "plan"), ("first", "first"), ("", "all"), (None, "all")):
            self.assertEqual(brainstorm.build_mode(label), mode, label)


class Features(Base):
    def test_queue_is_in_build_order_with_changed_and_unneeded_features_marked(self):
        feats = {f["id"]: f for f in self.confirm(ANSWERS)["features"]}
        self.assertEqual(list(feats), ["household-model", "sign-in", "list-screen", "push"])
        self.assertEqual((feats["household-model"]["revise"], feats["household-model"]["changed"]), (True, ["entities"]))
        self.assertNotIn("revise", feats["sign-in"])
        self.assertEqual(feats["push"]["status"], "dropped")   # its only detail was answered Not needed
        self.assertEqual({f["status"] for k, f in feats.items() if k != "push"}, {"queued"})

    def test_dependencies_come_first_whatever_order_was_drafted(self):
        sheet = scout_sheet()
        sheet["features"].reverse()
        order = [f["id"] for f in self.confirm(sheet=sheet)["features"]]
        self.assertLess(order.index("household-model"), order.index("list-screen"))
        self.assertLess(order.index("sign-in"), order.index("list-screen"))
        self.assertLess(order.index("list-screen"), order.index("push"))

    def test_settled_details_in_no_feature_are_named(self):
        app = self.confirm(ANSWERS)
        note = next(n for n in app["notes"] if n.startswith("settled but in no feature"))
        self.assertIn("edge_cases", note)
        self.assertNotIn("purpose", note)     # framing, not something to build
        self.assertNotIn("frontend", note)    # already in the repo

    def test_an_invalid_list_is_refused_and_the_queue_is_kept(self):
        app = self.confirm(ANSWERS)
        before = Path(app["written_to"]).read_text(encoding="utf-8")
        bad = [
            [{"id": "a", "title": "A", "request": "too short"}],
            [{"id": "a", "title": "A", "request": "build the a part with tests", "covers": ["colour"]}],
            [{"id": "a", "title": "A", "request": "build the a part with tests", "depends_on": ["zzz"]}],
            [{"id": "a", "title": "A", "request": "build the a part with tests", "depends_on": ["b"]},
             {"id": "b", "title": "B", "request": "build the b part with tests", "depends_on": ["a"]}],
            [{"id": "a", "title": "A", "request": "build the a part with tests"},
             {"id": "a", "title": "A again", "request": "build the a part with tests"}],
            [],
        ]
        for features in bad:
            r = self.run_cli("features", app["written_to"], self.write("f.json", {"features": features}), ok=False)
            self.assertEqual(r.returncode, 1, features)
            self.assertTrue(json.loads(r.stdout)["errors"], features)
        self.assertEqual(Path(app["written_to"]).read_text(encoding="utf-8"), before)

    def test_a_revised_list_replaces_the_queue_and_clears_the_marks(self):
        app = self.confirm(ANSWERS)
        revised = [dict(f, request=f["request"] + " Many lists per household.")
                   for f in app["features"] if f["status"] != "dropped"]
        out = json.loads(self.run_cli("features", app["written_to"], self.write("f.json", {"features": revised})).stdout)
        self.assertEqual([f["id"] for f in out["features"]], ["household-model", "sign-in", "list-screen"])
        self.assertTrue(all("revise" not in f for f in out["features"]))
        self.assertNotIn("REVISE", Path(out["readable"]).read_text(encoding="utf-8"))


class Queue(Base):
    def next(self):
        return json.loads(self.run_cli("next").stdout)

    def done(self, fid, status="done"):
        return json.loads(self.run_cli("done", fid, "--status", status).stdout)

    def test_features_are_handed_out_one_at_a_time_in_order(self):
        self.confirm(ANSWERS)
        n = self.next()
        self.assertEqual((n["build"], n["feature"]["id"], n["feature"]["n"], n["feature"]["of"]),
                         ("all", "household-model", 1, 3))
        self.assertEqual(n["feature"]["request"].split()[0], "Add")
        self.assertEqual(n["remaining"]["blocked"], [])                     # waiting its turn is not blocked
        self.assertEqual(self.next()["feature"]["id"], "household-model")   # still running: handed out again, not skipped
        self.done("household-model")
        self.assertEqual(self.next()["feature"]["id"], "sign-in")
        self.done("sign-in")
        self.assertEqual(self.next()["feature"]["id"], "list-screen")
        left = self.done("list-screen")["remaining"]
        self.assertEqual((left["done"], left["queued"], left["dropped"]), (3, 0, 1))
        end = self.next()
        self.assertIsNone(end["feature"])
        self.assertEqual(end["note"], "every feature is finished")

    def test_a_feature_that_did_not_finish_blocks_what_depends_on_it(self):
        self.confirm(ANSWERS)
        self.next()
        self.done("household-model", "partial")
        self.assertEqual(self.next()["feature"]["id"], "sign-in")
        self.done("sign-in")
        end = self.next()
        self.assertIsNone(end["feature"])
        self.assertEqual(end["remaining"]["blocked"], ["list-screen"])
        self.assertIn("blocked", end["note"])

    def test_plan_only_builds_nothing_until_the_user_comes_back(self):
        self.confirm(dict(ANSWERS, decisions=dict(ANSWERS["decisions"], build="Plan only")))
        first = self.next()
        self.assertIsNone(first["feature"])
        self.assertIn("plan only", first["note"])
        self.assertEqual(self.run_cli("find", "some other request").stdout, "")
        found = self.run_cli("find", "  build a shared   grocery list app for households ").stdout.strip()
        self.assertTrue(found.endswith(".json"))
        again = self.next()
        self.assertEqual((again["build"], again["feature"]["id"]), ("first", "household-model"))

    def test_nothing_is_handed_out_once_the_session_budget_is_spent(self):
        self.confirm(dict(ANSWERS, decisions=dict(ANSWERS["decisions"], session_agents="Small (1-2 subagents)")))
        for _ in range(2):
            self.assertTrue(session_budget.check(self.root, "implementer", "sonnet")[0])
        n = self.next()
        self.assertIsNone(n["feature"])
        self.assertEqual(n["session_left"], 0)
        self.assertEqual(n["remaining"]["queued"], 3)


class Context(Base):
    def test_a_card_carries_only_what_its_feature_covers(self):
        app = self.confirm(ANSWERS)
        text = brainstorm.context(app, "sign-in")
        self.assertTrue(text.startswith("## App context (settled in the brainstorm; binding)"))
        self.assertIn("App: A household keeps one shared grocery list", text)
        self.assertIn("Feature 2 of 3: Google sign-in. Builds on: nothing.", text)
        self.assertIn("Sign-in: Firebase Auth with Google sign-in.", text)
        for always in ("Stack: ", "Layout: ", "Testing: "):
            self.assertIn(always, text)
        self.assertNotIn("Many lists per household", text)        # another feature's detail
        self.assertNotIn("Push on new item", text)                # dropped
        self.assertIn("Other features, not yours: Household and list data model; Shared list screen", text)
        model = brainstorm.context(app, "household-model")
        self.assertIn("Data model: Many lists per household (households/{id}/lists/{id}/items)", model)
        self.assertLessEqual(len(text), brainstorm.MAX_CONTEXT)

    def test_long_details_are_capped(self):
        sheet = scout_sheet()
        sheet["dimensions"]["auth"]["value"] = "Firebase Auth. " + "very long detail " * 400
        sheet["dimensions"]["auth"]["confidence"] = "high"
        text = brainstorm.context(self.confirm(sheet=sheet), "sign-in")
        self.assertLessEqual(len(text), brainstorm.MAX_CONTEXT)
        self.assertLessEqual(max(len(line) for line in text.splitlines()), brainstorm.MAX_VALUE + 40)

    def test_the_feature_intake_carries_the_context(self):
        self.confirm(ANSWERS)
        pick = json.loads(self.run_cli("next").stdout)["feature"]
        self.assertEqual(set(pick["brainstorm"]), {"sheet", "feature", "context"})
        prop = intake.scan(pick["request"], str(self.root))
        final = intake.apply(prop, {"brainstorm": pick["brainstorm"]})
        self.assertEqual(final["brainstorm"]["feature"], "household-model")
        self.assertIn("## App context", final["brainstorm"]["context"])
        self.assertIsNone(intake.apply(prop, {})["brainstorm"])   # without the switch: nothing
        cli = json.loads(self.run_cli("context", "--feature", "household-model", "--json").stdout)
        self.assertEqual(cli["context"], pick["brainstorm"]["context"])


class Wiring(unittest.TestCase):
    def read(self, *parts):
        return (REPO.joinpath(*parts)).read_text(encoding="utf-8")

    def test_guide_names_this_script_and_every_step(self):
        r = subprocess.run([sys.executable, str(ROUTER / "brainstorm.py"), "guide"], capture_output=True, text=True,
                           encoding="utf-8", timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(str((ROUTER / "brainstorm.py").resolve()), r.stdout)
        for word in ("scan --save", "decide", "-sheet.json", '"features"', "REVISE", "Retained: none"):
            self.assertIn(word, r.stdout)
        self.assertNotIn("{{", r.stdout)

    def test_the_switch_is_documented_on_both_surfaces(self):
        feature = self.read("plugins", "feature-ladder", "commands", "feature.md")
        self.assertIn("| `--brainstorm` |", feature)
        self.assertIn("[--brainstorm]", feature.split("---")[1])
        self.assertIn("## 0b. Brainstorm (only with `--brainstorm`)", feature)
        self.assertIn("Mode: brainstorm", self.read("plugins", "feature-ladder", "agents", "scoper.md"))
        app = self.read("claude-app", "model-ladder-router", "SKILL.md")
        self.assertIn("complexity-router/scripts/brainstorm.py", app.split("for f in ")[1].split("; do")[0])
        self.assertIn("`B guide`", app)

    def test_the_brainstorm_adds_no_agent_beyond_the_scout(self):
        feature = self.read("plugins", "feature-ladder", "commands", "feature.md")
        section = feature.split("## 0b. Brainstorm")[1].split("\n## 1. ")[0]
        agents = set(re.findall(r'subagent_type="feature-ladder:([a-z-]+)"', section))
        self.assertEqual(agents, {"scoper"})
        self.assertNotIn("opus", section.lower())
        self.assertNotIn("fable", section.lower())
        source = (ROUTER / "brainstorm.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("fable", source)
        self.assertNotIn("opus", source)


if __name__ == "__main__":
    unittest.main()
