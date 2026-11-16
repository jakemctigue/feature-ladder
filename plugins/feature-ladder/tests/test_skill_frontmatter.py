"""Every skill, agent and command frontmatter must be YAML a strict parser accepts.

A plain (unquoted) YAML value may not contain ": " or " #": the first starts a
nested mapping and makes the whole frontmatter invalid, the second starts a
comment and silently truncates the value. v1.15.1 shipped an unquoted
"review: pending" in the orchestration-ladder description, so the host dropped
the skill. The suite has no YAML dependency, so this checks the rule directly.

A value that starts with "[" or "{" is a YAML flow collection: it must close at
the end of the line (`[--review] [--base <ref>]` is invalid YAML and made the
host drop /feature-ladder:wrap), and it is a list or mapping, never a string,
so the text keys (name, description, argument-hint) must be quoted instead.
When PyYAML happens to be installed, every frontmatter is also parsed with it.

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
TEXT_KEYS = ("name", "description", "argument-hint")
PAIRS = {"[": "]", "{": "}"}

try:  # optional: the suite must pass without it
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def frontmatter(path):
    text = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    return m.group(1) if m else None


def flow_closes_at_end(value):
    """True when a value opening with [ or { is one flow collection that ends
    exactly at the end of the line (quotes respected)."""
    stack, quote = [], None
    for i, ch in enumerate(value):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch in PAIRS:
            stack.append(PAIRS[ch])
        elif ch in "]}":
            if not stack or stack.pop() != ch:
                return False
            if not stack:
                return i == len(value) - 1
    return False


def top_level(path):
    """The frontmatter's top-level `key: value` lines, values as written."""
    block = frontmatter(path)
    if block is None:
        return None
    pairs = {}
    for line in block.split("\n"):
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

    def test_flow_values_close_at_the_end_of_the_line(self):
        for path in SKILLS + OTHERS:
            for key, value in top_level(path).items():
                if value[:1] in PAIRS:
                    with self.subTest(file=str(path.relative_to(REPO)), key=key):
                        self.assertTrue(flow_closes_at_end(value),
                                        "invalid YAML: text follows the closing bracket; quote the value")

    def test_text_keys_are_strings_not_flow_collections(self):
        for path in SKILLS + OTHERS:
            pairs = top_level(path)
            for key in TEXT_KEYS:
                value = pairs.get(key, "")
                with self.subTest(file=str(path.relative_to(REPO)), key=key):
                    self.assertFalse(value[:1] in PAIRS,
                                     "quote this value: unquoted it is a YAML list or mapping, not text")

    def test_the_flow_check_itself(self):
        self.assertTrue(flow_closes_at_end("[Read, Write]"))
        self.assertTrue(flow_closes_at_end('["a]b", [c]]'))
        self.assertFalse(flow_closes_at_end("[--review] [--adversarial]"))
        self.assertFalse(flow_closes_at_end("[--base <ref>] trailing"))
        self.assertFalse(flow_closes_at_end("[unclosed"))

    @unittest.skipIf(yaml is None, "PyYAML not installed; the dependency-free checks above still run")
    def test_a_strict_yaml_parser_accepts_every_frontmatter(self):
        for path in SKILLS + OTHERS:
            with self.subTest(file=str(path.relative_to(REPO))):
                try:
                    data = yaml.safe_load(frontmatter(path))
                except yaml.YAMLError as e:
                    self.fail(f"invalid YAML: {e}")
                self.assertIsInstance(data, dict)
                for key in TEXT_KEYS:
                    if key in data:
                        self.assertIsInstance(data[key], str, key)

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
