#!/usr/bin/env python3
"""
Deterministic skill selector for the feature-ladder plugin.

Given ONE task (the router's role, kind, files in scope, task text, flags), it
decides exactly which skills the worker gets, using rules in skill-rules.json.
Same task + same rules + same installed skills -> same skills, every time.
No model is consulted.

Usage
  python3 select_skills.py '<task json>'            selection as JSON
  python3 select_skills.py --inject '<task json>'   markdown block with the skills' full text
  python3 select_skills.py --inject --out PATH '<task json>'
                                                    write that block to PATH and print only the
                                                    short "Skills" line for the Task Card
  python3 select_skills.py --file task.json [--inject] [--out PATH]
  python3 select_skills.py --list                   every resolvable skill + rule problems

--out is the normal way to use the selector from a coordinator: the skill text
(up to max_chars) goes straight to a file the worker reads, so it never passes
through the coordinator's context or its output. The printed line names the
file by its absolute path (workers in a git worktree have a different cwd).

Task fields (all optional)
  role     router role or agent name: mechanic, evidence-collector, implementer,
           senior-implementer, domain-expert, domain-expert-high, deep-verifier, ...
  kind     code | docs | fixture | check | research | analysis
  paths    files/dirs in the card's Scope; rule globs match these
  text     the card's goal + description; rule keywords match this
  flags    router booleans, e.g. {"security": true, "persistence": true}
  add      skill names the coordinator adds explicitly (logged as such)
  exclude  skill names to drop

Rules are merged from, in order (later wins on the same rule id):
  1. <plugin>/skills/skill-selector/skill-rules.json   (shipped defaults)
  2. ~/.claude/feature-ladder/skill-rules.json          (your personal rules)
  3. <repo>/.feature-ladder/skill-rules.json            (per-project rules)
"""
import fnmatch
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
SKILL_DIR = HERE.parents[1]                 # .../skills/skill-selector
PLUGIN_ROOT = HERE.parents[3]               # .../feature-ladder
PLUGIN_NAME = "feature-ladder"
GROUPS = ("roles", "kinds", "globs", "keywords", "all_keywords", "flags")


# ---------------------------------------------------------------- rules ----
def _project_rules_file():
    d = Path.cwd().resolve()
    for p in [d, *d.parents]:
        f = p / ".feature-ladder" / "skill-rules.json"
        if f.is_file():
            return f
    return None


def load_rules():
    sources = [SKILL_DIR / "skill-rules.json",
               Path.home() / ".claude" / "feature-ladder" / "skill-rules.json",
               _project_rules_file()]
    settings = {"max_skills": 3, "max_chars": 60000}
    rules, used = [], []
    for f in sources:
        if not f or not Path(f).is_file():
            continue
        data = json.loads(Path(f).read_text(encoding="utf-8-sig"))
        used.append(str(f))
        for k in ("max_skills", "max_chars"):
            if k in data:
                settings[k] = int(data[k])
        for rid in data.get("disable", []):
            rules = [r for r in rules if r.get("id") != rid]
        for r in data.get("rules", []):
            r = dict(r, _source=str(f))
            idx = next((i for i, x in enumerate(rules) if x.get("id") and x.get("id") == r.get("id")), None)
            if idx is None:
                rules.append(r)
            else:
                rules[idx] = r
    return settings, rules, used


# ------------------------------------------------------------ inventory ----
def _frontmatter(path):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}, ""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("\n")
        for i in range(1, len(parts)):
            if parts[i].strip() == "---":
                for line in parts[1:i]:
                    m = re.match(r"^([A-Za-z_-]+):\s*(.*)$", line)
                    if m:
                        meta[m.group(1)] = m.group(2).strip().strip("'\"")
                body = "\n".join(parts[i + 1:]).lstrip("\n")
                break
    return meta, body


def _skill_files(root, depth=3):
    root = Path(root)
    if not root.is_dir():
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        rel = Path(dirpath).relative_to(root).parts
        if len(rel) >= depth:
            dirnames[:] = []          # do not descend further
        if "SKILL.md" in filenames and len(rel) <= depth:
            out.append(Path(dirpath) / "SKILL.md")
    return sorted(out)


def inventory():
    """name -> path of SKILL.md. Precedence: personal > project > plugins."""
    found = {}

    def put(name, path):
        if name and name not in found:
            found[name] = Path(path)

    home = Path.home() / ".claude"
    for f in _skill_files(home / "skills"):
        meta, _ = _frontmatter(f)
        n = meta.get("name") or f.parent.name
        put(n, f)
        if "synced" in f.relative_to(home / "skills").parts[:1] and ":" not in n:
            put(f"anthropic-skills:{n}", f)   # how the Claude app lists synced account skills

    d = Path.cwd().resolve()
    for p in [d, *d.parents]:
        for f in _skill_files(p / ".claude" / "skills"):
            meta, _ = _frontmatter(f)
            put(meta.get("name") or f.parent.name, f)

    def plugin_skills(plugin, root):
        for f in _skill_files(Path(root) / "skills", depth=1):
            meta, _ = _frontmatter(f)
            n = meta.get("name") or f.parent.name
            put(n if ":" in n else f"{plugin}:{n}", f)

    plugin_skills(PLUGIN_NAME, PLUGIN_ROOT)
    ip = home / "plugins" / "installed_plugins.json"
    if ip.is_file():
        try:
            data = json.loads(ip.read_text(encoding="utf-8-sig"))
            for key in sorted(data.get("plugins", {})):
                entries = data["plugins"][key]
                entry = entries[0] if isinstance(entries, list) and entries else entries
                path = (entry or {}).get("installPath")
                if path:
                    plugin_skills(key.split("@")[0], path)
        except (ValueError, OSError, AttributeError):
            pass

    # Claude app (cloud) layout: ~/.claude/plugins/synced/<bucket>/<plugin-dir>/
    synced = home / "plugins" / "synced"
    if synced.is_dir():
        for manifest in sorted(synced.glob("*/*/.claude-plugin/plugin.json")):
            try:
                pname = json.loads(manifest.read_text(encoding="utf-8-sig")).get("name")
            except (ValueError, OSError):
                continue
            if pname:
                plugin_skills(pname, manifest.parent.parent)
    return found


# -------------------------------------------------------------- matching ---
def _norm_paths(paths):
    return [str(p).replace("\\", "/").lower() for p in (paths or [])]


def _kw(text, kw):
    return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", text) is not None


def _cond(spec, task):
    """True if every group present in spec matches (any-of within a group)."""
    role = str(task.get("role", "")).split(":")[-1].lower()
    kind = str(task.get("kind", "code")).lower()
    paths = _norm_paths(task.get("paths"))
    text = str(task.get("text", "")).lower()
    flags = task.get("flags") or {}
    why = []
    for g in GROUPS:
        vals = spec.get(g)
        if not vals:
            continue
        hit = None
        if g == "roles":
            hit = next((v for v in vals if v.lower() == role), None)
        elif g == "kinds":
            hit = next((v for v in vals if v.lower() == kind), None)
        elif g == "globs":
            for pat in vals:
                pl = pat.lower()
                m = next((p for p in paths if fnmatch.fnmatchcase(p, pl)
                          or fnmatch.fnmatchcase(p.rsplit("/", 1)[-1], pl)), None)
                if m:
                    hit = f"{pat} -> {m}"
                    break
        elif g == "keywords":
            hit = next((v for v in vals if _kw(text, v)), None)
        elif g == "all_keywords":
            hit = ",".join(vals) if all(_kw(text, v) for v in vals) else None
        elif g == "flags":
            hit = next((v for v in vals if flags.get(v)), None)
        if hit is None:
            return False, []
        why.append(f"{g[:-1] if g.endswith('s') else g}:{hit}")
    return True, why


def _valid(rule):
    return bool(rule.get("skill")) and any(rule.get("when", {}).get(g) for g in GROUPS)


def select(task, settings, rules, inv):
    picked = {}  # skill -> (priority, order, reasons)
    for order, r in enumerate(rules):
        if not _valid(r):
            continue
        ok, why = _cond(r["when"], task)
        if not ok:
            continue
        if r.get("unless"):
            bad, _ = _cond(r["unless"], task)
            if bad:
                continue
        pri = int(r.get("priority", 50))
        cur = picked.get(r["skill"])
        if cur is None or pri > cur[0]:
            picked[r["skill"]] = (pri, order, [f"rule {r.get('id', order)}"] + why, r.get("group"))
    for n in task.get("add", []) or []:
        picked[n] = (1000, -1, ["added explicitly by coordinator"], None)
    for n in task.get("exclude", []) or []:
        picked.pop(n, None)

    ranked = sorted(picked.items(), key=lambda kv: (-kv[1][0], kv[1][1], kv[0]))
    chosen, dropped, unresolved, total, groups = [], [], [], 0, set()
    for name, (pri, _, why, group) in ranked:
        path = inv.get(name)
        if path is None:
            unresolved.append(name)
            continue
        if group and group in groups:
            dropped.append({"skill": name, "reason": f"group '{group}' already filled"})
            continue
        _, body = _frontmatter(path)
        size = len(body)
        if len(chosen) >= settings["max_skills"]:
            dropped.append({"skill": name, "reason": "over max_skills"})
        elif total + size > settings["max_chars"]:
            dropped.append({"skill": name, "reason": f"over max_chars ({size} chars)"})
        else:
            total += size
            if group:
                groups.add(group)
            chosen.append({"skill": name, "priority": pri, "chars": size,
                           "path": str(path), "why": why})
    return {"skills": chosen, "dropped": dropped, "unresolved": unresolved,
            "total_chars": total, "settings": settings}


def inject(result):
    if not result["skills"]:
        return "## Skills for this task\nNone selected. Work from the task card alone.\n"
    out = ["## Skills for this task",
           "Selected deterministically by skill-selector. These are the only skills you "
           "have; the Skill tool is disabled. Files a skill mentions are relative to its "
           "base directory; open them with Read.", ""]
    for s in result["skills"]:
        _, body = _frontmatter(s["path"])
        out += [f"### Skill: {s['skill']}",
                f"Base directory: {Path(s['path']).parent}",
                f"Selected because: {'; '.join(s['why'])}", "", body.rstrip(), ""]
    return "\n".join(out) + "\n"


def write_out(result, out):
    """Write the inject block to `out`; return the short Task Card line.

    Nothing is written when no skill was selected. A file under a .feature-ladder
    folder is git-ignored there (same mechanism as the other run files)."""
    out = Path(out).expanduser()
    if not result["skills"]:
        return "## Skills for this task\nNone selected. Work from the task card alone.\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(inject(result), encoding="utf-8")
    _ignore_in_feature_ladder(out)
    names = ", ".join(s["skill"] for s in result["skills"])
    return ("## Skills for this task\n"
            f"Read `{out.resolve()}` before you start. It holds the only skills you have "
            f"({names}; {result['total_chars']:,} chars), selected deterministically by "
            "skill-selector. The Skill tool is disabled; if you need another skill, return "
            "`blocked` naming it.\n")


def _ignore_in_feature_ladder(path):
    path = path.resolve()
    for d in path.parents:
        if d.name == ".feature-ladder":
            try:
                rel = path.relative_to(d).parts[0]
                entry = rel + "/" if len(path.relative_to(d).parts) > 1 else rel
                gi = d / ".gitignore"
                lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
                if entry not in lines:
                    gi.write_text("\n".join(lines + [entry]) + "\n", encoding="utf-8")
            except OSError:
                pass
            return


def list_all(settings, rules, used, inv):
    print(f"Rule files: {', '.join(used) or 'none'}")
    print(f"Settings: {settings}")
    print(f"Resolvable skills ({len(inv)}):")
    for n in sorted(inv):
        print(f"  {n}")
    problems = []
    for i, r in enumerate(rules):
        if not _valid(r):
            problems.append(f"rule {r.get('id', i)}: needs 'skill' and at least one 'when' group")
        elif r["skill"] not in inv:
            problems.append(f"rule {r.get('id', i)}: skill '{r['skill']}' is not installed")
    print(f"Rule problems ({len(problems)}):")
    for p in problems:
        print(f"  {p}")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    settings, rules, used = load_rules()
    inv = inventory()
    if argv[0] == "--list":
        list_all(settings, rules, used, inv)
        return 0
    want_inject = "--inject" in argv
    out = None
    args = []
    it = iter(argv)
    for a in it:
        if a == "--inject":
            continue
        if a == "--out":
            out = next(it, None)
            if not out:
                print("--out needs a path", file=sys.stderr)
                return 2
            continue
        args.append(a)
    if args and args[0] == "--file":
        task = json.loads(Path(args[1]).read_text(encoding="utf-8-sig"))
    else:
        task = json.loads(args[0])
    result = select(task, settings, rules, inv)
    if out:
        sys.stdout.write(write_out(result, out))
    elif want_inject:
        sys.stdout.write(inject(result))
    else:
        result["rule_files"] = used
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
