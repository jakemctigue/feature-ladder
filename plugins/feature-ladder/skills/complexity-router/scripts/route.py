#!/usr/bin/env python3
"""
Deterministic complexity router for the feature-ladder plugin.

Given a structured description of ONE coding task, returns the rung of the
ladder that should execute it (agent name, model alias, effort level) plus a
score breakdown. Same input -> same output, always. No model is consulted.

Usage
-----
  # structured (preferred) - fields the coordinator fills in
  python3 route.py '{"files": 3, "loc": 120, "kind": "code", ...}'
  python3 route.py --file task.json

  # quick keyword pre-fill from free text (coordinator should still review flags)
  python3 route.py --describe "add a retry wrapper around the S3 upload with backoff"

  # print the scoring table
  python3 route.py --table

Input fields (all optional; missing = 0 / false)
------------------------------------------------
  kind              "code" | "docs" | "fixture" | "check" | "research" | "analysis"
  files             int   files expected to change
  loc               int   lines expected to change (net adds+edits)
  modules           int   distinct modules/packages touched
  new_dependency    bool  adds or upgrades a third-party dependency
  public_api        bool  changes a public/exported interface or CLI surface
  persistence       bool  touches schema, migrations, stored formats, on-disk layouts
  security          bool  touches auth, secrets, permissions, crypto, input trust boundary
  concurrency       bool  async/threads/locks/queues/race-prone code
  cross_system      bool  spans >1 service, repo, process, or external system
  irreversible      bool  effects outside the repo (real data, deploys, publishing, external calls); +3, never run by the ladder
  no_tests          bool  area has no existing tests to lean on
  ambiguity         0-3   0 fully specified ... 3 goal only, approach unknown
  novelty           0-3   0 pattern exists in repo ... 3 nothing like it anywhere here
  blast_radius      0-3   0 isolated ... 3 whole app / customers affected if wrong
  design_agreed     bool  an accepted design/spec exists for this task (required for Opus rungs)

Output: JSON {rung, agent, model, effort, score, hard_flags, breakdown, reason}
"""
import json
import sys

# ---- Scoring table (edit here; keep it monotonic) ---------------------------
POINTS = {
    "files":        lambda v: 0 if v <= 1 else 1 if v <= 3 else 2 if v <= 6 else 4,
    "loc":          lambda v: 0 if v <= 30 else 1 if v <= 120 else 2 if v <= 300 else 4,
    "modules":      lambda v: 0 if v <= 1 else 1 if v == 2 else 3,
    "new_dependency": lambda v: 2 if v else 0,
    "public_api":   lambda v: 2 if v else 0,
    "persistence":  lambda v: 3 if v else 0,
    "security":     lambda v: 3 if v else 0,
    "concurrency":  lambda v: 3 if v else 0,
    "cross_system": lambda v: 3 if v else 0,
    "no_tests":     lambda v: 1 if v else 0,
    # Effects outside the repo (real data, deploys, publishing, external calls).
    # Code on a branch is always reversible, so this is a risk weight, not a stop:
    # the step is written but never run, and is parked in a checkpoint PR.
    "irreversible": lambda v: 3 if v else 0,
    "ambiguity":    lambda v: [0, 1, 3, 5][max(0, min(3, int(v)))],
    "novelty":      lambda v: [0, 1, 2, 4][max(0, min(3, int(v)))],
    "blast_radius": lambda v: [0, 1, 3, 5][max(0, min(3, int(v)))],
}

# Rungs are chosen by score band. "escalate" means: hand to the coordinator
# (Fable 5.1 Medium), who decides the Fable rung or re-scopes the task.
BANDS = [
    (0,  2,  "mechanic",            "haiku",  "low",    "T0 Haiku Low - mechanical patch"),
    (3,  6,  "implementer",         "sonnet", "medium", "T2 Sonnet Medium - bounded coding"),
    (7,  9,  "implementer",         "sonnet", "high",   "T3 Sonnet High - bounded coding, harder"),
    (10, 13, "senior-implementer",  "opus",   "medium", "T4 Opus Medium - difficult implementation"),
    (14, 17, "senior-implementer",  "opus",   "high",   "T5 Opus High - difficult implementation"),
    (18, 10**6, "escalate",         "fable",  "medium", "ESCALATE - coordinator decides (Fable rung or re-scope)"),
]

# Any of these forces escalation regardless of score.
HARD_FLAGS = {
    "ambiguity>=2": "approach not specified (ambiguity >= 2)",
    "cross_system+security": "cross-system AND security-sensitive",
    "blast_radius=3": "whole-app blast radius",
}

NON_CODE_KINDS = {
    "docs":     ("mechanic",           "haiku", "low",    "T0 Haiku Low - docs draft"),
    "fixture":  ("mechanic",           "haiku", "low",    "T0 Haiku Low - fixtures"),
    "check":    ("mechanic",           "haiku", "low",    "T0 Haiku Low - execute defined checks"),
    "research": ("evidence-collector", "haiku", "medium", "T1 Haiku Medium - collect sources / prepare evidence"),
    "analysis": ("domain-expert",      "opus",  "medium", "T4 Opus Medium - domain analysis (use domain-expert-high if novelty>=2)"),
}

KEYWORDS = {
    "security":     ["auth", "login", "password", "secret", "token", "permission", "crypto", "oauth", "jwt", "csrf", "xss", "sanitize"],
    "persistence":  ["schema", "migration", "database", "db ", "sql", "table", "index", "on-disk", "file format", "serializ"],
    "concurrency":  ["async", "thread", "lock", "mutex", "race", "queue", "worker", "concurren", "parallel", "websocket"],
    "cross_system": ["service", "microservice", "api call", "webhook", "another repo", "cross-repo", "integration with", "third-party", "external"],
    "irreversible": ["delete", "drop ", "truncate", "prod ", "production", "destroy", "purge", "wipe"],
    "public_api":   ["public api", "endpoint", "cli flag", "exported", "breaking", "interface change"],
    "new_dependency": ["add dependency", "install ", "npm i", "pip install", "new library", "upgrade "],
}


def score(task: dict):
    breakdown = {}
    for k, fn in POINTS.items():
        v = task.get(k, 0)
        pts = fn(v)
        if pts:
            breakdown[k] = pts
    total = sum(breakdown.values())

    flags = []
    if int(task.get("ambiguity", 0)) >= 2:
        flags.append(HARD_FLAGS["ambiguity>=2"])
    if task.get("cross_system") and task.get("security"):
        flags.append(HARD_FLAGS["cross_system+security"])
    if int(task.get("blast_radius", 0)) >= 3:
        flags.append(HARD_FLAGS["blast_radius=3"])
    return total, breakdown, flags


def route(task: dict) -> dict:
    kind = (task.get("kind") or "code").lower()
    total, breakdown, flags = score(task)

    if kind in NON_CODE_KINDS and not flags:
        agent, model, effort, label = NON_CODE_KINDS[kind]
        if kind == "analysis" and int(task.get("novelty", 0)) >= 2:
            agent, effort, label = "domain-expert-high", "high", "T5 Opus High - domain analysis, novel area"
        return _out(label, agent, model, effort, total, flags, breakdown,
                    f"kind={kind} routes by kind, not score")

    if flags:
        return _out(BANDS[-1][5], "escalate", "fable", "medium", total, flags, breakdown,
                    "hard flag present: " + "; ".join(flags))

    for lo, hi, agent, model, effort, label in BANDS:
        if lo <= total <= hi:
            reason = f"score {total} in band {lo}-{hi}"
            # Opus rungs require an agreed design; otherwise bounce to coordinator.
            if model == "opus" and not task.get("design_agreed", False):
                return _out("ESCALATE - Opus rung needs an agreed design first", "escalate",
                            "fable", "medium", total, flags, breakdown,
                            reason + "; design_agreed=false so coordinator must settle design (domain-expert) before implementation")
            return _out(label, agent, model, effort, total, flags, breakdown, reason)
    raise RuntimeError("unreachable")


def _out(label, agent, model, effort, total, flags, breakdown, reason):
    return {
        "rung": label,
        "agent": agent,
        "model": model,
        "effort": effort,
        "score": total,
        "hard_flags": flags,
        "breakdown": breakdown,
        "reason": reason,
    }


def describe(text: str) -> dict:
    """Keyword pre-fill. The coordinator must review the result; this only sets flags."""
    t = text.lower()
    task = {"kind": "code", "_prefilled_from": text}
    for flag, words in KEYWORDS.items():
        if any(w in t for w in words):
            task[flag] = True
    task["ambiguity"] = 2 if len(t.split()) < 8 else 1
    return task


def table():
    print("Score points:")
    print("  files    <=1:0  2-3:1  4-6:2  7+:4")
    print("  loc      <=30:0 31-120:1 121-300:2 301+:4")
    print("  modules  1:0  2:1  3+:3")
    print("  new_dependency +2   public_api +2   no_tests +1")
    print("  persistence +3  security +3  concurrency +3  cross_system +3  irreversible +3")
    print("  ambiguity 0/1/3/5   novelty 0/1/2/4   blast_radius 0/1/3/5")
    print("Bands:")
    for lo, hi, agent, model, effort, label in BANDS:
        hi_s = "+" if hi >= 10**6 else str(hi)
        print(f"  {lo:>2}-{hi_s:<3} {label:<58} -> {agent} ({model}/{effort})")
    print("Hard flags (always escalate): " + "; ".join(HARD_FLAGS.values()))
    print("Opus rungs additionally require design_agreed=true.")


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--table":
        table()
        return 0
    if argv[0] == "--describe":
        task = describe(" ".join(argv[1:]))
        result = route(task)
        result["prefilled_task"] = task
        print(json.dumps(result, indent=2))
        return 0
    if argv[0] == "--file":
        with open(argv[1], encoding="utf-8") as fh:
            task = json.load(fh)
    else:
        task = json.loads(argv[0])
    print(json.dumps(route(task), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
