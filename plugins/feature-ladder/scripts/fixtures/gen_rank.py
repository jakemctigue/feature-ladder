#!/usr/bin/env python3
"""Generate tests/fixtures/skill_ranking/cases.json from the CURRENT skill
selector and the SHIPPED skill-rules.json (personal and project rules are not
read; the inventory is synthetic, so the result is the same on every machine).

    python gen_rank.py            regenerate (review the diff before committing)
    python gen_rank.py --check    exit 1 if the committed fixture differs

Add or change a case HERE, never in the JSON. A case may carry "absent": skill
names that must never be chosen for it (written as expect_absent).
"""
import json
import sys
import tempfile
from pathlib import Path

import fixture_io

fixture_io.use("skill-selector")
import select_skills as ss  # noqa: E402

RULES_FILE = fixture_io.PLUGIN / "skills" / "skill-selector" / "skill-rules.json"
DEBUG, TDD = "superpowers:systematic-debugging", "superpowers:test-driven-development"


def T(role, text, kind="code", paths=(), flags=None, **kw):
    return dict(role=role, kind=kind, paths=list(paths), text=text, flags=flags or {}, **kw)


# (name, installed profile, {skill: body chars}, task[, skills that must stay absent])
CASES = [
    ("implementer-bugfix-code-gets-debugging-then-tdd", "claude-code", {}, T("implementer", "fix the crash when the queue is empty")),
    ("implementer-bugfix-with-real-skill-sizes-gets-both", "claude-code", {DEBUG: 9365, TDD: 9424}, T("implementer", "fix the crash when the queue is empty")),
    ("implementer-bugfix-claude-app-gets-app-equivalents", "claude-app", {}, T("implementer", "fix the crash when the queue is empty"), [DEBUG]),
    ("implementer-plain-feature-gets-tdd-only", "claude-code", {}, T("implementer", "add a retry wrapper around the upload")),
    ("prototype-keyword-cancels-tdd", "claude-code", {}, T("implementer", "quick prototype of the retry wrapper"), [TDD, "engineering:testing-strategy"]),
    ("sql-file-in-scope-adds-sql-skill", "claude-code", {}, T("implementer", "add the report view", paths=["db/report.sql"])),
    ("frontend-file-in-scope-adds-the-ui-skill", "claude-code", {}, T("implementer", "build the settings page", paths=["web/settings.tsx"])),
    ("security-flag-gives-the-senior-the-hardening-skill", "claude-code", {}, T("senior-implementer", "add the token exchange endpoint", flags={"security": True})),
    ("security-flag-alone-gives-an-implementer-nothing-extra", "claude-code", {}, T("implementer", "add the token exchange endpoint", flags={"security": True}), ["security-and-hardening"]),
    ("removed-aws-and-figma-rules-select-nothing", "claude-code", {}, T("mechanic", "sync the figma export to s3 with boto3 and cdk", kind="fixture")),
    ("data-loss-keyword-has-top-priority", "claude-code", {}, T("implementer", "drop table legacy_users and add the new schema")),
    ("max-skills-3-drops-the-rest", "claude-code", {}, T("implementer", "fix the failing code review feedback in the husky hook", paths=["a.sql"])),
    ("mechanic-docs-gets-no-skill-text", "claude-code", {}, T("mechanic", "draft the readme", kind="docs", paths=["README.md"]), ["engineering:documentation"]),
    ("domain-expert-library-docs-and-plans", "claude-code", {}, T("domain-expert", "choose a framework library and plan the migration", kind="analysis")),
    ("senior-implementer-gets-tdd-and-verification", "claude-code", {}, T("senior-implementer", "add the lock manager")),
    ("explicit-exclude-removes-a-rule-skill", "claude-code", {}, T("implementer", "add a retry wrapper", exclude=[TDD]), [TDD]),
    ("explicit-add-ranks-first", "claude-code", {}, T("implementer", "add a retry wrapper", add=["docx"])),
    ("nothing-installed-everything-is-unresolved", "none", {}, T("implementer", "add a retry wrapper")),
    ("oversize-skill-body-is-dropped-for-the-role-budget", "claude-code", {TDD: 21000}, T("implementer", "add a retry wrapper")),
]
DOC = ("Skill ranking cases for select_skills.select() using the SHIPPED skill-rules.json (personal and project rules are "
       "not read). 'installed' picks a synthetic inventory: claude-code (every skill the rules name), claude-app (no "
       "superpowers:* skills) or none. 'body_chars' overrides the size of a skill's text. 'expect_order' is the exact "
       "chosen order; 'expect_dropped' maps skill -> reason fragment; 'expect_absent' lists skills that must not be chosen.")


def installed(profile, names):
    if profile == "claude-code":
        return names
    if profile == "claude-app":
        return [n for n in names if not n.startswith("superpowers:")]
    if profile == "none":
        return []
    raise ValueError(profile)


def build():
    data = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    names = sorted({r["skill"] for r in data["rules"]})
    settings = {"max_skills": data["max_skills"], "max_chars": data["max_chars"],
                "max_chars_by_role": {k.lower(): v for k, v in data["max_chars_by_role"].items()}}
    out = {"_doc": DOC, "cases": []}
    with tempfile.TemporaryDirectory() as tmp:
        for name, profile, body, task, *absent in CASES:
            inv = {}
            for n in installed(profile, names):
                p = Path(tmp) / (n.replace(":", "__") + ".md")
                p.write_text("---\nname: x\n---\n" + "b" * body.get(n, 100), encoding="utf-8")
                inv[n] = p
            r = ss.select(task, settings, data["rules"], inv)
            case = {"name": name, "installed": profile, "task": task,
                    "expect_order": [s["skill"] for s in r["skills"]],
                    "expect_dropped": {d["skill"]: d["reason"] for d in r["dropped"]},
                    "expect_unresolved": sorted(r["unresolved"])}
            if body:
                case["body_chars"] = body
            if absent:
                missing = [n for n in absent[0] if n in case["expect_order"]]
                if missing:
                    raise SystemExit(f"{name}: {missing} must stay absent but the selector chose them")
                case["expect_absent"] = absent[0]
            out["cases"].append(case)
    return out


if __name__ == "__main__":
    sys.exit(fixture_io.emit(fixture_io.FIXTURES / "skill_ranking" / "cases.json", build(), "--check" in sys.argv[1:]))
