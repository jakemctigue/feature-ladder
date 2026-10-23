"""Manifest conformance for the surfaces docs/harness-compat.md marks as
supported (Claude Code plugin, Claude app skill): the plugin and marketplace
manifests, .mcp.json and hooks.json parse and carry the keys the host needs;
every path they, the commands and the agents reference exists; every hook
command points at a real script. Other harnesses are "untested" in the matrix
and deliberately have no checks here.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import json
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}/"
PATH_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([A-Za-z0-9_./-]+)")


def jload(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def frontmatter(path):
    text = Path(path).read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    out, key = {}, None
    for line in m.group(1).split("\n"):
        if re.match(r"^[A-Za-z_-]+:", line):
            key, _, val = line.partition(":")
            out[key] = val.strip()
        elif key and line.strip().startswith("- "):
            out.setdefault(key + "[]", []).append(line.strip()[2:].strip())
    return out


class PluginManifest(unittest.TestCase):
    def test_plugin_json_has_required_keys(self):
        p = jload(PLUGIN / ".claude-plugin" / "plugin.json")
        for key in ("name", "version", "description", "license"):
            self.assertTrue(p.get(key), key)
        self.assertRegex(p["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(p["name"], "feature-ladder")

    def test_marketplace_points_at_this_plugin(self):
        m = jload(REPO / ".claude-plugin" / "marketplace.json")
        self.assertTrue(m.get("name"))
        self.assertTrue(m.get("owner", {}).get("name"))
        self.assertEqual(len(m["plugins"]), 1)
        entry = m["plugins"][0]
        self.assertEqual(entry["name"], jload(PLUGIN / ".claude-plugin" / "plugin.json")["name"])
        self.assertTrue(entry["description"])
        target = (REPO / entry["source"]).resolve()
        self.assertEqual(target, PLUGIN.resolve())
        self.assertTrue((target / ".claude-plugin" / "plugin.json").is_file())


class McpManifest(unittest.TestCase):
    def test_mcp_json_servers_are_well_formed(self):
        servers = jload(PLUGIN / ".mcp.json")["mcpServers"]
        self.assertTrue(servers)
        for name, spec in servers.items():
            with self.subTest(server=name):
                if spec.get("type") == "http":
                    self.assertTrue(spec.get("url"))
                else:
                    self.assertTrue(spec.get("command"))

    def test_mcp_secrets_come_from_the_environment_not_the_file(self):
        text = (PLUGIN / ".mcp.json").read_text(encoding="utf-8")
        for value in re.findall(r'"Authorization":\s*"([^"]*)"', text):
            self.assertIn("${", value)


class HooksManifest(unittest.TestCase):
    def setUp(self):
        self.hooks = jload(PLUGIN / "hooks" / "hooks.json")

    def commands(self):
        for event, groups in self.hooks["hooks"].items():
            for group in groups:
                for h in group["hooks"]:
                    yield event, h

    def test_hooks_json_shape(self):
        self.assertTrue(self.hooks.get("description"))
        known = {"SessionStart", "PreToolUse", "PostToolUse", "SubagentStop", "Stop", "UserPromptSubmit"}
        self.assertTrue(set(self.hooks["hooks"]) <= known, set(self.hooks["hooks"]) - known)
        n = 0
        for event, h in self.commands():
            n += 1
            self.assertEqual(h["type"], "command", event)
            self.assertTrue(h["command"], event)
            self.assertIsInstance(h.get("timeout", 1), int)
        self.assertGreater(n, 0)

    def test_every_hook_command_references_existing_scripts(self):
        for event, h in self.commands():
            refs = PATH_REF.findall(h["command"])
            self.assertTrue(refs, f"{event}: command does not use ${{CLAUDE_PLUGIN_ROOT}}")
            for ref in refs:
                with self.subTest(event=event, ref=ref):
                    self.assertTrue((PLUGIN / ref).is_file(), ref)

    def test_pretooluse_matchers_are_valid_regexes(self):
        for group in self.hooks["hooks"].get("PreToolUse", []):
            re.compile(group["matcher"])


class Components(unittest.TestCase):
    def test_every_agent_has_frontmatter_and_existing_skills(self):
        agents = sorted((PLUGIN / "agents").glob("*.md"))
        self.assertTrue(agents)
        for f in agents:
            with self.subTest(agent=f.name):
                fm = frontmatter(f)
                self.assertIsNotNone(fm)
                self.assertEqual(fm.get("name"), f.stem)
                self.assertTrue(fm.get("description"))
                self.assertIn(fm.get("model"), {"haiku", "sonnet", "opus", "fable", "inherit"})
                for skill in fm.get("skills[]", []):
                    ns, _, name = skill.partition(":")
                    self.assertEqual(ns, "feature-ladder", skill)
                    self.assertTrue((PLUGIN / "skills" / name / "SKILL.md").is_file(), skill)

    def test_every_command_has_a_description(self):
        commands = sorted((PLUGIN / "commands").glob("*.md"))
        self.assertTrue(commands)
        for f in commands:
            with self.subTest(command=f.name):
                fm = frontmatter(f)
                self.assertIsNotNone(fm)
                self.assertTrue(fm.get("description"))

    def test_every_skill_dir_has_a_named_skill_md(self):
        dirs = [d for d in sorted((PLUGIN / "skills").iterdir()) if d.is_dir()]
        self.assertTrue(dirs)
        for d in dirs:
            with self.subTest(skill=d.name):
                fm = frontmatter(d / "SKILL.md")
                self.assertIsNotNone(fm)
                self.assertEqual(fm.get("name"), d.name)
                self.assertTrue(fm.get("description"))

    def test_plugin_root_paths_in_commands_agents_and_skills_exist(self):
        files = [*(PLUGIN / "commands").glob("*.md"), *(PLUGIN / "agents").glob("*.md"),
                 *(PLUGIN / "skills").glob("*/SKILL.md")]
        checked = 0
        for f in files:
            for ref in PATH_REF.findall(f.read_text(encoding="utf-8-sig")):
                ref = ref.rstrip(".,;:)`'\"")
                if any(c in ref for c in "<>*"):
                    continue
                checked += 1
                with self.subTest(file=f.name, ref=ref):
                    self.assertTrue((PLUGIN / ref).exists(), ref)
        self.assertGreater(checked, 0)

    def test_claude_app_skill_has_frontmatter(self):
        fm = frontmatter(REPO / "claude-app" / "model-ladder-router" / "SKILL.md")
        self.assertIsNotNone(fm)
        self.assertEqual(fm.get("name"), "model-ladder-router")
        self.assertTrue(fm.get("description"))


class CompatMatrix(unittest.TestCase):
    def test_matrix_marks_only_tested_surfaces_as_supported(self):
        text = (REPO / "docs" / "harness-compat.md").read_text(encoding="utf-8")
        rows = {}
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 7 and line.startswith("|") and not set(cells[0]) <= set("-: "):
                rows[cells[0]] = cells[1:]
        for surface in ("Claude Code", "Claude app", "Codex", "OpenCode", "Zed", "dmux", "Generic agent"):
            self.assertIn(surface, rows)
        for surface, cells in rows.items():
            if surface in ("Claude Code", "Claude app", "Surface"):
                continue
            for cell in cells:
                self.assertEqual(cell, "untested", f"{surface}: {cell}")


class IntegratorTools(unittest.TestCase):
    def test_integrator_keeps_coding_tools_denies_web(self):
        fm = frontmatter(PLUGIN / "agents" / "integrator.md")
        self.assertIsNotNone(fm)
        denied = fm.get("disallowedTools[]", [])
        for tool in ("Edit", "Write", "Bash", "Read", "Glob", "Grep"):
            self.assertNotIn(tool, denied, f"integrator must keep {tool}")
        for tool in ("WebSearch", "WebFetch"):
            self.assertIn(tool, denied, f"integrator must still deny {tool}")


if __name__ == "__main__":
    unittest.main()
