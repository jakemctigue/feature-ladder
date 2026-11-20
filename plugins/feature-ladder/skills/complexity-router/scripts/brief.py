#!/usr/bin/env python3
"""
feature-ladder research brief: what a requested feature wants from this
codebase, what the run would cost (qualitative labels only, never a number)
and what to expect, written next to the intake as
.feature-ladder/intake/<slug>-brief.md.

  build  [--proposal P.json] [--intake I.json] [--switches a,b] [--evidence FILE]
         [--external NAME] [--cwd DIR] [--out FILE] [--no-skills] [--no-bs]
         Builds the brief from the scoper's proposal and/or a confirmed intake,
         writes it and prints it. With only a proposal, the router runs on the
         proposal's values with ambiguity=1 and blast_radius=1 assumed. No
         confirmed intake is created. With --no-bs (or no-bs among
         --switches, or no_bs in the confirmed intake's switches) the
         cheap-tier labels say which Silk Compute model external_worker.py
         really runs under a --no-bs run (a per-run switch, never a saved
         setting).
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
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import intake  # noqa: E402
import route as router  # noqa: E402
from intake import _load, ladder_state, pop_opt  # noqa: E402  (shared with intake.py, not copied)

LABELS = ["low token cost", "medium token cost", "high token cost"]
MODEL_NAMES = {"haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus", "fable": "Fable"}
CLAUDE, OPENROUTER = "Claude plan", "OpenRouter credits"
ASSUMED = {"ambiguity": 1, "blast_radius": 1}

# --no-bs: a run started with --no-bs moves the cheap tiers to Silk Compute
# (external_worker.py, SILK). One small table here; the ids are asserted equal
# to that SILK table by test_research_brief.py. The flag is per run only --
# never read from or written to a saved setting or environment variable. The
# cost table must stay digit-free, so its rung text gets a version-less name.
SILK_MODELS = {
    "mechanic": "deepseek-v4.1-flash-1017-fast-uncensored",
    "implementer": "deepseek-v4.1-flash-1017-uncensored",
    "implementer-mid": "kimi-k3.1-pro-uncensored",
}
SILK_REACH = 14    # implementer/medium cards up to this score run on Silk
SILK_MID_FROM = 7  # implementer cards from this score get the mid tier
SILK_LABELS = {  # model id -> (What-to-expect label, digit-free cost-table rung)
    "deepseek-v4.1-flash-1017-fast-uncensored": ("DeepSeek V4.1 Flash Fast (Silk)", "DeepSeek Flash Fast (Silk)"),
    "deepseek-v4.1-flash-1017-uncensored": ("DeepSeek V4.1 Flash (Silk)", "DeepSeek Flash (Silk)"),
    "kimi-k3.1-pro-uncensored": ("Kimi K3.1 Pro (Silk)", "Kimi Pro (Silk)"),
}


def norm(text):
    return " ".join(str(text or "").split())


def _rank(model, effort, glm):
    """Base cost rank (0 low, 1 medium, 2 high) of a rung."""
    if glm or model == "haiku":
        return 0
    if model == "sonnet":
        return 1 if effort in (None, "low", "medium") else 2
    return 2


def orchestrator_rung(orchestrator=None):
    """'Opus xhigh' or 'Fable medium': model and effort, no version (this
    section carries no digits)."""
    o = orchestrator or {}
    return f"{MODEL_NAMES.get(o.get('model'), 'Opus')} {o.get('effort') or 'xhigh'}"


def silk_model(route, no_bs=False):
    """The Silk Compute model that runs this card under --no-bs, or None.
    Trusts the runner model the intake attached (external_worker.model_for's
    pick) when there is one; else this file's table: mechanic, implementer up
    to the GLM band and the intermediate band up to SILK_REACH (the same tiers
    external_worker.takes moves). implementer/high and every rung above keep
    their Claude rung."""
    if not no_bs:
        return None
    runner = (route.get("runner") or {}).get("model")
    if runner:
        return runner
    agent, effort = route.get("agent"), route.get("effort")
    s = route.get("score")
    if agent == "mechanic":
        return SILK_MODELS["mechanic"]
    if agent == "implementer" and effort != "high":
        if s is None:
            return SILK_MODELS["implementer"]  # external_worker.model_for does the same
        if s < SILK_MID_FROM:
            return SILK_MODELS["implementer"]
        if s <= SILK_REACH:
            return SILK_MODELS["implementer-mid"]
    return None


def cost_rows(route, budget, reviewer=False, evidence=False, orchestrator=None, no_bs=False):
    """[(agent, rung text, label, billing)] for the predicted plan. No numbers."""
    size = route.get("size", "S")
    glm = route.get("provider") == "glm"
    rows = [("scout", "Sonnet medium", LABELS[1], CLAUDE)]
    if evidence:
        rows.append(("evidence collector (one call, external library)", "Haiku medium", LABELS[0], CLAUDE))
    rows.append(("coordinator", orchestrator_rung(orchestrator), LABELS[2], CLAUDE))
    if route.get("design_first"):
        rows.append(("design step", "Opus medium", LABELS[2], CLAUDE))
    model, effort = route.get("model"), route.get("effort")
    rank = _rank(model, effort, glm)
    if size == "L":
        rank = min(2, rank + 1)
    silk = silk_model(route, no_bs)
    if silk:
        rung = SILK_LABELS.get(silk, ("", "Silk Compute"))[1]
    else:
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
    """One "- item" line each. An item that already is a list line ("- x",
    "* x", "1. x") keeps its text and loses its own marker."""
    items = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", str(i)).strip() for i in items if i]
    items = [i for i in items if i]
    return [f"- {i}" for i in items] if items else [f"- {empty}"]


def _top_modules(files):
    return sorted({f.split("/")[0] for f in files if "/" in f}) or ["(repo root)"]


def make_brief(data):
    """data: request, fields{name: value}, route, budget, candidate_files, test_command,
    secrets{names}, environments, open_decisions, decisions, switches, skills,
    external{name, notes}, reviewer, assumed, orchestrator{model, effort},
    no_bs (labels the cheap tiers with their Silk Compute models). Returns markdown."""
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
                     if k not in ("session_agents", "workspace", "orchestrator")]

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
    no_bs = bool(data.get("no_bs"))
    silk = silk_model(route, no_bs)
    lines.append(cost_section(cost_rows(route, budget, bool(data.get("reviewer")), bool(external),
                                        data.get("orchestrator"), no_bs)))
    model = MODEL_NAMES.get(route.get("model"), route.get("model"))
    if silk:
        rung = SILK_LABELS.get(silk, (f"{silk} (Silk)", ""))[0]
    else:
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
def _values(fields):
    if not isinstance(fields or {}, dict):
        raise ValueError('"fields" must be an object ({name: value or {value, confidence, evidence}})')
    return {k: (v.get("value") if isinstance(v, dict) else v) for k, v in (fields or {}).items()}


def data_from(prop, conf, switches, evidence=None, external=None, no_bs=False):
    prop, conf = prop or {}, conf or {}
    no_bs = no_bs or "no_bs" in (conf.get("switches") or [])  # the confirmed run asked for Silk
    if conf:
        fields = _values(conf.get("fields"))
        # An intake saved by hand or by an older version may lack the route or the budget.
        route = conf.get("route") or router.route(fields)
        budget = conf.get("budget") or intake.budget_for(route, fields)
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
        "orchestrator": conf.get("orchestrator") or intake.session_budget.orchestrator(
            intake.state_root(prop.get("repo_root") or conf.get("repo_root"))),
        "no_bs": bool(no_bs),
    }
    if external:
        data["external"] = {"name": external, "notes": evidence or ""}
    return data


def _skills(data):
    sel = HERE.parents[1] / "skill-selector" / "scripts" / "select_skills.py"
    r = data["route"]
    task = {"role": r["agent"], "kind": data["fields"].get("kind", "code"), "paths": data["candidate_files"][:8],
            "text": data.get("request", ""), "flags": intake.skill_flags(data["fields"])}
    try:
        res = subprocess.run([sys.executable, str(sel), json.dumps(task)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30)
        return [s["skill"] for s in json.loads(res.stdout).get("skills", [])] if res.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def find_brief(text, cwd):
    d = ladder_state.state_dir(intake.state_root(cwd)) / "intake"
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
        return pop_opt(rest, name, default)

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
            # Per run only (the flag, this run's --switches, or this run's confirmed
            # intake); never a saved setting or environment variable.
            no_bs = "--no-bs" in rest or "no_bs" in sw
            if not pp and not ip:
                print("brief build needs --proposal and/or --intake", file=sys.stderr)
                return 1
            evidence = Path(ev).read_text(encoding="utf-8", errors="replace") if ev else None
            prop, conf = (_load(pp) if pp else None), (_load(ip) if ip else None)
            data = data_from(prop, conf, sw, evidence, ext, no_bs)
            if not data.get("request"):
                print("brief build: no request text in the proposal or intake", file=sys.stderr)
                return 1
            if not skip_skills:
                data["skills"] = _skills(data)
            md = make_brief(data)
            if not out:
                root = intake.state_root((prop or conf).get("repo_root") or cwd)
                intake._ensure_ignore(root)
                out = str(ladder_state.state_dir(root) / "intake" / f"{intake.slugify(data['request'])}-brief.md")
            ladder_state.write_text(out, md)  # atomic, LF on every platform and Python version
            sys.stdout.write(md)
            return 0
    except (ValueError, KeyError, IndexError, OSError, TypeError, AttributeError) as e:
        kind = "" if isinstance(e, (ValueError, OSError)) else f"{type(e).__name__}: "
        print(f"brief {cmd}: {kind}{e}", file=sys.stderr)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
