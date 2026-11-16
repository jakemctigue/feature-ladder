"""The Claude app skill and the README must point at the release in plugin.json,
and the app must download every file the downloaded scripts need."""
import ast
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLUGIN = REPO / "plugins" / "feature-ladder"
SKILL = REPO / "claude-app" / "model-ladder-router" / "SKILL.md"
VERSION = "v" + json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
# The shell variable a download loop reads from -> the folder it stands for in this repo.
BASES = {"B": PLUGIN / "skills", "H": PLUGIN / "hooks" / "scripts"}
LOOP = re.compile(r"for f in (.+?); do\n(.*?)\ndone", re.S)


def downloads():
    """[(repo file, where the app saves it relative to $FL)] for every download loop in the skill."""
    text = SKILL.read_text(encoding="utf-8").replace("\r\n", "\n")
    found = []
    for names, body in LOOP.findall(text):
        src = re.search(r'curl[^\n]*?"\$(\w+)/\$f"', body)
        dest = re.search(r'-o "\$FL/([^"]*)\$f"', body)
        if not src or not dest:
            continue
        for f in names.split():
            found.append((BASES[src.group(1)] / f, dest.group(1) + f))
    return found


def repo_modules():
    """Importable script names in the plugin -> their file (tests and one-off tools excluded)."""
    files = list(PLUGIN.glob("skills/*/scripts/*.py")) + list(PLUGIN.glob("hooks/scripts/*.py"))
    return {p.stem: p for p in files}


def imported(path):
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class ReleasePins(unittest.TestCase):
    def test_app_skill_pins_the_plugin_version(self):
        pins = set(re.findall(r"\bv\d+\.\d+\.\d+\b", SKILL.read_text(encoding="utf-8")))
        self.assertEqual(pins, {VERSION})

    def test_readme_names_the_plugin_version(self):
        m = re.search(r"currently `(v\d+\.\d+\.\d+)`", (REPO / "README.md").read_text(encoding="utf-8"))
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), VERSION)

    def test_every_file_the_app_downloads_exists(self):
        files = downloads()
        self.assertTrue(files)
        for src, _ in files:
            self.assertTrue(src.is_file(), src)

    def test_every_module_a_downloaded_script_imports_is_downloaded_too(self):
        modules = repo_modules()
        got = {src.stem for src, _ in downloads() if src.suffix == ".py"}
        for src, _ in downloads():
            if src.suffix != ".py":
                continue
            for name in sorted(imported(src) & set(modules)):
                with self.subTest(script=src.name, imports=name):
                    self.assertIn(name, got, f"{src.name} imports {name}.py, which the app skill does not download")

    def test_a_downloaded_module_lands_where_its_importer_looks(self):
        """Every script folder is put on sys.path by the scripts that import from it;
        a hook script has no such folder in the app, so it must sit beside its importer."""
        where = {src.stem: Path(dest).parent.as_posix() for src, dest in downloads() if src.suffix == ".py"}
        skill_dirs = {p.parent.relative_to(PLUGIN / "skills").as_posix() for p in PLUGIN.glob("skills/*/scripts/*.py")}
        for name, folder in where.items():
            with self.subTest(module=name):
                self.assertIn(folder, skill_dirs)

    def test_files_read_by_path_are_downloaded(self):
        got = {dest for _, dest in downloads()}
        self.assertIn("worker-contract/SKILL.md", got)  # external_worker.py reads it for GLM cards
        self.assertIn("skill-selector/skill-rules.json", got)

    def test_readme_lists_what_the_app_downloads(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        section = readme[readme.index("### Claude app"):readme.index("### Hindsight (memory)")]
        for src, dest in downloads():
            name = dest if src.name == "SKILL.md" else src.name
            with self.subTest(file=name):
                self.assertIn(f"`{name}`", section)

    def test_every_script_the_skill_tells_the_app_to_run_is_downloaded(self):
        text = SKILL.read_text(encoding="utf-8")
        got = {src.name for src, _ in downloads()}
        for name in sorted(set(re.findall(r"\b([a-z_]+\.py)\b", text))):
            with self.subTest(script=name):
                self.assertIn(name, got)


if __name__ == "__main__":
    unittest.main()
