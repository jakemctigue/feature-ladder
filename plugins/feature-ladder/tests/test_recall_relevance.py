"""Hindsight in every mode, and only relevant facts: recall.py, confidence.select_relevant,
the key lookup, the project tags, the unattended flush, and the foreign-server rule.

No test here touches the network: the MCP client is replaced by a fake.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
HM = PLUGIN / "skills" / "hindsight-memory" / "scripts"
OL = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(HM))
sys.path.insert(0, str(OL))

import confidence as conf  # noqa: E402
import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402
import memory_gate  # noqa: E402
import recall  # noqa: E402
import resolve_bank  # noqa: E402


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=str(cwd), check=True, capture_output=True)


def mems(*lines):
    return conf.parse_memories("\n".join(lines))


class FakeMcp:
    def __init__(self, result=None, banks=()):
        self.result, self.banks, self.calls = result or {"results": []}, list(banks), []

    def start(self):
        return self

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_banks":
            return {"banks": self.banks}
        if tool == "recall":
            return self.result
        return {}


class Repo(unittest.TestCase):
    folder = "widgets-src"           # a clone whose folder name is not the repo's name
    remote = "https://github.com/acme/widgets.git"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / self.folder
        (self.root / "server").mkdir(parents=True)
        (self.root / "server" / "mapRoutes.ts").write_text("x\n", encoding="utf-8")
        git(self.root, "init", "-q")
        if self.remote:
            git(self.root, "remote", "add", "origin", self.remote)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.mcp = FakeMcp()
        for p in (mock.patch.object(resolve_bank, "CACHE", self.home / "banks.json"),
                  mock.patch.object(resolve_bank, "Mcp", lambda *a, **k: self.mcp),
                  mock.patch.object(Path, "home", lambda: self.home),
                  mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        for k in ("HINDSIGHT_API_KEY", "HINDSIGHT_MCP_URL", "HINDSIGHT_BANK_ID", "FEATURE_LADDER_CHILD", "CLAUDECODE"):
            os.environ.pop(k, None)
        ladder_state._ROOTS.clear()

    def bank_exists(self):
        self.mcp.banks.append({"bank_id": "coding-agent::acme-widgets", "name": "acme/widgets"})

    def key(self):
        os.environ["HINDSIGHT_API_KEY"] = "hsk_test"

    def recall(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = recall.main([*args, "--cwd", str(self.root)])
        return code, json.loads(out.getvalue())


# ------------------------------------------------------------- relevance --
class SelectRelevant(unittest.TestCase):
    def test_one_shared_word_is_not_enough_but_a_file_or_two_words_are(self):
        lines = ["[pitfall] the deploy step of the docs site times out on Fridays",       # one word: deploy
                 "[pitfall] server/mapRoutes.ts must await the batch commit",              # file
                 "[convention] map owner checks run before resolveMapOwner in every route"]  # owner + checks
        kept, dropped = conf.select_relevant(mems(*lines), "deploy owner checks for maps", ["server/mapRoutes.ts"])
        texts = [m["text"] for m in kept]
        self.assertEqual(len(kept), 2, texts)
        self.assertTrue(texts[0].startswith("server/mapRoutes.ts"))  # file hits rank first
        self.assertEqual(dropped["off_topic"], 1)

    def test_duplicates_and_the_cap(self):
        line = "[pitfall] server/mapRoutes.ts must await the batch commit"
        many = [line, line] + [f"[pitfall] server/mapRoutes.ts edge case number {i} fails" for i in range(8)]
        kept, dropped = conf.select_relevant(mems(*many), "x", ["server/mapRoutes.ts"], cap=5)
        self.assertEqual(len(kept), 5)
        self.assertEqual(dropped["duplicate"], 1)
        self.assertEqual(dropped["over_cap"], 4)

    def test_newer_memory_wins_a_tie(self):
        raw = {"results": [{"text": "[decision] server/mapRoutes.ts uses the v1 owner check", "mentioned_at": "2025-01-01T00:00:00Z"},
                           {"text": "[decision] server/mapRoutes.ts uses the v2 owner check", "mentioned_at": "2026-06-01T00:00:00Z"}]}
        kept, _ = conf.select_relevant(conf.parse_memories(raw), "x", ["server/mapRoutes.ts"])
        self.assertIn("v2", kept[0]["text"])

    def test_a_memory_about_a_deleted_path_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "live.py").write_text("x", encoding="utf-8")
            git(root, "init", "-q")
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "i")
            lines = ["[pitfall] src/gone.py drops the upload retry; the uploader breaks",
                     "[pitfall] src/live.py drops the upload retry; the uploader breaks",
                     "[pitfall] settings.local.json overrides the upload retry for the uploader"]  # bare name: never stale
            kept, dropped = conf.select_relevant(mems(*lines), "upload retry uploader", [], root=root)
            texts = " ".join(m["text"] for m in kept)
            self.assertNotIn("gone.py", texts)
            self.assertIn("live.py", texts)
            self.assertIn("settings.local.json", texts)
            self.assertEqual(dropped["stale"], 1)

    def test_unrelated_memories_do_not_move_the_confidence(self):
        far = ["[pitfall] the Firestore composite index build fails on deploy and times out"]
        with_far = conf.decide("card", "rename the settings page title", ["src/title.ts"], "M", memories=mems(*far))
        without = conf.decide("card", "rename the settings page title", ["src/title.ts"], "M", memories=[])
        self.assertEqual(with_far["confidence"], without["confidence"])
        self.assertIn("none about this card", with_far["surface"]["texture"])


# ------------------------------------------------------------- recall.py --
class RecallScript(Repo):
    def test_from_a_saved_recall_only_relevant_facts_reach_the_memory_line(self):
        f = self.root / ".feature-ladder" / "recall.json"
        f.parent.mkdir()
        f.write_text(json.dumps({"results": [
            {"text": "[pitfall] server/mapRoutes.ts must await the batch commit", "tags": ["project:widgets"]},
            {"text": "[convention] the docs site deploys from the docs branch", "tags": ["project:widgets"]}]}),
            encoding="utf-8")
        code, out = self.recall("--query", "save the map name", "--paths", "server/mapRoutes.ts", "--from", str(f))
        self.assertEqual(code, 0)
        self.assertEqual(out["memory"], "Memory: [pitfall] server/mapRoutes.ts must await the batch commit")
        self.assertEqual((out["recalled"], out["kept"], out["dropped"]["off_topic"]), (2, 1, 1))
        self.assertEqual(self.mcp.calls, [])  # --from never touches the network

    def test_nothing_relevant_is_a_real_answer(self):
        f = self.root / "r.json"
        f.write_text(json.dumps({"results": [{"text": "[convention] the docs site deploys from the docs branch"}]}),
                     encoding="utf-8")
        _, out = self.recall("--query", "save the map name", "--paths", "server/mapRoutes.ts", "--from", str(f))
        self.assertTrue(out["memory"].startswith("Memory: none (1 recalled, none about this work"), out["memory"])

    def test_live_recall_uses_both_project_tags_strictly_and_saves_the_raw_result(self):
        self.key()
        self.bank_exists()
        self.mcp.result = {"results": [{"text": "[pitfall] server/mapRoutes.ts must await the batch commit"}]}
        out_file = self.root / ".feature-ladder" / "intake" / "f-recall.json"
        _, out = self.recall("--query", "save the map name", "--paths", "server/mapRoutes.ts", "--out", str(out_file))
        args = [a for t, a in self.mcp.calls if t == "recall"][0]
        self.assertEqual(args["tags"], ["project:widgets", "project:widgets-src"])
        self.assertEqual(args["tags_match"], "any_strict")
        self.assertEqual(args["bank_id"], "coding-agent::acme-widgets")
        self.assertEqual(args["max_tokens"], 1000)
        self.assertEqual(out["source"], "live")
        self.assertTrue(out_file.is_file())
        self.assertIn("mapRoutes.ts", out["memory"])

    def test_the_coordinator_reuses_the_scouts_recall_without_a_network_call(self):
        saved = self.root / ".feature-ladder" / "intake" / "f-recall.json"
        saved.parent.mkdir(parents=True)
        saved.write_text(json.dumps({"results": [{"text": "[pitfall] server/mapRoutes.ts must await the commit"}]}),
                         encoding="utf-8")
        run_out = self.root / ".feature-ladder" / "runs" / "f" / "recall.json"
        _, out = self.recall("--query", "save the map name", "--paths", "server/mapRoutes.ts",
                             "--reuse", str(saved), "--out", str(run_out))
        self.assertEqual(out["source"], "reused")
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(run_out.read_text(encoding="utf-8"), saved.read_text(encoding="utf-8"))

    def test_without_a_key_it_says_so_and_hands_over_the_mcp_arguments(self):
        _, out = self.recall("--query", "save the map name", "--paths", "server/mapRoutes.ts")
        self.assertIn("Hindsight is off", out["memory"])
        self.assertEqual(out["mcp_fallback"]["tags_match"], "any_strict")
        self.assertEqual(self.mcp.calls, [])
        os.environ["CLAUDECODE"] = "1"  # in Claude Code the plugin's MCP server has no key either
        _, out = self.recall("--query", "save the map name")
        self.assertNotIn("mcp_fallback", out)

    def test_before_the_first_write_there_is_nothing_to_recall(self):
        self.key()
        _, out = self.recall("--query", "save the map name")
        self.assertIn("no memories yet", out["memory"])
        self.assertNotIn("recall", [t for t, _ in self.mcp.calls])


# ------------------------------------------------------- key and project --
class Settings(Repo):
    def test_the_key_is_found_in_the_users_feature_ladder_env_file(self):
        d = self.home / ".claude" / "feature-ladder"
        d.mkdir(parents=True)
        (d / ".env").write_text('OTHER=1\nexport HINDSIGHT_API_KEY="hsk_file"\n', encoding="utf-8")
        self.assertEqual(resolve_bank.setting("HINDSIGHT_API_KEY", str(self.root)), ("hsk_file", str(d / ".env")))
        self.assertTrue(resolve_bank.key_status(str(self.root))[0])

    def test_the_repos_env_file_counts_too(self):
        (self.root / ".env").write_text("HINDSIGHT_API_KEY=hsk_repo\n", encoding="utf-8")
        self.assertEqual(resolve_bank.setting("HINDSIGHT_API_KEY", str(self.root))[0], "hsk_repo")

    def test_session_start_says_loudly_when_memory_is_off(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": str(self.root)}))), redirect_stdout(out):
            resolve_bank.session_start()
        self.assertIn("Hindsight memory is OFF", out.getvalue())
        self.assertEqual(self.mcp.calls, [])

    def test_projects_put_the_repo_name_first_then_the_folder(self):
        self.assertEqual(resolve_bank.projects(str(self.root)), ["widgets", "widgets-src"])

    def test_a_cached_no_bank_yet_is_rechecked_at_session_start(self):
        self.key()
        self.assertEqual(resolve_bank.resolve(str(self.root))["how"], "planned")
        self.bank_exists()                                  # another clone wrote the first memory
        self.assertEqual(resolve_bank.resolve(str(self.root))["how"], "planned")   # cached
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": str(self.root)}))), redirect_stdout(out):
            resolve_bank.session_start()
        self.assertIn("coding-agent::acme-widgets", out.getvalue())
        self.assertNotIn("no Hindsight memory yet", out.getvalue())
        self.assertEqual(resolve_bank.resolve(str(self.root))["how"], "existing")


class NoRemote(Repo):
    remote = None
    folder = "scratch"

    def test_without_a_remote_the_folder_is_the_only_project(self):
        self.assertEqual(resolve_bank.projects(str(self.root)), ["scratch"])


# ------------------------------------------------------ foreign servers --
class ForeignServer(Repo):
    def pre(self, tool, tin):
        out = io.StringIO()
        payload = {"tool_name": tool, "cwd": str(self.root), "tool_input": tin}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
            resolve_bank.pretooluse()
        return json.loads(out.getvalue())["hookSpecificOutput"] if out.getvalue().strip() else {}

    def start_run(self):
        fl = self.root / ".feature-ladder"
        fl.mkdir(exist_ok=True)
        (fl / "run.json").write_text(json.dumps({"active": True, "slug": "f", "started": "t"}), encoding="utf-8")

    def test_left_alone_outside_a_run(self):
        self.assertEqual(self.pre("mcp__hindsight__reflect", {"query": "x"}), {})
        self.assertEqual(self.pre("mcp__hindsight__retain", {"content": "anything"}), {})

    def test_during_a_run_a_write_is_denied(self):
        self.start_run()
        self.assertEqual(self.pre("mcp__hindsight__retain", {"content": "x"})["permissionDecision"], "deny")
        self.assertEqual(self.pre("mcp__hindsight__recall", {"query": "x"}), {})


# ------------------------------------------------------- unattended flush --
class AutoFlush(Repo):
    GOOD = "[pitfall] server/mapRoutes.ts must await the Firestore batch commit before it returns"

    def queue(self):
        q = self.root / ".feature-ladder" / "session-memory.md"
        q.parent.mkdir(exist_ok=True)
        q.write_text(f"- {self.GOOD}\n", encoding="utf-8")

    def test_skips_with_nothing_queued_without_a_key_and_from_a_child(self):
        self.assertEqual(memory_gate.auto_skip(self.root), "nothing queued")
        self.queue()
        self.assertIn("HINDSIGHT_API_KEY", memory_gate.auto_skip(self.root))
        self.key()
        os.environ["FEATURE_LADDER_CHILD"] = "1"
        self.assertIn("GLM worker", memory_gate.auto_skip(self.root))
        del os.environ["FEATURE_LADDER_CHILD"]
        self.assertIsNone(memory_gate.auto_skip(self.root))

    def test_never_during_a_run(self):
        self.queue()
        self.key()
        (self.root / ".feature-ladder" / "run.json").write_text(json.dumps({"active": True, "slug": "f"}), encoding="utf-8")
        self.assertIn("feature run is active", memory_gate.auto_skip(self.root))

    def test_writes_the_queue_with_the_repo_tag_and_backs_off_after_a_failure(self):
        self.queue()
        self.key()
        out = memory_gate.flush(self.root, auto=True)
        self.assertEqual(len(out["written"]), 1)
        retain = [a for t, a in self.mcp.calls if t == "retain"][0]
        self.assertEqual(retain["tags"][0], "project:widgets")
        self.assertEqual(memory_gate.flush(self.root, auto=True)["written"], [])  # already in the ledger

        q = self.root / ".feature-ladder" / "session-memory.md"
        q.write_text("- [pitfall] server/mapRoutes.ts drops the owner when the batch commit is retried\n", encoding="utf-8")
        with mock.patch.object(resolve_bank, "Mcp", side_effect=OSError("down")):
            self.assertIn("error", memory_gate.flush(self.root, auto=True))
        self.assertIn("10 minutes", memory_gate.auto_skip(self.root))

    def test_background_returns_at_once_and_never_fails_the_hook(self):
        t0 = time.time()
        r = subprocess.run([sys.executable, str(HM / "memory_gate.py"), "flush", "--background"],
                           input=json.dumps({"cwd": str(self.root)}), capture_output=True, text=True, timeout=60,
                           env={**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home)})
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        self.assertLess(time.time() - t0, 30)


# -------------------------------------------------------- every surface --
class EveryMode(unittest.TestCase):
    def read(self, *parts):
        return REPO.joinpath(*parts).read_text(encoding="utf-8")

    def test_hooks_write_memory_when_a_session_ends_and_gate_any_hindsight_server(self):
        hooks = json.loads(self.read("plugins", "feature-ladder", "hooks", "hooks.json"))["hooks"]
        end = json.dumps(hooks["SessionEnd"])
        self.assertIn("memory_gate.py\\\" flush --background", end)
        self.assertIn("flush --background", json.dumps(hooks["SessionStart"]))
        matchers = [e.get("matcher") for e in hooks["PreToolUse"]]
        self.assertIn("mcp__.*[Hh]indsight.*", matchers)

    def test_every_mode_reads_memory_through_recall_py(self):
        for parts in (("plugins", "feature-ladder", "agents", "scoper.md"),
                      ("plugins", "feature-ladder", "agents", "coordinator.md"),
                      ("plugins", "feature-ladder", "commands", "research.md"),
                      ("plugins", "feature-ladder", "commands", "recall.md"),
                      ("plugins", "feature-ladder", "commands", "feature.md"),
                      ("claude-app", "model-ladder-router", "SKILL.md")):
            with self.subTest(parts[-1]):
                self.assertIn("recall.py", self.read(*parts))
        guide = subprocess.run([sys.executable, str(PLUGIN / "skills" / "complexity-router" / "scripts" / "brainstorm.py"),
                                "guide"], capture_output=True, text=True, encoding="utf-8", timeout=60).stdout
        self.assertIn(str((HM / "recall.py").resolve()), guide)
        self.assertTrue((HM / "recall.py").resolve().is_file())

    def test_the_app_fetches_recall_py(self):
        app = self.read("claude-app", "model-ladder-router", "SKILL.md")
        self.assertIn("hindsight-memory/scripts/recall.py", app.split("for f in ")[1].split("; do")[0])

    def test_glm_children_get_no_hindsight_tools(self):
        self.assertNotIn("hindsight", ew.TOOLS)
        self.assertIn("hindsight__retain", ew.DISALLOWED)


if __name__ == "__main__":
    unittest.main()
