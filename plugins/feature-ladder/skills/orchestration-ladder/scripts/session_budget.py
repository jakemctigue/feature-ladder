#!/usr/bin/env python3
"""
Session subagent budget and per-run spawn rules for feature-ladder. Costs no
model tokens.

The user picks, once per coding session, how many subagents the ladder may
spawn in that session (asked in the intake round of the first feature):

    S = 2 (small, 1-2)   M = 4 (medium, 3-4)   L = 8 (large, up to 8)

The intake scout and the coordinator are not counted; every worker, expert,
reviewer and mechanic is, and so is every external (GLM) worker. Outside an
active run only feature-ladder:* agents are counted, so an unrelated Explore
or web-fetch spawn never spends the ladder's budget. State lives in
<main checkout>/.feature-ladder/session.json (git-ignored); it is written
atomically under a lock, and a file that does not parse denies spawns instead
of silently meaning "no budget".

In the same round the user picks the session's orchestrator, the model the
coordinator runs on:

    opus  = Opus 5.5 at xhigh effort     fable = Fable 5.2

It is asked once per coding session, up front, beside the budget. A run
started with `--orchestrator fable|opus` answers it in advance. Nothing picks
Fable on its own: with no answer (--auto) the coordinator runs on Opus xhigh,
and a coordinator spawn on Fable is denied unless the user chose Fable.

As a PreToolUse hook on Agent/Task it denies a spawn when:
  - the session budget is spent;
  - a run is active and the spawn is something a feature run never does:
      codex-reviewer (it belongs to /feature-ladder:wrap; the removed
      memory-scribe name stays denied so nothing spawns a stand-in),
      any Fable rung or `model: fable` except the integrator, unless the
      run's switches allow it (--second-opinion, --escalate, --ultra);
      the integrator before two coding agents have returned packets for the
      feature, or a second integrator in the same run;
      a domain expert when the intake planned no design step, or a second one;
      a second mechanic in the same run;
      more workers than the run's budget (its workers, +1 retry, +1 design;
      the integrator is not a worker and does not count against it).

    session_budget.py status [--cwd DIR]          JSON: size, max, used, left (or "unset")
    session_budget.py set --size S|M|L [--cwd DIR]
    session_budget.py orchestrator [--set opus|fable] [--cwd DIR]
                                                  JSON: the session's orchestrator (key, label, model,
                                                  effort, chosen); --set stores the user's answer
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
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ladder_state  # noqa: E402

SIZES = {"S": 2, "M": 4, "L": 8}
SIZE_LABELS = {"S": "Small (1-2 subagents)", "M": "Medium (3-4 subagents)", "L": "Large (up to 8 subagents)"}
NOT_COUNTED = {"scoper", "coordinator"}
FABLE_RUNGS = {"second-opinion": "second_opinion", "deep-verifier": "escalate",
               "architect-escalation": "escalate", "ultra-investigator": "ultra"}
# memory-scribe no longer exists (memory is written by memory_gate.py flush, no agent);
# the name stays here so an old prompt cannot spawn a stand-in inside a run.
WRAP_ONLY = {"codex-reviewer", "memory-scribe"}
DESIGN_RUNGS = {"domain-expert", "domain-expert-high"}
INTEGRATOR = "integrator"  # the one Fable rung a run reaches on its own: after >= 2 coding agents
INTEGRATOR_MIN_WORKERS = 2

# The orchestrator: the model the coordinator runs on, picked by the user once
# per session in the up-front question. `model` and `effort` are what the Agent call passes; `model_id` is
# the full id for callers that need one.
ORCHESTRATORS = {
    "opus": {"label": "Opus 5.5 xhigh", "model": "opus", "model_id": "claude-opus-5-5", "effort": "xhigh",
             "description": "Opus 5.5 at xhigh effort runs the coordinator. Used when nothing is picked."},
    "fable": {"label": "Fable 5.2", "model": "fable", "model_id": "claude-fable-5-2", "effort": "medium",
              "description": "Fable 5.2 runs the coordinator. Only ever used when you pick it here."},
}
DEFAULT_ORCHESTRATOR = "opus"  # never Fable unless the user said so


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_root(cwd):
    """The main checkout's root (linked worktrees resolve to it), or cwd."""
    return ladder_state.state_root(cwd)


def _path(root, name):
    return Path(root) / ".feature-ladder" / name


def _read(path):
    return ladder_state.read_json(path)


def _write(path, data):
    ladder_state.write_json(path, data)
    ladder_state.ensure_gitignore(Path(path).parent.parent)


def load(root):
    """The session state; {} when there is none. Raises ladder_state.CorruptState
    (a ValueError) when session.json exists but does not parse: an unreadable
    budget must never read as "no budget"."""
    return ladder_state.read_json(_path(root, "session.json"), strict=True) or {}


def save(root, sess):
    _write(_path(root, "session.json"), sess)


UNREADABLE = ("feature-ladder: .feature-ladder/session.json is unreadable ({why}), so the session's subagent "
              "budget cannot be checked. Do not spawn; tell the user to run `session_budget.py new` (the "
              "budget is asked again) or repair the file.")


def status(root):
    try:
        s = load(root)
    except ladder_state.CorruptState as e:
        return {"budget": "unreadable", "reason": str(e), "session_id": None,
                "options": {k: SIZE_LABELS[k] for k in SIZES}}
    if not s.get("max_agents"):
        return {"budget": "unset", "session_id": s.get("session_id"),
                "options": {k: SIZE_LABELS[k] for k in SIZES}, "orchestrator": orchestrator(root)}
    used = len(s.get("spawned", []))
    return {"budget": s["size"], "label": SIZE_LABELS[s["size"]], "max": s["max_agents"],
            "used": used, "left": max(0, s["max_agents"] - used), "session_id": s.get("session_id"),
            "orchestrator": orchestrator(root)}


def orchestrator_key(value):
    """'Fable 5.2', 'fable', 'claude-fable-5-2', 'Opus 5.5 xhigh', 'opus' -> key, or None."""
    v = str(value or "").strip().lower()
    if v.startswith("claude-"):
        v = v[len("claude-"):]
    for key in ORCHESTRATORS:
        if v.startswith(key):
            return key
    return None


def orchestrator(root, override=None):
    """Which model coordinates this session. `override` is a run's
    --orchestrator switch; otherwise the user's answer to the up-front
    question, stored in session.json. `chosen` is False until one of those
    exists: the fallback is then Opus xhigh, never Fable."""
    key, source = orchestrator_key(override), "switch"
    if override and key is None:
        raise ValueError(f"orchestrator must be one of {sorted(ORCHESTRATORS)}")
    if key is None:
        key, source = orchestrator_key(load(root).get("orchestrator")), "asked"
    if key is None:
        key, source = DEFAULT_ORCHESTRATOR, "default"
    o = ORCHESTRATORS[key]
    return {"key": key, "label": o["label"], "model": o["model"], "model_id": o["model_id"],
            "effort": o["effort"], "source": source, "chosen": source != "default"}


def set_orchestrator(root, value):
    """Store the user's answer for this session (asked again in the next one)."""
    key = orchestrator_key(value)
    if key is None:
        raise ValueError(f"orchestrator must be one of {sorted(ORCHESTRATORS)}")
    with ladder_state.locked(root):
        s = load(root)
        s.update({"orchestrator": key, "orchestrator_set_at": now()})
        s.setdefault("spawned", [])
        s.setdefault("started", now())
        save(root, s)
    return orchestrator(root)


def set_run_orchestrator(root, value, slug=None):
    """Remember a run's --orchestrator switch in session.json, so the spawn
    hook honours it even when no run.json exists (a folder that is not a git
    repo). The next confirmed intake replaces or clears it: one run only."""
    key = orchestrator_key(value)
    with ladder_state.locked(root):
        s = load(root)
        if key is None and not s.get("orchestrator_run"):
            return
        s["orchestrator_run"] = {"key": key, "slug": slug, "at": now()} if key else None
        s.setdefault("spawned", [])
        s.setdefault("started", now())
        save(root, s)


def set_size(root, size):
    size = str(size).strip().upper()[:1]
    if size not in SIZES:
        raise ValueError(f"size must be one of {sorted(SIZES)}")
    with ladder_state.locked(root):
        s = load(root)
        s.update({"size": size, "max_agents": SIZES[size], "set_at": now()})
        s.setdefault("spawned", [])
        s.setdefault("started", now())
        save(root, s)
    return status(root)


def active_run(root):
    run = _read(_path(root, "run.json")) or {}
    return run if run.get("active") else None


def _same_run(entry, run):
    """A spawn belongs to this run when slug AND start time match: a retry after
    an abort reuses the slug and must not inherit the dead run's spawns."""
    return entry.get("run") == run.get("slug") and entry.get("run_started") == run.get("started")


def check(root, agent, model=None, via="subagent"):
    """Decide one spawn. Returns (allowed, reason). Records it when allowed.
    Load, decide and save happen under one lock, so parallel spawns cannot
    overspend the budget."""
    try:
        with ladder_state.locked(root):
            return _check(root, agent, model, via)
    except ladder_state.CorruptState as e:
        return False, UNREADABLE.format(why=e)


def _check(root, agent, model, via):
    agent = str(agent or "").split(":")[-1]
    if agent == "coordinator" and "fable" in str(model or "").lower():
        run_pick = (orchestrator_key(((active_run(root) or {}).get("orchestrator") or {}).get("key"))
                    or orchestrator_key((load(root).get("orchestrator_run") or {}).get("key")))
        if orchestrator(root, run_pick)["key"] != "fable":
            return False, ("feature-ladder: the coordinator runs on Fable only when the user picked Fable 5.2 "
                           "as the orchestrator (the up-front question, or --orchestrator fable on this "
                           "run). Dispatch it with model opus, effort xhigh.")
    if agent in NOT_COUNTED:
        return True, ""
    sess = load(root)
    run = active_run(root)
    if run is not None:
        switches = set(run.get("switches") or [])
        budget = run.get("budget") or {}
        mine = [e for e in sess.get("spawned", []) if _same_run(e, run)]
        if agent in WRAP_ONLY:
            return False, (f"feature-ladder: {agent} does not run inside a feature run; reviews and memory "
                           "happen once at the end of the session with /feature-ladder:wrap. Carry on without it.")
        workers = [e for e in mine if e["agent"] not in DESIGN_RUNGS and e["agent"] != INTEGRATOR]
        if agent == INTEGRATOR:
            distinct = {(e["agent"], str(e.get("model") or "").lower()) for e in workers}
            if len(distinct) < INTEGRATOR_MIN_WORKERS:
                return False, (f"feature-ladder: the integrator (Fable) runs only after {INTEGRATOR_MIN_WORKERS} or "
                               "more different coding agents (another agent or another model) returned packets "
                               "for this feature; one worker's work, or a retry of the same worker, is accepted "
                               "by verify.py alone.")
            if any(e["agent"] == INTEGRATOR for e in mine):
                return False, "feature-ladder: one integration pass per feature; route its card and verify."
        else:
            fable_switch = FABLE_RUNGS.get(agent)
            if fable_switch or str(model or "").lower().startswith("fable") or "fable" in str(model or "").lower():
                ok = fable_switch and (fable_switch in switches or (fable_switch == "escalate" and "ultra" in switches))
                if not ok:
                    return False, (f"feature-ladder: the only Fable rung a run reaches on its own is the integrator "
                                   f"({agent or model} is not). Decide it yourself or use a switch the user gave.")
        if agent in DESIGN_RUNGS:
            if not budget.get("design"):
                return False, ("feature-ladder: the intake planned no design step for this feature (no hard "
                               "flag). Decide the approach yourself from the intake and the card.")
            if any(e["agent"] in DESIGN_RUNGS for e in mine):
                return False, "feature-ladder: one design step per feature; accept or decide yourself."
        if agent == "mechanic" and any(e["agent"] == "mechanic" for e in mine):
            return False, "feature-ladder: one mechanic per feature at most; verify.py runs the checks."
        cap = int(budget.get("workers", 1) or 1) + 1 + (1 if budget.get("design") else 0)
        if agent != INTEGRATOR and len([e for e in mine if e["agent"] != INTEGRATOR]) >= cap:
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
                                               "via": via, "run": run.get("slug") if run else None,
                                               "run_started": run.get("started") if run else None})
        save(root, sess)
    return True, ""


def refund(root, agent, model=None, via="external"):
    """Give back the newest matching spawn: the worker was charged but never
    ran (bad key, empty credits). Returns True when a slot was returned."""
    agent = str(agent or "").split(":")[-1]
    try:
        with ladder_state.locked(root):
            sess = load(root)
            spawned = sess.get("spawned") or []
            for i in range(len(spawned) - 1, -1, -1):
                e = spawned[i]
                if e.get("agent") == agent and e.get("model") == model and e.get("via") == via:
                    del spawned[i]
                    save(root, sess)
                    return True
    except ladder_state.CorruptState:
        pass
    return False


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
        with ladder_state.locked(root):
            with open(p, "a", encoding="utf-8") as fh:
                for l in ok:
                    fh.write(f"- {l}\n")
            try:
                sess = load(root)
            except ladder_state.CorruptState:
                sess = None  # leave an unreadable budget file for `new` to replace
            if sess is not None:
                save(root, sess or {"started": now(), "spawned": []})
    return {"queued": len(ok), "rejected": rejected}


def pending_memory(root):
    """Queued lines that still pass the gate and were never written: what a
    flush left behind (over the per-session cap, or Hindsight unreachable)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "hindsight-memory" / "scripts"))
    import memory_gate
    p = _path(root, "session-memory.md")
    if not p.is_file():
        return []
    seen, out = memory_gate.retained_hashes(root), []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            kind, fact, h = memory_gate.check_line(root, line, seen)
        except ValueError:
            continue
        seen.add(h)
        out.append(f"- [{kind}] {fact}")
    return out


def close(root):
    """End the session. The queue is archived; lines that were never written
    go back into a fresh queue so the next wrap picks them up."""
    with ladder_state.locked(root):
        s = load(root)
        s.update({"closed": now(), "max_agents": None})
        save(root, s)
    p = _path(root, "session-memory.md")
    kept = []
    if p.is_file():
        kept = pending_memory(root)
        p.replace(p.with_name(f"session-memory-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.md"))
        if kept:
            ladder_state.write_text(p, "\n".join(kept) + "\n")
    return {"closed": True, "memory_left_in_queue": len(kept)}


# -------------------------------------------------------------------- hooks --
def hook_session_start(payload):
    """A new Claude Code session is a new coding session: the budget is asked again.
    resume/compact keep the current one."""
    if str(payload.get("source") or "startup") in ("resume", "compact") or os.environ.get("FEATURE_LADDER_CHILD"):
        return
    root = find_root(payload.get("cwd"))
    if not (Path(root) / ".feature-ladder").is_dir():
        return  # never ran the ladder here: write nothing
    if active_run(root) is not None:
        return  # a second window or a background session opened mid-run: the run keeps its budget
    with ladder_state.locked(root):
        save(root, {"session_id": payload.get("session_id"), "started": now(), "spawned": []})


def hook_pretooluse(payload):
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    ti = payload.get("tool_input") or {}
    root = find_root(payload.get("cwd"))
    if not (Path(root) / ".feature-ladder").is_dir():
        return None
    agent_type = str(ti.get("subagent_type") or "general-purpose")
    if active_run(root) is None and not agent_type.startswith("feature-ladder:"):
        return None  # not a ladder agent and no run: not the ladder's budget to spend
    allowed, reason = check(root, agent_type, ti.get("model"))
    if allowed:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


# Every subcommand and the options it takes; anything else is an error, not ignored.
OPTIONS = {"status": {"--cwd"}, "set": {"--cwd", "--size"}, "orchestrator": {"--cwd", "--set"},
           "charge": {"--cwd", "--agent", "--model"}, "remember": {"--cwd", "--line"},
           "memory": {"--cwd"}, "new": {"--cwd"}, "close": {"--cwd"}}


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
    ladder_state.utf8_stdio()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    known = OPTIONS.get(cmd)
    if known is None:
        print(__doc__)
        return 1
    opts, lines = {}, []
    it = iter(rest)
    for a in it:
        if a not in known:
            print(f"session_budget {cmd}: unknown option {a!r} (it takes: {', '.join(sorted(known)) or 'none'})",
                  file=sys.stderr)
            return 1
        value = next(it, None)
        if value is None:
            print(f"session_budget {cmd}: {a} needs a value", file=sys.stderr)
            return 1
        if a == "--line":
            lines.append(value)
        else:
            opts[a] = value
    root = find_root(opts.get("--cwd"))
    try:
        if cmd == "status":
            print(json.dumps(status(root), indent=2))
        elif cmd == "set":
            print(json.dumps(set_size(root, opts.get("--size", "")), indent=2))
        elif cmd == "orchestrator":
            print(json.dumps(set_orchestrator(root, opts["--set"]) if "--set" in opts else orchestrator(root),
                             indent=2))
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
            with ladder_state.locked(root):
                save(root, {"started": now(), "spawned": []})
            print(json.dumps(status(root)))
        elif cmd == "close":
            print(json.dumps(close(root)))
        else:
            print(__doc__)
            return 1
    except ValueError as e:
        print(f"session_budget {cmd}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
