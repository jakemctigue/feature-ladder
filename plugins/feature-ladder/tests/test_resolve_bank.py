"""resolve_bank.py: which Hindsight bank a repository uses, without creating banks nobody writes to.

No test here touches the network: the MCP client is replaced by a fake.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "hindsight-memory" / "scripts"))
sys.path.insert(0, str(PLUGIN / "skills" / "orchestration-ladder" / "scripts"))

import ladder_state  # noqa: E402
import memory_gate  # noqa: E402
import resolve_bank  # noqa: E402

GOOD = "[pitfall] Firestore batch writes over 500 ops fail in src/maps_workflow.py; split them with chunked()"


class FakeMcp:
    """Stands in for the Hindsight MCP endpoint and records every call."""
    def __init__(self, banks=(), fail=()):
        self.banks, self.calls, self.fail = list(banks), [], set(fail)

    def start(self):
        return self

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.fail:
            raise RuntimeError(f"{tool}: refused")
        if tool == "list_banks":
            q = str(args.get("query") or "").lower()
            return {"banks": [b for b in self.banks if q in b["bank_id"].lower() or q in (b.get("name") or "").lower()]}
        if tool == "create_bank":
            self.banks.append({"bank_id": args["bank_id"], "name": args.get("name")})
        return {}

    def tools(self):
        return [t for t, _ in self.calls]


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=str(cwd), check=True, capture_output=True)


class RepoCase(unittest.TestCase):
    remote = "https://github.com/acme/widgets.git"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "widgets"
        self.root.mkdir()
        git(self.root, "init", "-q")
        if self.remote:
            git(self.root, "remote", "add", "origin", self.remote)
        self.cache = Path(self.tmp.name) / "banks.json"
        self.mcp = FakeMcp()
        for p in (mock.patch.object(resolve_bank, "CACHE", self.cache),
                  mock.patch.object(resolve_bank, "Mcp", lambda: self.mcp)):
            p.start()
            self.addCleanup(p.stop)
        ladder_state._ROOTS.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, mode, payload):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), redirect_stdout(out):
            getattr(resolve_bank, mode)()
        return out.getvalue().strip()

    def pre(self, tool, tool_input):
        out = self.hook("pretooluse", {"tool_name": f"mcp__plugin_feature-ladder_hindsight__{tool}",
                                       "cwd": str(self.root), "tool_input": tool_input})
        return json.loads(out)["hookSpecificOutput"] if out else {}


class Remotes(unittest.TestCase):
    def test_owner_is_the_whole_namespace_so_two_groups_never_share_a_bank(self):
        cases = {"https://github.com/jakemctigue/feature-ladder.git": ("jakemctigue", "feature-ladder"),
                 "git@github.com:jakemctigue/feature-ladder.git": ("jakemctigue", "feature-ladder"),
                 "git@gitlab.com:acme/platform/api.git": ("acme-platform", "api"),
                 "https://gitlab.com/other/platform/api.git": ("other-platform", "api"),
                 "https://dev.azure.com/org/proj/_git/api": ("org-proj", "api"),
                 "ssh://git@example.com:2222/team/repo.git/": ("team", "repo"),
                 "C:/Users/Jake/mirrors/api.git": ("", ""),
                 "/srv/git/api.git": ("", ""),
                 "file:///srv/git/api.git": ("", ""),
                 "": ("", "")}
        for url, want in cases.items():
            self.assertEqual(resolve_bank.parse_remote(url), want, url)

    def test_candidates_are_slugged_and_older_ids_are_still_found(self):
        ident = {"owner": "acme-platform", "repo": "api", "project": "My App (v2)", "legacy_owner": "platform"}
        c = resolve_bank.candidates(ident)
        self.assertEqual(c[0], "coding-agent::acme-platform-api")
        self.assertIn("coding-agent::platform-api", c)      # what versions before this one created
        self.assertIn("coding-agent::my-app-v2", c)
        self.assertIn("coding-agent::My App (v2)", c)       # ditto, unslugged
        self.assertEqual(resolve_bank.candidates({"owner": "", "repo": "", "project": "My App (v2)"})[0],
                         "coding-agent::my-app-v2")

    def test_an_existing_older_bank_wins_over_creating_a_new_one(self):
        ident = {"root": "/nope", "owner": "acme-platform", "repo": "api", "project": "api", "legacy_owner": "platform"}
        bank, action = resolve_bank.choose(ident, [{"bank_id": "coding-agent::platform-api", "name": "platform/api"}])
        self.assertEqual((bank, action), ("coding-agent::platform-api", "existing"))


class LazyCreation(RepoCase):
    def test_opening_a_session_does_not_create_a_bank(self):
        msg = self.hook("session_start", {"cwd": str(self.root)})
        self.assertNotIn("create_bank", self.mcp.tools())
        self.assertIn("coding-agent::acme-widgets", msg)
        self.assertIn("first", msg)  # "...is created on the first memory write"
        r = resolve_bank.resolve(str(self.root))
        self.assertEqual((r["bank_id"], r["how"]), ("coding-agent::acme-widgets", "planned"))

    def test_the_first_write_creates_and_configures_it_once(self):
        resolve_bank.resolve(str(self.root))
        r = resolve_bank.ensure_bank(str(self.root))
        self.assertEqual(r["how"], "created")
        self.assertEqual(self.mcp.tools().count("create_bank"), 1)
        cfg = [a for t, a in self.mcp.calls if t == "update_bank"][0]
        self.assertEqual(cfg["bank_id"], "coding-agent::acme-widgets")
        self.assertEqual(cfg["config_updates"]["retain_extraction_mode"], "verbatim")
        resolve_bank.ensure_bank(str(self.root))
        self.assertEqual(self.mcp.tools().count("create_bank"), 1)

    def test_a_server_that_rejects_the_config_still_gets_a_bank(self):
        self.mcp.fail.add("update_bank")
        r = resolve_bank.ensure_bank(str(self.root))
        self.assertEqual(r["how"], "created")
        self.assertIn("create_bank", self.mcp.tools())

    def test_an_existing_bank_is_used_and_not_reconfigured(self):
        self.mcp.banks.append({"bank_id": "coding-agent::acme-widgets", "name": "acme/widgets"})
        r = resolve_bank.ensure_bank(str(self.root))
        self.assertEqual(r["how"], "existing")
        self.assertEqual([t for t in self.mcp.tools() if t != "list_banks"], [])

    def test_session_start_says_cached_not_created_on_later_sessions(self):
        resolve_bank.ensure_bank(str(self.root))
        msg = self.hook("session_start", {"cwd": str(self.root)})
        self.assertNotIn("(created)", msg)
        self.assertIn("coding-agent::acme-widgets", msg)
        self.assertIn('"tags": ["project:widgets"]', msg)  # the exact recall arguments, so the hook rewrites nothing


class Hook(RepoCase):
    def test_recall_before_any_memory_exists_is_answered_without_a_call(self):
        out = self.pre("recall", {"query": "retries"})
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("no memories", out["permissionDecisionReason"])
        self.assertEqual([t for t in self.mcp.tools() if t != "list_banks"], [])

    def test_recall_is_routed_with_one_git_call(self):
        self.mcp.banks.append({"bank_id": "coding-agent::acme-widgets", "name": "acme/widgets"})
        resolve_bank.resolve(str(self.root))      # the session-start resolve fills the cache
        ladder_state._ROOTS.clear()
        real, spawns = subprocess.run, []

        def counting(cmd, *a, **k):
            spawns.append(cmd)
            return real(cmd, *a, **k)

        with mock.patch.object(subprocess, "run", counting):
            out = self.pre("recall", {"query": "retries"})
        self.assertEqual(out["updatedInput"]["bank_id"], "coding-agent::acme-widgets")
        self.assertEqual(out["updatedInput"]["tags"], ["project:widgets"])
        self.assertLessEqual(len(spawns), 1, spawns)

    def test_a_fully_specified_recall_is_not_rewritten(self):
        self.mcp.banks.append({"bank_id": "coding-agent::acme-widgets", "name": "acme/widgets"})
        resolve_bank.resolve(str(self.root))
        exact = {"query": "retries", "bank_id": "coding-agent::acme-widgets", "tags": ["project:widgets"],
                 "tags_match": "all", "max_tokens": 1000, "budget": "low", "types": ["world", "experience"]}
        self.assertEqual(self.pre("recall", exact), {})

    def test_a_gate_that_raises_denies_instead_of_letting_the_call_through(self):
        with mock.patch.object(memory_gate, "gate", side_effect=RuntimeError("boom")):
            out = self.pre("retain", {"content": GOOD})
        self.assertEqual(out["permissionDecision"], "deny")

    def test_retain_creates_the_bank_and_is_recorded_only_after_it_ran(self):
        out = self.pre("retain", {"content": GOOD})
        self.assertIn("create_bank", self.mcp.tools())
        sent = out["updatedInput"]
        self.assertEqual(sent["bank_id"], "coding-agent::acme-widgets")
        self.assertEqual(memory_gate.retained_hashes(self.root), set())
        self.hook("posttooluse", {"tool_name": "mcp__plugin_feature-ladder_hindsight__retain", "cwd": str(self.root),
                                  "tool_input": sent, "tool_response": {"content": [{"type": "text", "text": "ok"}]}})
        self.assertEqual(len(memory_gate.retained_hashes(self.root)), 1)

    def test_a_retain_the_server_reported_as_an_error_is_not_recorded(self):
        sent = self.pre("retain", {"content": GOOD})["updatedInput"]
        self.hook("posttooluse", {"tool_name": "mcp__plugin_feature-ladder_hindsight__retain", "cwd": str(self.root),
                                  "tool_input": sent, "tool_response": {"isError": True, "content": []}})
        self.assertEqual(memory_gate.retained_hashes(self.root), set())


class NoRemote(RepoCase):
    remote = None

    def test_a_repo_without_a_remote_uses_the_slugged_folder_name(self):
        r = resolve_bank.resolve(str(self.root))
        self.assertEqual(r["bank_id"], "coding-agent::widgets")


class ClientInfo(unittest.TestCase):
    def test_the_client_reports_the_plugin_version_not_a_stale_literal(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(resolve_bank.client_version(), manifest["version"])


if __name__ == "__main__":
    unittest.main()
