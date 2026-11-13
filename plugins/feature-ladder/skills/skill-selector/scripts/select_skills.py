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
  python3 select_skills.py --scan [--out PATH] [--check]
                                                    rescan every installed skill into a catalog
                                                    (default ~/.claude/feature-ladder/skill-catalog.json)
                                                    and report: rules whose skill is not installed,
                                                    installed skills no rule reaches, and what was
                                                    added, removed or edited since the last scan.
                                                    --check writes nothing and exits 1 on any of
                                                    the first or the last.

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

Budgets: at most max_skills skills and max_chars characters of skill text per
task; max_chars_by_role (keyed by router role) overrides max_chars for that
role, so a mechanic gets no skill text and a senior implementer gets more than
an implementer. Anything cut is reported, never silently included.

Rules are merged from, in order (later wins on the same rule id):
  1. <plugin>/skills/skill-selector/skill-rules.json   (shipped defaults)
  2. ~/.claude/feature-ladder/skill-rules.json          (your personal rules)
  3. <repo>/.feature-ladder/skill-rules.json            (per-project rules)
"""
import fnmatch
import hashlib
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
    settings = {"max_skills": 3, "max_chars": 20000, "max_chars_by_role": {}}
    rules, used = [], []
    for f in sources:
        if not f or not Path(f).is_file():
            continue
        data = json.loads(Path(f).read_text(encoding="utf-8-sig"))
        used.append(str(f))
        for k in ("max_skills", "max_chars"):
            if k in data:
                settings[k] = int(data[k])
        for role, v in (data.get("max_chars_by_role") or {}).items():
            settings["max_chars_by_role"][str(role).lower()] = int(v)
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
def _parse_meta(lines):
    """Top-level `key: value` pairs of a SKILL.md frontmatter. A YAML block
    scalar (`>`, `|`, with or without `-`/`+`) or a value continued on indented
    lines is folded into one line, so a multi-line description is read whole."""
    meta, key = {}, None
    for line in lines:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            meta[key] = "" if re.fullmatch(r"[>|][+-]?\d*", val) else val
        elif key is not None and line[:1] in (" ", "\t") and line.strip():
            meta[key] = (meta[key] + " " + line.strip()).strip()
        elif line.strip() and not line.lstrip().startswith("#"):
            key = None
    return {k: v.strip().strip("'\"") for k, v in meta.items()}


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
                meta = _parse_meta(parts[1:i])
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


def _plugin_name(root):
    """The plugin's own name from its manifest (what Claude Code prefixes its
    skills with), or None."""
    try:
        data = json.loads((Path(root) / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8-sig"))
        return str(data.get("name") or "").strip() or None
    except (ValueError, OSError, AttributeError):
        return None


def inventory_entries():
    """[(name, path of SKILL.md, source)] in a fixed order; the first entry for
    a name wins. Precedence: personal > project > plugins. Sources: personal,
    account (synced from the Claude account), project, plugin:<name>."""
    seen, out = set(), []

    def put(name, path, source):
        if name and name not in seen:
            seen.add(name)
            out.append((name, Path(path), source))

    home = Path.home() / ".claude"
    for f in _skill_files(home / "skills"):
        meta, _ = _frontmatter(f)
        n = meta.get("name") or f.parent.name
        synced = "synced" in f.relative_to(home / "skills").parts[:1]
        put(n, f, "account" if synced else "personal")
        if synced and ":" not in n:
            put(f"anthropic-skills:{n}", f, "account")   # how the Claude app lists synced account skills

    d = Path.cwd().resolve()
    for p in [d, *d.parents]:
        for f in _skill_files(p / ".claude" / "skills"):
            meta, _ = _frontmatter(f)
            put(meta.get("name") or f.parent.name, f, "project")

    def plugin_skills(plugin, root):
        for f in _skill_files(Path(root) / "skills", depth=1):
            meta, _ = _frontmatter(f)
            n = meta.get("name") or f.parent.name
            put(n if ":" in n else f"{plugin}:{n}", f, f"plugin:{plugin}")

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
                    # Both names resolve: the marketplace key (data-agent-kit) and the
                    # manifest name Claude Code shows the skills under (dak).
                    for pname in dict.fromkeys([key.split("@")[0], _plugin_name(path)]):
                        if pname:
                            plugin_skills(pname, path)
        except (ValueError, OSError, AttributeError):
            pass

    # Claude app (cloud) layout: ~/.claude/plugins/synced/<bucket>/<plugin-dir>/
    synced = home / "plugins" / "synced"
    if synced.is_dir():
        for manifest in sorted(synced.glob("*/*/.claude-plugin/plugin.json")):
            pname = _plugin_name(manifest.parent.parent)
            if pname:
                plugin_skills(pname, manifest.parent.parent)
    return out


def inventory():
    """name -> path of SKILL.md (see inventory_entries for the precedence)."""
    return {name: path for name, path, _ in inventory_entries()}


# ----------------------------------------------------------------- scan ----
CATALOG = Path.home() / ".claude" / "feature-ladder" / "skill-catalog.json"


def build_catalog(entries, rules):
    """Every installed skill, one record per SKILL.md file, in name order.
    Same installed skills + same rules -> the same catalog, byte for byte
    (no timestamps; the digest leaves machine paths out)."""
    by_path = {}
    for name, path, source in entries:
        by_path.setdefault(str(path), {"names": [], "source": source})["names"].append(name)
    ruled = {}
    for i, r in enumerate(rules):
        if _valid(r):
            ruled.setdefault(r["skill"], []).append(str(r.get("id", i)))
    known = {name for name, _, _ in entries}
    skills = []
    for path, e in by_path.items():
        meta, body = _frontmatter(path)
        ids = sorted({i for n in e["names"] for i in ruled.get(n, [])})
        skills.append({"name": e["names"][0], "aliases": e["names"][1:], "source": e["source"],
                       "path": path, "chars": len(body),
                       "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest()[:16],
                       "description": " ".join((meta.get("description") or "").split())[:200],
                       "rules": ids})
    skills.sort(key=lambda k: k["name"])
    missing = sorted(({"rule": i, "skill": sk} for sk, ids in ruled.items() if sk not in known for i in ids),
                     key=lambda m: (m["skill"], m["rule"]))
    digest = hashlib.sha256(json.dumps(
        [[k["name"], k["aliases"], k["chars"], k["sha256"]] for k in skills]).encode("utf-8")).hexdigest()[:16]
    return {"version": 1, "digest": digest, "count": len(skills), "skills": skills,
            "rules_missing_skill": missing,
            "unreached": [k["name"] for k in skills if not k["rules"]]}


def catalog_changes(old, new):
    """What differs between two catalogs: skills added, removed, or edited."""
    a = {k["name"]: k["sha256"] for k in (old or {}).get("skills", [])}
    b = {k["name"]: k["sha256"] for k in new["skills"]}
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "changed": sorted(n for n in set(a) & set(b) if a[n] != b[n])}


def scan(rules, used, out=None, check=False):
    """Rescan and report. Returns (report dict, exit code)."""
    out = Path(out).expanduser() if out else CATALOG
    cat = build_catalog(inventory_entries(), rules)
    try:
        old = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    except (ValueError, OSError):
        old = None
    changes = catalog_changes(old, cat) if old else None
    drift = bool(changes and any(changes.values()))
    if not check:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(cat, indent=1) + "\n", encoding="utf-8")
        _ignore_in_feature_ladder(out)
    report = {"catalog": str(out), "written": not check, "digest": cat["digest"], "skills": cat["count"],
              "names": sum(1 + len(k["aliases"]) for k in cat["skills"]), "rule_files": used,
              "rules_missing_skill": cat["rules_missing_skill"],
              "unreached_count": len(cat["unreached"]), "unreached": cat["unreached"],
              "since_last_scan": changes if old else "no earlier catalog"}
    return report, (1 if check and (cat["rules_missing_skill"] or drift or old is None) else 0)


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


def budget(task, settings):
    """Character budget for this task's role: max_chars_by_role wins over max_chars."""
    role = str(task.get("role", "")).split(":")[-1].lower()
    by_role = settings.get("max_chars_by_role") or {}
    return int(by_role.get(role, settings["max_chars"]))


def select(task, settings, rules, inv):
    max_chars = budget(task, settings)
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
    chosen, dropped, unresolved, total, groups, files = [], [], [], 0, set(), {}
    for name, (pri, _, why, group) in ranked:
        path = inv.get(name)
        if path is None:
            unresolved.append(name)
            continue
        if str(path) in files:   # two names for one SKILL.md (docx, anthropic-skills:docx)
            dropped.append({"skill": name, "reason": f"same file as {files[str(path)]}"})
            continue
        if group and group in groups:
            dropped.append({"skill": name, "reason": f"group '{group}' already filled"})
            continue
        _, body = _frontmatter(path)
        size = len(body)
        if len(chosen) >= settings["max_skills"]:
            dropped.append({"skill": name, "reason": "over max_skills"})
        elif total + size > max_chars:
            dropped.append({"skill": name, "reason": f"over max_chars for this role ({size} chars, budget {max_chars})"})
        else:
            total += size
            files[str(path)] = name
            if group:
                groups.add(group)
            chosen.append({"skill": name, "priority": pri, "chars": size,
                           "path": str(path), "why": why})
    return {"skills": chosen, "dropped": dropped, "unresolved": unresolved,
            "total_chars": total, "max_chars": max_chars, "settings": settings}


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
    if argv[0] == "--scan":
        rest = argv[1:]
        out = rest[rest.index("--out") + 1] if "--out" in rest and rest.index("--out") + 1 < len(rest) else None
        report, code = scan(rules, used, out, "--check" in rest)
        print(json.dumps(report, indent=2))
        return code
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
