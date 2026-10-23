import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, "skills/skill-selector/scripts")
import select_skills as ss
rules_file = Path("skills/skill-selector/skill-rules.json")
data = json.loads(rules_file.read_text(encoding="utf-8"))
names = sorted({r["skill"] for r in data["rules"]})
settings = {"max_skills": data["max_skills"], "max_chars": data["max_chars"], "max_chars_by_role": {k.lower(): v for k, v in data["max_chars_by_role"].items()}}
def inv_for(profile, body):
    if profile == "claude-code": ns = names
    elif profile == "claude-app": ns = [n for n in names if not n.startswith("superpowers:")]
    elif profile == "none": ns = []
    return ns
T = lambda role, text, kind="code", paths=(), flags=None, **kw: dict(role=role, kind=kind, paths=list(paths), text=text, flags=flags or {}, **kw)
cases = [
 ("implementer-bugfix-code-gets-debugging-then-tdd", "claude-code", {}, T("implementer", "fix the crash when the queue is empty")),
 ("implementer-bugfix-claude-app-gets-app-equivalents", "claude-app", {}, T("implementer", "fix the crash when the queue is empty")),
 ("implementer-plain-feature-gets-tdd-only", "claude-code", {}, T("implementer", "add a retry wrapper around the upload")),
 ("prototype-keyword-cancels-tdd", "claude-code", {}, T("implementer", "quick prototype of the retry wrapper")),
 ("sql-file-in-scope-adds-sql-skill", "claude-code", {}, T("implementer", "add the report view", paths=["db/report.sql"])),
 ("figma-keyword-outranks-everything-for-implementer", "claude-code", {}, T("implementer", "build the settings page from the figma frame", paths=["web/settings.tsx"])),
 ("data-loss-keyword-has-top-priority", "claude-code", {}, T("implementer", "drop table legacy_users and add the new schema")),
 ("max-skills-3-drops-the-rest", "claude-code", {}, T("implementer", "fix the failing code review feedback in the figma export", paths=["a.sql"])),
 ("mechanic-docs-gets-no-skill-text", "claude-code", {}, T("mechanic", "draft the readme", kind="docs", paths=["README.md"])),
 ("domain-expert-library-docs-and-plans", "claude-code", {}, T("domain-expert", "choose a framework library and plan the migration", kind="analysis")),
 ("senior-implementer-gets-tdd-and-verification", "claude-code", {}, T("senior-implementer", "add the lock manager")),
 ("explicit-exclude-removes-a-rule-skill", "claude-code", {}, T("implementer", "add a retry wrapper", exclude=["superpowers:test-driven-development"])),
 ("explicit-add-ranks-first", "claude-code", {}, T("implementer", "add a retry wrapper", add=["docx"])),
 ("nothing-installed-everything-is-unresolved", "none", {}, T("implementer", "add a retry wrapper")),
 ("oversize-skill-body-is-dropped-for-the-role-budget", "claude-code", {"superpowers:test-driven-development": 9000}, T("implementer", "add a retry wrapper")),
]
out = {"_doc": "Skill ranking cases for select_skills.select() using the SHIPPED skill-rules.json (personal and project rules are not read). 'installed' picks a synthetic inventory: claude-code (every skill the rules name), claude-app (no superpowers:* skills) or none. 'body_chars' overrides the size of a skill's text. 'expect_order' is the exact chosen order; 'expect_dropped' maps skill -> reason fragment; 'expect_absent' lists skills that must not be chosen.", "cases": []}
with tempfile.TemporaryDirectory() as tmp:
  for name, profile, body, task in cases:
    inv = {}
    for n in inv_for(profile, body):
        p = Path(tmp) / (n.replace(":", "__") + ".md")
        p.write_text("---\nname: x\n---\n" + "b" * body.get(n, 100), encoding="utf-8")
        inv[n] = p
    r = ss.select(task, settings, data["rules"], inv)
    exp = {"expect_order": [s["skill"] for s in r["skills"]],
           "expect_dropped": {d["skill"]: d["reason"] for d in r["dropped"]},
           "expect_unresolved": sorted(r["unresolved"])}
    case = {"name": name, "installed": profile, "task": task, **exp}
    if body: case["body_chars"] = body
    out["cases"].append(case)
    print(name, exp)
with open("tests/fixtures/skill_ranking/cases.json","w",encoding="utf-8",newline="\n") as fh:
    json.dump(out, fh, indent=2, sort_keys=True); fh.write("\n")
