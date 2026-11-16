"""Regression tests for the skill selector's cost and correctness: no inventory
when no skill text can be given, one walk per skills folder, project rules that
stop at the repository root, broken rules that are reported instead of silently
applied, and frontmatter read from the head of the file.

Run from the repo root:  python3 -m unittest discover -s plugins/feature-ladder/tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "skills" / "skill-selector" / "scripts"))

import select_skills as ss  # noqa: E402

SHIPPED = json.loads((PLUGIN / "skills" / "skill-selector" / "skill-rules.json").read_text(encoding="utf-8"))
SETTINGS = {"max_skills": SHIPPED["max_skills"], "max_chars": SHIPPED["max_chars"],
            "max_chars_by_role": {k.lower(): v for k, v in SHIPPED["max_chars_by_role"].items()}}


def write_skill(folder, name, body="# Body\nDo it.\n", bom=False):
    folder.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: about {name}\n---\n{body}"
    (folder / "SKILL.md").write_text(("﻿" if bom else "") + text, encoding="utf-8")
    return folder / "SKILL.md"


@contextlib.contextmanager
def fake_world(tmp, cwd):
    """A fake home and cwd for in-process calls (Path.home / Path.cwd patched)."""
    home = Path(tmp) / "home"
    home.mkdir(parents=True, exist_ok=True)
    with mock.patch.object(Path, "home", return_value=home), mock.patch.object(Path, "cwd", return_value=Path(cwd)):
        yield home


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = ss.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class NoInventoryWhenNothingCanBeGiven(unittest.TestCase):
    def setUp(self):
        self.patches = [mock.patch.object(ss, "load_rules", return_value=(SETTINGS, SHIPPED["rules"], ["shipped"])),
                        mock.patch.object(ss, "inventory_entries", side_effect=AssertionError("inventory was built"))]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_a_role_with_no_budget_never_builds_the_inventory(self):
        code, out, err = run_main(json.dumps({"role": "mechanic", "kind": "docs", "paths": ["README.md"],
                                              "text": "draft the readme"}))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["skills"], data["max_chars"], data["unresolved"]), ([], 0, []))
        self.assertTrue(data["dropped"])
        self.assertTrue(all("budget 0" in d["reason"] for d in data["dropped"]))

    def test_a_task_no_rule_matches_never_builds_the_inventory(self):
        code, out, err = run_main(json.dumps({"role": "evidence-collector", "kind": "research", "text": "nothing special"}))
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["skills"], data["dropped"], data["unresolved"]), ([], [], []))

    def test_select_accepts_a_lazy_inventory(self):
        calls = []

        def lazy():
            calls.append(1)
            return {}

        r = ss.select({"role": "implementer", "kind": "code", "text": "add a retry wrapper"}, SETTINGS, SHIPPED["rules"], lazy)
        self.assertEqual(calls, [1])
        self.assertIn("superpowers:test-driven-development", r["unresolved"])
        ss.select({"role": "mechanic", "kind": "docs", "text": "x"}, SETTINGS, SHIPPED["rules"], lazy)
        self.assertEqual(calls, [1])


class ProjectWalk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.repo = self.home / "work" / "repo"
        (self.repo / ".git").mkdir(parents=True)
        (self.repo / "src" / "deep").mkdir(parents=True)
        write_skill(self.home / ".claude" / "skills" / "personal-one", "personal-one")
        write_skill(self.repo / ".claude" / "skills" / "project-one", "project-one")
        write_skill(self.home / "work" / ".claude" / "skills" / "outside-repo", "outside-repo")

    def test_the_walk_stops_at_the_repository_root(self):
        with fake_world(self.base, self.repo / "src" / "deep"):
            self.assertEqual(ss._project_dirs(), [self.repo / "src" / "deep", self.repo / "src", self.repo])

    def test_the_home_folder_is_never_a_project(self):
        (self.repo / ".git").rmdir()
        with fake_world(self.base, self.repo):
            self.assertEqual(ss._project_dirs(), [self.repo, self.home / "work"])
        with fake_world(self.base, self.home):
            self.assertEqual(ss._project_dirs(), [])

    def test_a_rules_file_in_an_ancestor_or_the_home_folder_is_not_project_rules(self):
        for d in (self.home, self.home / "work"):
            (d / ".feature-ladder").mkdir()
            (d / ".feature-ladder" / "skill-rules.json").write_text('{"rules": []}', encoding="utf-8")
        with fake_world(self.base, self.repo / "src"):
            self.assertIsNone(ss._project_rules_file())
            (self.repo / ".feature-ladder").mkdir()
            mine = self.repo / ".feature-ladder" / "skill-rules.json"
            mine.write_text('{"rules": []}', encoding="utf-8")
            self.assertEqual(ss._project_rules_file(), mine)

    def test_each_skills_folder_is_walked_once_and_sources_are_right(self):
        walked = []
        real = ss._skill_files

        def counting(root, depth=3):
            walked.append(Path(root))
            return real(root, depth)

        with fake_world(self.base, self.repo / "src"), mock.patch.object(ss, "_skill_files", side_effect=counting):
            entries = {name: source for name, _, source in ss.inventory_entries()}
        self.assertEqual(walked.count(self.home / ".claude" / "skills"), 1)
        self.assertEqual(len(walked), len(set(walked)), walked)
        self.assertEqual(entries["personal-one"], "personal")
        self.assertEqual(entries["project-one"], "project")
        self.assertNotIn("outside-repo", entries)


class PluginSkills(unittest.TestCase):
    def test_a_plugin_with_two_names_is_walked_once_and_answers_to_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            work = base / "work"
            (work / ".git").mkdir(parents=True)
            plug = base / "store" / "data-agent-kit"
            (plug / ".claude-plugin").mkdir(parents=True)
            (plug / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": "dak"}), encoding="utf-8")
            write_skill(plug / "skills" / "bigquery-sql", "bigquery-sql")
            walked = []
            real = ss._skill_files

            def counting(root, depth=3):
                walked.append(Path(root))
                return real(root, depth)

            with fake_world(base, work) as home:
                (home / ".claude" / "plugins").mkdir(parents=True)
                (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
                    {"plugins": {"data-agent-kit@official": [{"installPath": str(plug)}]}}), encoding="utf-8")
                with mock.patch.object(ss, "_skill_files", side_effect=counting):
                    entries = ss.inventory_entries()
            names = [n for n, _, _ in entries]
            self.assertEqual(walked.count(plug / "skills"), 1)
            self.assertLess(names.index("data-agent-kit:bigquery-sql"), names.index("dak:bigquery-sql"))


class Frontmatter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_bom_does_not_hide_the_frontmatter(self):
        f = write_skill(self.dir / "bom", "with-bom", bom=True)
        meta, body = ss._frontmatter(f)
        self.assertEqual(meta["name"], "with-bom")
        self.assertEqual(body, "# Body\nDo it.\n")
        self.assertEqual(ss._meta_only(f)["name"], "with-bom")

    def test_meta_only_agrees_with_the_full_parse(self):
        cases = {
            "plain": "---\nname: plain\ndescription: x\n---\n" + "b" * 50000,
            "folded": "---\nname: folded\ndescription: >-\n  Deploys things\n  to Railway.\n---\n# F\n",
            "none": "# no frontmatter here\n",
            "long-head": "---\nname: long-head\ndescription: " + "d" * (ss.HEAD_CHARS + 500) + "\nlicense: MIT\n---\nbody\n",
            "unclosed": "---\nname: unclosed\n",
        }
        for name, text in cases.items():
            f = self.dir / f"{name}.md"
            f.write_text(text, encoding="utf-8")
            self.assertEqual(ss._meta_only(f), ss._frontmatter(f)[0], name)
        self.assertEqual(ss._meta_only(self.dir / "long-head.md")["license"], "MIT")
        self.assertEqual(ss._meta_only(self.dir / "missing.md"), {})

    def test_the_inventory_does_not_read_skill_bodies(self):
        base = self.dir.resolve()
        work = base / "work"
        (work / ".git").mkdir(parents=True)
        write_skill(work / ".claude" / "skills" / "one", "one", body="x" * 100000)
        with fake_world(base, work), mock.patch.object(ss, "_frontmatter", side_effect=AssertionError("body read")):
            names = [n for n, _, _ in ss.inventory_entries()]
        self.assertIn("one", names)


class BrokenRules(unittest.TestCase):
    INV = {"demo": Path("demo.md")}

    def rule(self, **over):
        r = {"id": "demo", "skill": "demo", "priority": 90, "when": {"keywords": ["demo"]}}
        r.update(over)
        return r

    def chosen(self, rule, text="add the demo widget"):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "demo.md"
            p.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
            r = ss.select({"role": "implementer", "kind": "code", "text": text}, SETTINGS, [rule], {"demo": p})
            return [s["skill"] for s in r["skills"]]

    def test_a_misspelt_group_makes_the_rule_broken_not_silently_different(self):
        good = self.rule(unless={"keywords": ["never"]})
        self.assertEqual(self.chosen(good), ["demo"])
        for bad in (self.rule(unless={"keyword": ["never"]}),                       # was: cancelled every time
                    self.rule(when={"keywords": ["demo"], "role": ["mechanic"]})):  # was: the role limit was ignored
            self.assertFalse(ss._valid(bad))
            self.assertEqual(self.chosen(bad), [])
            problems = " ".join(ss.rule_problems(bad))
            self.assertIn("unknown group", problems)
        self.assertEqual(ss.rule_problems(good), [])

    def test_unless_and_all_keywords_still_work_on_a_valid_rule(self):
        self.assertEqual(self.chosen(self.rule(unless={"keywords": ["prototype"]}), "a demo prototype"), [])
        both = self.rule(when={"all_keywords": ["demo", "widget"]})
        self.assertEqual(self.chosen(both), ["demo"])
        self.assertEqual(self.chosen(both, "add the demo page"), [])

    def test_list_names_the_broken_rule(self):
        bad = self.rule(unless={"keyword": ["never"]})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ss.list_all(SETTINGS, [bad, {"id": "empty", "skill": "demo"}], ["x"], {"demo": Path("demo.md")})
        text = out.getvalue()
        self.assertIn("Rule problems (2)", text)
        self.assertIn("rule demo: unknown group 'keyword' in unless", text)
        self.assertIn("rule empty: needs 'skill' and at least one 'when' group", text)


class TaskFile(unittest.TestCase):
    def test_file_reads_a_task_with_a_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "task.json"
            f.write_text("﻿" + json.dumps({"role": "mechanic", "kind": "docs", "text": "draft the readme"}),
                         encoding="utf-8")
            with mock.patch.object(ss, "load_rules", return_value=(SETTINGS, SHIPPED["rules"], ["shipped"])):
                code, out, err = run_main("--file", str(f))
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["max_chars"], 0)


class Gitignore(unittest.TestCase):
    def test_an_extra_file_under_feature_ladder_is_ignored_with_the_shared_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / ".feature-ladder" / "skill-catalog.json"
            out.parent.mkdir()
            out.write_text("{}", encoding="utf-8")
            ss._ignore_in_feature_ladder(out)
            ss._ignore_in_feature_ladder(out)
            lines = (out.parent / ".gitignore").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines.count("skill-catalog.json"), 1)
            for entry in ("cards/", "runs/", "run.json"):
                self.assertEqual(lines.count(entry), 1)


if __name__ == "__main__":
    unittest.main()
