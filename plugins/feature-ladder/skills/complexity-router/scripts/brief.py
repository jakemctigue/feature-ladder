#!/usr/bin/env python3
"""
feature-ladder research brief: what a requested feature wants from this
codebase, what the run would cost (qualitative labels only, never a number)
and what to expect, written next to the intake as
.feature-ladder/intake/<slug>-brief.md.

  build  [--proposal P.json] [--intake I.json] [--switches a,b] [--evidence FILE]
         [--external NAME] [--cwd DIR] [--out FILE] [--no-skills]
         Builds the brief from the scoper's proposal and/or a confirmed intake,
         writes it and prints it. With only a proposal, the router runs on the
         proposal's values with ambiguity=1 and blast_radius=1 assumed. No
         confirmed intake is created.
  find   "<request>" [--cwd DIR]
         Prints the path of the saved brief whose request text matches (same
         normalisation as intake.py find), then the matching proposal path if
         one is saved. Exit 1 when there is none.
  show   [PATH | "<request>"] [--cwd DIR]
         Prints a saved brief.

make_brief(data) is a pure function (dict in, markdown out). The cost section
holds no digits: rungs are described by model name and effort only.
Exit 0 on success, 1 on bad input or no match.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import intake  # noqa: E402
import route as router  # noqa: E402

LABELS = ["low token cost", "medium token cost", "high token cost"]
MODEL_NAMES = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus", "fable": "Fable"}
CLAUDE, OPENROUTER = "Claude plan", "OpenRouter credits"
ASSUMED = {"ambiguity": 1, "blast_radius": 1}


def norm(text):
    return " ".join(str(text or "").split())


def _rank(model, effort, glm):
    """Base cost rank (0 low, 1 medium, 2 high) of a rung."""
    if glm or model == "haiku":
        return 0
    if model == "sonnet":
        return 1 if effort in (None, "low", "medium") else 2
    return 2


def cost_rows(route, budget, reviewer=False, evidence=False):
    """[(agent, rung text, label, billing)] for the predicted plan. No numbers."""
    size = route.get("size", "S")
    glm = route.get("provider") == "glm"
    rows = [("scout", "Sonnet medium", LABELS[1], CLAUDE)]
    if evidence:
        rows.append(("evidence collector (one call, external library)", "Haiku medium", LABELS[0], CLAUDE))
    rows.append(("coordinator", "Opus medium", LABELS[2], CLAUDE))
    if route.get("design_first"):
        rows.append(("design step", "Opus medium", LABELS[2], CLAUDE))
    model, effort = route.get("model"), route.get("effort")
    rank = _rank(model, effort, glm)
    if size == "L":
        rank = min(2, rank + 1)
    rung = "GLM Flash" if glm else f"{MODEL_NAMES.get(model, str(model).title())} {effort}"
    rows.append((f"worker ({route.get('agent')})", rung, LABELS[rank], OPENROUTER if glm else CLAUDE))
    if int((budget or {}).get("workers", 1) or 1) > 1:
        rows.append(("integrator (only if two or more agents write code)", "Fable medium", LABELS[2], CLAUDE))
    if reviewer:
        rows.append(("reviewer (scheduled at wrap)", "Haiku low driver for the Codex CLI", LABELS[0],
                     "Claude plan for the driver; Codex runs on your own Codex sign-in"))
    return rows


def cost_section(rows):
    out = ["## What it would cost", "",
           "Qualitative only. Labels come from rung, effort and size.", "",
           "| Agent | Rung | Cost | Bills to |", "|---|---|---|---|"]
    out += [f"| {a} | {r} | {c} | {b} |" for a, r, c, b in rows]
    return "\n".join(out)


def _bullets(items, empty):
    items = [i for i in items if i]
    return [f"- {i}" for i in items] if items else [f"- {empty}"]


def _top_modules(files):
    return sorted({f.split("/")[0] for f in files if "/" in f}) or ["(repo root)"]


def make_brief(data):
    """data: request, fields{name: value}, route, budget, candidate_files, test_command,
    secrets{names}, environments, open_decisions, decisions, switches, skills,
    external{name, notes}, reviewer, assumed. Returns markdown."""
    fields = data.get("fields") or {}
    route = data.get("route") or router.route(fields)
    budget = data.get("budget") or intake.budget_for(route, fields)
    files = list(data.get("candidate_files") or [])
    tests = [f for f in files if intake.is_test(f)]
    external = data.get("external") or {}
    switches = sorted(set(data.get("switches") or []))
    names = (data.get("secrets") or {}).get("names") or []
    prod = (data.get("environments") or {}).get("production") or []

    outside = []
    if fields.get("irreversible"):
        outside.append("a step that reaches outside the repo (deploy, publish, data or external call); "
                       "the audit hook logs it, and it is labelled review: pending when it touches production")
        if prod:
            outside.append("production targets the hook would see: " + ", ".join(prod))
    if fields.get("cross_system"):
        outside.append("cross-system call detected in the request or code")

    decisions = []
    for d in data.get("open_decisions") or []:
        opts = [o if isinstance(o, str) else o.get("label") for o in d.get("options") or []]
        decisions.append(f"{d.get('question')} (options: {'; '.join(str(o) for o in opts)})" if opts
                         else str(d.get("question")))
    if not decisions:
        decisions = [f"{k}: {v}" for k, v in (data.get("decisions") or {}).items()
                     if k not in ("session_agents", "workspace")]

    lines = [f"<!-- request: {norm(data.get('request'))} -->",
             f"# Research brief: {norm(data.get('request'))}", "",
             "## What the feature wants from the codebase", "",
             "Files and modules it will touch:"]
    lines += _bullets(files[:8], "no candidate files matched; the request may add new files")
    lines += ["Modules: " + ", ".join(_top_modules(files)), "", "Tests and test command:"]
    lines += _bullets(tests[:4], "no test file near this area")
    lines.append(f"- test command: `{data.get('test_command')}`" if data.get("test_command")
                 else "- test command: none detected")
    lines += ["", "Secrets (names only):"] + _bullets(names, "none found")
    lines += ["", "Outside-repo steps the audit hook would log:"] + _bullets(outside, "none expected")
    lines += ["", "Open design decisions:"] + _bullets(decisions, "none raised")
    if data.get("skills"):
        lines += ["", "Skills the worker would get: " + ", ".join(data["skills"])]
    if external:
        lines += ["", f"External library or API the repo does not use: {external.get('name')}"]
        if external.get("notes"):
            lines += _bullets(str(external["notes"]).strip().splitlines()[:8], "")
    lines.append("")
    lines.append(cost_section(cost_rows(route, budget, bool(data.get("reviewer")), bool(external))))
    model = MODEL_NAMES.get(route.get("model"), route.get("model"))
    rung = (f"GLM Flash worker (falls back to {model} {route.get('effort')} if the GLM tier is off)"
            if route.get("provider") == "glm" else f"{model} {route.get('effort')} ({route.get('agent')})")
    test_cmd = data.get("test_command")
    lines += ["", "## What to expect", "",
              f"- Predicted rung: {rung}; size {route.get('size')}",
              f"- Plan: {budget.get('shape')}",
              "- Checks that gate acceptance: "
              + (f"`{test_cmd}` plus the card's Done-when commands, re-run by verify.py" if test_cmd
                 else "the card's Done-when commands, re-run by verify.py"),
              "- Labelled review: pending: the merged PRs, any deploy and any data step",
              "- Switches that apply: " + (", ".join("--" + s.replace("_", "-") for s in switches)
                                          if switches else "none")]
    if data.get("assumed"):
        lines.append("- Assumed without asking: " + data["assumed"])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI side --
def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _values(fields):
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (fields or {}).items()}


def data_from(prop, conf, switches, evidence=None, external=None):
    prop, conf = prop or {}, conf or {}
    if conf:
        fields, route, budget = conf.get("fields") or {}, conf.get("route"), conf.get("budget")
        assumed = None
    else:
        fields = _values(prop.get("fields"))
        fields.update(ASSUMED)
        route = router.route(fields)
        budget = intake.budget_for(route, fields)
        assumed = "ambiguity and blast radius, plus every low-confidence field; no questions were asked"
    repo = prop.get("repo") or {}
    data = {
        "request": conf.get("request") or prop.get("request"),
        "fields": fields, "route": route, "budget": budget,
        "candidate_files": conf.get("candidate_files") or prop.get("candidate_files") or [],
        "test_command": conf.get("test_command") or repo.get("test_command"),
        "secrets": conf.get("secrets") or prop.get("secrets") or {},
        "environments": conf.get("environments") or prop.get("environments") or {},
        "open_decisions": prop.get("open_decisions") or [],
        "decisions": conf.get("decisions") or {},
        "switches": switches, "assumed": assumed,
        "reviewer": bool((conf.get("codex") or {}).get("at_wrap")),
    }
    if external:
        data["external"] = {"name": external, "notes": evidence or ""}
    return data


def _skills(data):
    sel = HERE.parents[1] / "skill-selector" / "scripts" / "select_skills.py"
    r = data["route"]
    task = {"role": r["agent"], "kind": data["fields"].get("kind", "code"), "paths": data["candidate_files"][:8],
            "text": data.get("request", ""),
            "flags": {k: True for k in ("security", "persistence", "concurrency", "cross_system", "public_api",
                                        "irreversible", "new_dependency") if data["fields"].get(k)}}
    try:
        res = subprocess.run([sys.executable, str(sel), json.dumps(task)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        return [s["skill"] for s in json.loads(res.stdout).get("skills", [])] if res.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def find_brief(text, cwd):
    root, _ = intake.repo_root(cwd)
    d = root / ".feature-ladder" / "intake"
    if not norm(text) or not d.is_dir():
        return None
    for f in sorted(d.glob("*-brief.md")):
        first = f.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        if first and first[0] == f"<!-- request: {norm(text)} -->":
            prop = f.with_name(f.name[:-len("-brief.md")] + "-proposal.json")
            return f, (prop if prop.is_file() else None)
    return None


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], list(argv[1:])

    def opt(name, default=None):
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1] if i + 1 < len(rest) else default
            del rest[i:i + 2]
            return val
        return default

    try:
        cwd = opt("--cwd", os.getcwd())
        if cmd == "find":
            hit = find_brief(" ".join(rest), cwd)
            if not hit:
                return 1
            print(hit[0])
            if hit[1]:
                print(hit[1])
            return 0
        if cmd == "show":
            arg = " ".join(rest)
            path = Path(arg) if arg.endswith(".md") and Path(arg).is_file() else None
            if path is None:
                hit = find_brief(arg, cwd)
                path = hit[0] if hit else None
            if path is None:
                print("brief show: no saved brief", file=sys.stderr)
                return 1
            sys.stdout.write(path.read_text(encoding="utf-8"))
            return 0
        if cmd == "build":
            pp, ip, out = opt("--proposal"), opt("--intake"), opt("--out")
            sw = [s.strip().lstrip("-").replace("-", "_") for s in (opt("--switches", "") or "").split(",") if s.strip()]
            ev, ext = opt("--evidence"), opt("--external")
            skip_skills = "--no-skills" in rest
            if not pp and not ip:
                print("brief build needs --proposal and/or --intake", file=sys.stderr)
                return 1
            evidence = Path(ev).read_text(encoding="utf-8", errors="replace") if ev else None
            prop, conf = (_load(pp) if pp else None), (_load(ip) if ip else None)
            data = data_from(prop, conf, sw, evidence, ext)
            if not data.get("request"):
                print("brief build: no request text in the proposal or intake", file=sys.stderr)
                return 1
            if not skip_skills:
                data["skills"] = _skills(data)
            md = make_brief(data)
            if not out:
                root = (prop or conf).get("repo_root") or cwd
                intake._ensure_ignore(root)
                out = str(Path(root) / ".feature-ladder" / "intake" / f"{intake.slugify(data['request'])}-brief.md")
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(md, encoding="utf-8", newline="\n")
            sys.stdout.write(md)
            return 0
    except (ValueError, KeyError, IndexError, OSError) as e:
        print(f"brief {cmd}: {e}", file=sys.stderr)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
