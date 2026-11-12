"""Every skill, agent and command frontmatter must be YAML a strict parser accepts.

A plain (unquoted) YAML value may not contain ": " or " #": the first starts a
nested mapping and makes the whole frontmatter invalid, the second starts a
comment and silently truncates the value. v1.15.1 shipped an unquoted
"review: pending" in the orchestration-ladder description, so the host dropped
the skill. The suite has no YAML dependency, so this checks the rule directly.

Skills also follow the Agent Skills format (https://agentskills.io/specification):
name matches the directory, description is 1-1024 characters.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import re
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parents[1]
SKILLS = sorted(PLUGIN.glob("skills/*/SKILL.md")) + sorted(REPO.glob("claude-app/*/SKILL.md"))
OTHERS = sorted(PLUGIN.glob("agents/*.md")) + sorted(PLUGIN.glob("commands/*.md"))
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
KEY = re.compile(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$")


def top_level(path):
    """The frontmatter's top-level `key: value` lines, values as written."""
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not m:
        return None
    pairs = {}
    for line in m.group(1).split("\n"):
        k = KEY.match(line)
        if k:
            pairs[k.group(1)] = k.group(2).strip()
    return pairs


def unquote(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


class Frontmatter(unittest.TestCase):
    def test_files_are_found(self):
        self.assertTrue(SKILLS)
        self.assertTrue(OTHERS)

    def test_plain_values_are_valid_yaml(self):
        for path in SKILLS + OTHERS:
            pairs = top_level(path)
            self.assertIsNotNone(pairs, f"{path}: no frontmatter")
            for key, value in pairs.items():
                if not value or value[0] in "\"'|>[{":
                    continue
                with self.subTest(file=str(path.relative_to(REPO)), key=key):
                    self.assertNotIn(": ", value, "quote this value: ': ' starts a nested mapping")
                    self.assertFalse(value.endswith(":"), "quote this value: it ends with ':'")
                    self.assertNotIn(" #", value, "quote this value: ' #' starts a comment")

    def test_skills_follow_the_agent_skills_format(self):
        for path in SKILLS:
            pairs = top_level(path)
            with self.subTest(skill=path.parent.name):
                name = unquote(pairs.get("name", ""))
                self.assertRegex(name, NAME)
                self.assertLessEqual(len(name), 64)
                self.assertEqual(name, path.parent.name)
                description = pairs.get("description", "")
                self.assertTrue(description, "description is missing or empty")
                if description[0] not in "|>":
                    self.assertTrue(1 <= len(unquote(description)) <= 1024, len(description))


if __name__ == "__main__":
    unittest.main()
