#!/usr/bin/env python3
"""
Session subagent budget and per-run spawn rules for feature-ladder. Costs no
model tokens.

The user picks, once per coding session, how many subagents the ladder may
spawn in that session (asked in the intake round of the first feature):

    S = 2 (small, 1-2)   M = 4 (medium, 3-4)   L = 8 (large, up to 8)

The intake scout and the coordinator are not counted; every worker, expert,
reviewer and mechanic is, and so is every external (GLM) worker. State lives
in <repo>/.feature-ladder/session.json (git-ignored).

As a PreToolUse hook on Agent/Task it denies a spawn when:
  - the session budget is spent;
  - a run is active and the spawn is something a feature run never does:
      codex-reviewer, memory-scribe (they belong to /feature-ladder:wrap),
      any Fable rung or `model: fable` other than the coordinator, unless the
      run's switches allow it (--second-opinion, --escalate, --ultra);
      a domain expert when the intake planned no design step, or a second one;
      a second mechanic in the same run;
      more workers than the run's budget (its workers, +1 retry, +1 design).

    session_budget.py status [--cwd DIR]          JSON: size, max, used, left (or "unset")
    session_budget.py set --size S|M|L [--cwd DIR]
    session_budget.py charge --agent NAME [--model M] [--cwd DIR]
                                                  for spawns no hook sees (external workers);
                                                  exit 0 allowed, 4 denied (reason on stdout)
    session_budget.py remember --line "[kind] fact" [--line ...] [--cwd DIR]
                                                  queue memories for /feature-ladder:wrap; each
                                                  line must pass memory_gate.py (else rejected
                                                  with the reason)
    session_budget.py memory [--cwd DIR]          print the queued memories
    session_budget.py new [--cwd DIR]             start a new session (budget asked again; the
                                                  memory queue is kept). The SessionStart hook
                                                  does this in Claude Code; the app skill calls it
    session_budget.py close [--cwd DIR]           end the session (after wrap)
    session_budget.py --session-start | --pretooluse   hook modes (payload on stdin)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SIZES = {"S": 2, "M": 4, "L": 8}
SIZE_LABELS = {"S": "Small (1-2 subagents)", "M": "Medium (3-4 subagents)", "L": "Large (up to 8 subagents)"}
NOT_COUNTED = {"scoper", "coordinator"}
FABLE_RUNGS = {"second-opinion": "second_opinion", "deep-verifier": "escalate",
               "architect-escalation": "escalate", "ultra-investigator": "ultra"}
WRAP_ONLY = {"codex-reviewer", "memory-scribe"}  # memory is written by memory_gate.py flush, no agent
DESIGN_RUNGS = {"domain-expert", "domain-expert-high"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_root(cwd):
    """The main checkout's root (linked worktrees resolve to it), or cwd."""
    p = Path(cwd or os.getcwd()).resolve()
    try:
        r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           cwd=str(p), capture_output=True, text=True, timeout=10)
        common = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        common = None
    if common and common.name == ".git":
        return common.parent
    for d in [p, *p.parents]:
        if (d / ".feature-ladder").is_dir():
            return d
    return p


def _path(root, name):
    return Path(root) / ".feature-ladder" / name


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    gi = path.parent / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
        missing = [e for e in ("session.json", "session-memory*.md", "memory-gate.json", "memory-ledger.jsonl")
                   if e not in lines]
        if missing:
            gi.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")
    except OSError:
        pass


def load(root):
    return _read(_path(root, "session.json")) or {}


def save(root, sess):
    _write(_path(root, "session.json"), sess)


def status(root):
    s = load(root)
    if not s.get("max_agents"):
        return {"budget": "unset", "session_id": s.get("session_id"),
                "options": {k: SIZE_LABELS[k] for k in SIZES}}
    used = len(s.get("spawned", []))
    return {"budget": s["size"], "label": SIZE_LABELS[s["size"]], "max": s["max_agents"],
            "used": used, "left": max(0, s["max_agents"] - used), "session_id": s.get("session_id")}


def set_size(root, size):
    size = str(size).strip().upper()[:1]
    if size not in SIZES:
        raise ValueError(f"size must be one of {sorted(SIZES)}")
    s = load(root)
    s.update({"size": size, "max_agents": SIZES[size], "set_at": now()})
    s.setdefault("spawned", [])
    s.setdefault("started", now())
    save(root, s)
    return status(root)


def active_run(root):
    run = _read(_path(root, "run.json")) or {}
    return run if run.get("active") else None


def check(root, agent, model=None, via="subagent"):
    """Decide one spawn. Returns (allowed, reason). Records it when allowed."""
    agent = str(agent or "").split(":")[-1]
    if agent in NOT_COUNTED:
        return True, ""
    sess = load(root)
    run = active_run(root)
    if run is not None:
        switches = set(run.get("switches") or [])
        budget = run.get("budget") or {}
        mine = [e for e in sess.get("spawned", []) if e.get("run") == run.get("slug")]
        if agent in WRAP_ONLY:
            return False, (f"feature-ladder: {agent} does not run inside a feature run; reviews and memory "
                           "happen once at the end of the session with /feature-ladder:wrap. Carry on without it.")
        fable_switch = FABLE_RUNGS.get(agent)
        if fable_switch or str(model or "").lower().startswith("fable") or "fable" in str(model or "").lower():
            ok = fable_switch and (fable_switch in switches or (fable_switch == "escalate" and "ultra" in switches))
            if not ok:
                return False, (f"feature-ladder: no Fable calls beyond the coordinator unless the user asked "
                               f"({agent or model}). Decide it yourself or leave it under 'Left for you'.")
        if agent in DESIGN_RUNGS:
            if not budget.get("design"):
                return False, ("feature-ladder: the intake planned no design step for this feature (no hard "
                               "flag). Decide the approach yourself from the intake and the card.")
            if any(e["agent"] in DESIGN_RUNGS for e in mine):
                return False, "feature-ladder: one design step per feature; accept or decide yourself."
        if agent == "mechanic" and any(e["agent"] == "mechanic" for e in mine):
            return False, "feature-ladder: one mechanic per feature at most; verify.py runs the checks."
        cap = int(budget.get("workers", 1) or 1) + 1 + (1 if budget.get("design") else 0)
        if len(mine) >= cap:
            return False, (f"feature-ladder: this feature's budget is {cap} subagents (workers + one retry"
                           f"{' + design' if budget.get('design') else ''}) and they are used. Finish with "
                           "what you have and report anything unfinished as partial.")
    if sess.get("max_agents"):
        used = len(sess.get("spawned", []))
        if used >= sess["max_agents"]:
            return False, (f"feature-ladder: the session's subagent budget is spent ({used} of "
                           f"{sess['max_agents']}, {SIZE_LABELS[sess['size']]}). Finish with what you have "
                           "and report anything unfinished as partial; do not work around this.")
    if sess.get("max_agents") or run is not None:
        sess.setdefault("spawned", []).append({"at": now(), "agent": agent or "?", "model": model,
                                               "via": via, "run": run.get("slug") if run else None})
        save(root, sess)
    return True, ""


def remember(root, lines):
    """Queue '[kind] fact' lines for the end of the session. Each line passes
    the memory gate now (memory_gate.py), so a summary or a vague line is
    refused with its reason instead of reaching Hindsight later."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hindsight-memory" / "scripts"))
    import memory_gate
    p = _path(root, "session-memory.md")
    queued = set()
    if p.is_file():
        queued = {memory_gate._hash(l.split("]", 1)[-1]) for l in p.read_text(encoding="utf-8").splitlines() if "]" in l}
    seen = memory_gate.retained_hashes(root) | queued
    ok, rejected = [], []
    for l in lines:
        l = " ".join(str(l).split())
        if not l:
            continue
        try:
            kind, fact, h = memory_gate.check_line(root, l, seen)
            seen.add(h)
            ok.append(f"[{kind}] {fact}")
        except ValueError as e:
            rejected.append({"line": l[:200], "reason": str(e)})
    if ok:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            for l in ok:
                fh.write(f"- {l}\n")
        _write(_path(root, "session.json"), load(root) or {"started": now(), "spawned": []})
    return {"queued": len(ok), "rejected": rejected}


# -------------------------------------------------------------------- hooks --
def hook_session_start(payload):
    """A new Claude Code session is a new coding session: the budget is asked again.
    resume/compact keep the current one."""
    if str(payload.get("source") or "startup") in ("resume", "compact") or os.environ.get("FEATURE_LADDER_CHILD"):
        return
    root = find_root(payload.get("cwd"))
    if not (Path(root) / ".feature-ladder").is_dir():
        return  # never ran the ladder here: write nothing
    save(root, {"session_id": payload.get("session_id"), "started": now(), "spawned": []})


def hook_pretooluse(payload):
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    ti = payload.get("tool_input") or {}
    root = find_root(payload.get("cwd"))
    if not (Path(root) / ".feature-ladder").is_dir():
        return None
    allowed, reason = check(root, ti.get("subagent_type") or "general-purpose", ti.get("model"))
    if allowed:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def main(argv):
    if argv and argv[0] in ("--session-start", "--pretooluse"):
        try:
            payload = json.loads(sys.stdin.read().lstrip("﻿") or "{}")
            if argv[0] == "--session-start":
                hook_session_start(payload)
            else:
                out = hook_pretooluse(payload)
                if out:
                    print(json.dumps(out))
        except Exception:  # noqa: BLE001 - a broken hook must never break the session
            pass
        return 0
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    opts, lines = {}, []
    it = iter(rest)
    for a in it:
        if a == "--line":
            lines.append(next(it, ""))
        elif a.startswith("--"):
            opts[a] = next(it, "")
    root = find_root(opts.get("--cwd"))
    try:
        if cmd == "status":
            print(json.dumps(status(root), indent=2))
        elif cmd == "set":
            print(json.dumps(set_size(root, opts.get("--size", "")), indent=2))
        elif cmd == "charge":
            ok, reason = check(root, opts.get("--agent"), opts.get("--model"), via="external")
            print(json.dumps({"allowed": ok, "reason": reason}))
            return 0 if ok else 4
        elif cmd == "remember":
            print(json.dumps(remember(root, lines), indent=2))
        elif cmd == "memory":
            p = _path(root, "session-memory.md")
            sys.stdout.write(p.read_text(encoding="utf-8") if p.is_file() else "")
        elif cmd == "new":
            save(root, {"started": now(), "spawned": []})
            print(json.dumps(status(root)))
        elif cmd == "close":
            s = load(root)
            s.update({"closed": now(), "max_agents": None})
            save(root, s)
            p = _path(root, "session-memory.md")
            if p.is_file():
                p.replace(p.with_name(f"session-memory-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.md"))
            print(json.dumps({"closed": True}))
        else:
            print(__doc__)
            return 1
    except ValueError as e:
        print(f"session_budget {cmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
