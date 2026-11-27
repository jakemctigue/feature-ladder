#!/usr/bin/env python3
"""
feature-ladder external coordinator: run the coordinator rung on GLM 5.3 (the
full model, not Flash) through OpenRouter, and dispatch the Claude subagents it
routes to on the user's own Claude sign-in.

Why: in a small feature most of the cost was the coordinator (an Opus 5.5
xhigh subagent re-reading its whole context every turn), not the worker that
wrote the code. In Claude Code a subagent can only run on a Claude model, so
the GLM coordinator runs the way the GLM workers do: as a child `claude -p`
session pointed at OpenRouter's Anthropic-compatible endpoint, with the same
tools, the same hooks (the audit hook, the ledger) and the coordinator's own
instructions (agents/coordinator.md). That child has no Agent tool: an
OpenRouter session would bill every subagent it started to OpenRouter at API
prices. Instead it runs `dispatch` below for each card the router sends to a
Claude rung, which starts a grandchild `claude -p` on the user's normal
sign-in (the Claude plan) with that agent's instructions, model and effort.

  external_coordinator.py check [--probe]
      CLI present? OpenRouter key readable and valid? --probe also sends one
      minimal request to the orchestrator model. Exit 0 ok, 3 unavailable.

  external_coordinator.py run --prompt-file FILE [--cwd DIR] [--model ID]
                              [--timeout S] [--max-turns N]
      Runs the coordinator on the hand-off prompt in FILE (the same text the
      Agent call would get). Prints the coordinator's report, then one
      `external coordinator:` line (model, turns, time, tokens, cost).
      Exit 0 report returned,
           3 unavailable before any work: no CLI, no key, OpenRouter refused
             the key or has no credits. Dispatch the Opus 5.5 xhigh
             coordinator subagent instead (the fallback),
           1 the child failed or timed out (the report says so; the run may
             still be active: `pr_checkpoint.py finish` or `abort`).
      --timeout defaults to 5400 s; run this in the background.

  external_coordinator.py dispatch --card FILE --id T1 --agent NAME
                                   [--model M] [--effort E] [--cwd DIR]
                                   [--timeout S] [--max-turns N]
      For the GLM coordinator: runs one Task Card on a Claude rung
      (implementer, senior-implementer, mechanic, evidence-collector,
      domain-expert(-high), integrator, or a Fable rung a switch enabled) on
      the user's own Claude sign-in. Model and effort default to the agent's
      file. The session subagent budget and the run's spawn rules
      (session_budget.check) decide first, exactly as the spawn hook does for
      an Agent call. Prints the Evidence Packet, then one `plan worker:` line.
      Exit 0 packet, 1 failed or timed out, 3 could not start (no CLI, not
      signed in), 4 refused by the budget or the spawn rules.

Configuration: the "openrouter" block of providers.json gives the endpoint and
the key (external_worker.py; --no-bs never moves the coordinator to Silk), and
an "orchestrator" block may change the model or the limits:

  {"orchestrator": {"model": "z-ai/glm-5.3", "max_turns": 40, "timeout": 5400}}

The key lives only in the child's environment. The grandchildren `dispatch`
starts get the user's environment back without it.
"""
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402

PLUGIN_ROOT = HERE.parents[1].parent  # .../plugins/feature-ladder
ORCH_DEFAULTS = {"model": "z-ai/glm-5.3", "max_turns": 40, "timeout": 5400}
# The coordinator runs scripts, writes cards and reports; it writes no feature
# code. Hindsight recall stays for recall.py's mcp_fallback; retain never runs in a run.
COORD_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,mcp__plugin_feature-ladder_hindsight__recall"
COORD_DISALLOWED = "Agent,Task,Skill,WebSearch,WebFetch,NotebookEdit,mcp__plugin_feature-ladder_hindsight__retain"
# Long enough for a worker command in the foreground (external_worker's 1500 s and a margin):
# a print-mode session cannot wait for a background command's notification.
BASH_TIMEOUT_MS = "1800000"
# What child_env changes; `dispatch` takes these back out so the grandchild bills the Claude plan.
PROVIDER_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                 "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
                 "BASH_DEFAULT_TIMEOUT_MS", "BASH_MAX_TIMEOUT_MS")
SECRET_KEYS = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}  # never copied into the restore list
PLAN_ENV = "FEATURE_LADDER_PLAN_ENV"
ORCH_NOTE = """
# You are the GLM 5.3 orchestrator

You run in a child Claude Code session on GLM 5.3 through OpenRouter, started by
external_coordinator.py. You have no Agent tool. Follow "Without the Agent tool"
above: Claude rungs (a `"kind": "subagent"` runner, the integrator, a design step)
go through `external_coordinator.py dispatch`; external runner commands run as
printed. Run every worker command in the foreground (Bash allows 30 minutes
here) and do not use run_in_background: this session cannot be woken by a
notification. Your last message is your report: nothing after it is read.
"""
# A grandchild that could not use the user's Claude sign-in: nothing ran.
PLAN_UNAVAILABLE = re.compile(r"not logged in|please run /login|invalid api key|authentication[_ ]error|"
                              r"\b401\b|oauth token (has )?expired|credit balance", re.I)
PLAN_AGENTS = {"implementer", "senior-implementer", "mechanic", "evidence-collector", "domain-expert",
               "domain-expert-high", "integrator", "second-opinion", "deep-verifier", "architect-escalation",
               "ultra-investigator"}
NEVER_DISPATCHED = {"coordinator": "the coordinator does not start another coordinator",
                    "scoper": "the intake scout runs before the coordinator, never inside a run",
                    "codex-reviewer": "reviews happen once, at /feature-ladder:wrap",
                    "memory-scribe": "memory is written by memory_gate.py flush at /feature-ladder:wrap"}
CODING_TOOLS = ["Bash", "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"]
WEB_TOOLS = ["WebFetch", "WebSearch"]
ALWAYS_DISALLOWED = ["Agent", "Task", "Skill", "NotebookEdit",
                     "mcp__plugin_feature-ladder_hindsight__retain"]


def config(cwd):
    cfg = ew.load_config(cwd, no_bs=False)  # the coordinator never moves to Silk
    o = dict(ORCH_DEFAULTS)
    o.update({k: v for k, v in (cfg.get("orchestrator") or {}).items() if v not in (None, "")})
    return cfg, o


def _strip_frontmatter(text):
    return re.sub(r"^---.*?---\s*", "", text, count=1, flags=re.S).strip()


def frontmatter(text):
    """The few keys this script needs from an agent file: model, effort,
    maxTurns, disallowedTools (a YAML list) and skills."""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {"disallowedTools": [], "skills": []}
    if not m:
        return out
    key = None
    for line in m.group(1).splitlines():
        item = re.match(r"^\s+-\s+(.+)$", line)
        if item and key in out and isinstance(out[key], list):
            out[key].append(item.group(1).strip().strip("'\""))
            continue
        kv = re.match(r"^([A-Za-z]+):\s*(.*)$", line)
        if kv:
            key, val = kv.group(1), kv.group(2).strip().strip("'\"")
            if key in ("disallowedTools", "skills"):
                out[key] = [v.strip() for v in val.strip("[]").split(",") if v.strip()] if val else []
            else:
                out[key] = val
    return out


def with_plugin_root(text):
    """Agent files say ${CLAUDE_PLUGIN_ROOT}; a print-mode child given the text as a
    system prompt does not expand it, so the real path goes in."""
    return text.replace("${CLAUDE_PLUGIN_ROOT}", str(PLUGIN_ROOT)).replace("$CLAUDE_PLUGIN_ROOT", str(PLUGIN_ROOT))


def agent_file(name):
    f = ew.AGENTS_DIR / f"{name}.md"
    return f.read_text(encoding="utf-8") if f.is_file() else None


def coordinator_env(cfg, key, model):
    """The child's environment: OpenRouter for every model tier (external_worker.child_env),
    worker commands allowed to run in the foreground, and the user's own values of
    what it changed (never a secret) so `dispatch` can hand them back."""
    restore = {k: os.environ[k] for k in PROVIDER_KEYS if k in os.environ and k not in SECRET_KEYS}
    env = ew.child_env(cfg, key, model)
    env.pop("FEATURE_LADDER_EXTERNAL_WORKER", None)
    env["FEATURE_LADDER_EXTERNAL_COORDINATOR"] = "1"
    env["BASH_DEFAULT_TIMEOUT_MS"] = BASH_TIMEOUT_MS
    env["BASH_MAX_TIMEOUT_MS"] = BASH_TIMEOUT_MS
    env[PLAN_ENV] = json.dumps({"keys": list(PROVIDER_KEYS), "restore": restore})
    return env


def plan_env():
    """The grandchild's environment: the user's own, with the OpenRouter values the
    coordinator child carried taken out, so `claude -p` uses the user's Claude
    sign-in (the plan) again. Outside a GLM coordinator nothing is taken out."""
    env = dict(os.environ)
    for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
              "FEATURE_LADDER_EXTERNAL_WORKER", "FEATURE_LADDER_EXTERNAL_COORDINATOR"):
        env.pop(k, None)
    raw = env.pop(PLAN_ENV, None)
    if raw:
        try:
            spec = json.loads(raw)
        except ValueError:
            spec = {"keys": list(PROVIDER_KEYS), "restore": {}}
        for k in spec.get("keys") or PROVIDER_KEYS:
            env.pop(k, None)
        for k, v in (spec.get("restore") or {}).items():
            if k not in SECRET_KEYS:
                env[k] = str(v)
    env["FEATURE_LADDER_CHILD"] = "1"  # its hooks must not reset the session budget
    return env


def _write_temp(text, prefix):
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.rstrip() + "\n")
    return path


def _unlink(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _int(args, name, default):
    v = args.get(name)
    if v is None:
        return int(default)
    if v is True or not str(v).strip().isdigit() or int(v) < 1:
        raise ValueError(f"{name} needs a whole number of 1 or more")
    return int(v)


def _tokens(result):
    u = result.get("usage") or {}
    inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return inp, int(u.get("output_tokens") or 0)


def _workers_cost_since(root, since):
    """What the OpenRouter workers this coordinator ran were charged on the same key
    (their evidence files: external_worker.py cards, and OpenClaude fanouts and
    teams, whose shared charge is in one `fanout-*` or `team-*` file), so the
    coordinator's own share can be told apart."""
    total = 0.0
    folder = ladder_state.state_dir(root) / "evidence"
    for pattern in ("*-external.json", "*-openclaude.json"):
        for f in folder.glob(pattern):
            try:
                if f.stat().st_mtime >= since:
                    total += float((ladder_state.read_json(f) or {}).get("openrouter_cost_usd") or 0)
            except (OSError, TypeError, ValueError):
                continue
    return total


def _run_slug(root):
    run = ladder_state.read_json(ladder_state.state_dir(root) / "run.json")
    return run.get("slug") if isinstance(run, dict) else None


def run(args):
    cwd = args.get("--cwd") or os.getcwd()
    cfg, o = config(cwd)
    model = args.get("--model") or o["model"]
    prompt_file = args.get("--prompt-file")
    if not prompt_file or not Path(prompt_file).is_file():
        print("external_coordinator: --prompt-file FILE is required", file=sys.stderr)
        return 1
    try:
        timeout = _int(args, "--timeout", o["timeout"])
        max_turns = _int(args, "--max-turns", o["max_turns"])
    except ValueError as e:
        print(f"external_coordinator: {e}", file=sys.stderr)
        return 1
    fallback = "Dispatch the Opus 5.5 xhigh coordinator subagent instead (model opus, effort xhigh)."
    exe = ew.claude_bin()
    if not exe:
        print(f"external coordinator unavailable: claude CLI not found on PATH. {fallback}")
        return 3
    key, source = ew.get_key(cfg, cwd)
    if not key:
        print(f"external coordinator unavailable: no OpenRouter key ({source}). {fallback}")
        return 3
    coord = agent_file("coordinator")
    if coord is None:
        print(f"external coordinator unavailable: agents/coordinator.md not found. {fallback}")
        return 3
    root = ew.repo_root(cwd)
    slug = _run_slug(root)
    system_file = _write_temp(with_plugin_root(_strip_frontmatter(coord)) + "\n" + ORCH_NOTE, "fl-coordinator-")
    cmd = [exe, "-p", "--model", "sonnet", "--output-format", "json", "--max-turns", str(max_turns),
           "--permission-mode", "acceptEdits", "--allowedTools", COORD_TOOLS, "--disallowedTools", COORD_DISALLOWED,
           "--append-system-prompt-file", system_file]
    prompt = Path(prompt_file).read_text(encoding="utf-8-sig")
    before = ew.usage_usd(cfg, key)
    ew.note_usage_baseline(root, before)
    t0 = time.time()
    try:
        try:
            child_exit, stdout, stderr = ew._spawn(cmd, cwd, coordinator_env(cfg, key, model), prompt, timeout)
        except OSError as e:
            child_exit, stdout, stderr = 127, "", f"could not start {exe}: {e}"
    finally:
        _unlink(system_file)
    stdout, stderr = ew.redact(stdout, key), ew.redact(stderr, key)
    secs = round(time.time() - t0)
    result = ew.parse_result(stdout) if stdout.strip() else {}
    said = str(result.get("result") or "").strip()
    timed_out = child_exit is None
    failed = timed_out or child_exit != 0 or bool(result.get("is_error"))
    try:
        turns = int(result.get("num_turns") or 0)
    except (TypeError, ValueError):
        turns = 0
    why = ew._last_lines(stderr) or ew._last_lines(said) or "no output"
    if failed and not timed_out and turns <= 1 and ew.UNAVAILABLE.search(f"{said}\n{stderr}"):
        print(f"external coordinator unavailable, nothing ran: {why}. {fallback}")
        return 3
    if timed_out:
        code, report = 1, (f"{said}\n\n" if said else "") + (
            f"Coordinator stopped: the GLM coordinator ran past {timeout}s and was stopped. The run may still be "
            "active: check `pr_checkpoint.py status`, then `finish` or `abort` it.")
    elif failed or not said:
        code, report = 1, (f"{said}\n\n" if said else "") + (
            f"Coordinator failed (exit {child_exit}): {why}. The run may still be active: check "
            "`pr_checkpoint.py status`, then `finish` or `abort` it.")
    else:
        code, report = 0, said
    after = ew.usage_usd(cfg, key)
    charged = round(after - before, 4) if before is not None and after is not None and after >= before else None
    own = round(max(0.0, charged - _workers_cost_since(root, t0)), 4) if charged is not None else None
    result["openrouter_cost_usd"] = own
    result["seconds"] = secs
    print(report)  # first: nothing below may lose a report that was paid for
    record = {"role": "coordinator", "model": model, "provider": "OpenRouter", "key_source": source,
              "seconds": secs, "exit": child_exit, "timed_out": timed_out, "returned": code,
              "num_turns": result.get("num_turns"), "usage": result.get("usage"),
              "openrouter_cost_usd": own, "openrouter_charged_on_key_usd": charged,
              "claude_code_estimate_usd": result.get("total_cost_usd"), "session_id": result.get("session_id"),
              "stderr_tail": (stderr or "")[-1500:], "report": report}
    try:
        ladder_state.ensure_gitignore(root)
        ladder_state.write_json(ladder_state.state_dir(root) / "runs" / (slug or "no-slug") / "coordinator-external.json",
                                record)
    except OSError as e:
        print(f"external_coordinator: evidence not written: {e}", file=sys.stderr)
    ew.record_ledger(root, "coordinator", "coordinator", model, result, "openrouter", slug=slug,
                     key="external-coordinator")
    inp, out = _tokens(result)
    cost = ("cost unknown" if own is None else
            f"OpenRouter charged about ${own:.4f} for the coordinator (${charged:.4f} on the key in all, "
            "external workers included)")
    print(f"\nexternal coordinator: {model} via OpenRouter, {result.get('num_turns', '?')} turns, {secs}s, "
          f"{inp:,} input tokens, {out:,} output, {cost}")
    return code


def _blocked(task_id, why, kind):
    return (f"TASK {task_id} - blocked\nChanged: none\nChecks run: none\nClaims: {why}\n"
            f"Risks: none\nMemory candidates: none\nRetained: none - {kind}")


def dispatch(args):
    cwd = args.get("--cwd") or os.getcwd()
    task_id = args.get("--id") or "task"
    agent = str(args.get("--agent") or "").split(":")[-1].strip()
    card_path = args.get("--card")
    if agent in NEVER_DISPATCHED:
        print(_blocked(task_id, f"feature-ladder: {agent} is never dispatched here: {NEVER_DISPATCHED[agent]}.",
                       "refused"))
        return 4
    if agent not in PLAN_AGENTS:
        print(f"external_coordinator: --agent must be one of {sorted(PLAN_AGENTS)}", file=sys.stderr)
        return 1
    if not card_path or not Path(card_path).is_file():
        print("external_coordinator: --card FILE is required", file=sys.stderr)
        return 1
    text = agent_file(agent)
    if text is None:
        print(_blocked(task_id, f"agents/{agent}.md not found", "unavailable"))
        return 3
    fm = frontmatter(text)
    model = str(args.get("--model") or fm.get("model") or "sonnet")
    effort = str(args.get("--effort") or fm.get("effort") or "medium")
    try:
        timeout = _int(args, "--timeout", 1500)
        max_turns = _int(args, "--max-turns", fm.get("maxTurns") or 40)
    except ValueError as e:
        print(f"external_coordinator: {e}", file=sys.stderr)
        return 1
    exe = ew.claude_bin()
    if not exe:
        print(_blocked(task_id, "claude CLI not found on PATH", "unavailable"))
        return 3
    import session_budget  # noqa: E402
    sroot = session_budget.find_root(cwd)
    ok, why = session_budget.check(sroot, agent, model, via="plan")
    if not ok:
        print(_blocked(task_id, why, "budget"))
        return 4
    disallowed = list(dict.fromkeys(ALWAYS_DISALLOWED + fm.get("disallowedTools", [])))
    allowed = [t for t in CODING_TOOLS + (WEB_TOOLS if agent in ("evidence-collector", "second-opinion") else [])
               if t not in disallowed]
    role = with_plugin_root(_strip_frontmatter(text))
    if "feature-ladder:worker-contract" in fm.get("skills", []) or agent != "integrator":
        role += "\n\n" + ew.contract_text()
    system_file = _write_temp(with_plugin_root(role), f"fl-{agent}-")
    cmd = [exe, "-p", "--model", model, "--effort", effort, "--output-format", "json", "--max-turns", str(max_turns),
           "--permission-mode", "acceptEdits", "--allowedTools", ",".join(allowed),
           "--disallowedTools", ",".join(disallowed), "--append-system-prompt-file", system_file]
    root = ew.repo_root(cwd)
    # A Claude rung reads the same filtered Hindsight recall as an OpenRouter card:
    # a Task Card without a Memory: line gets one (the integrator's prompt is not a card).
    card, memory_added = Path(card_path).read_text(encoding="utf-8-sig"), False
    if card.lstrip().startswith("TASK"):
        card, memory_added = ew.with_memory(card, root, cwd)
        card += "\n"
    t0 = time.time()
    try:
        try:
            child_exit, stdout, stderr = ew._spawn(cmd, cwd, plan_env(), card, timeout)
        except OSError as e:
            child_exit, stdout, stderr = 127, "", f"could not start {exe}: {e}"
    finally:
        _unlink(system_file)
    secs = round(time.time() - t0)
    result = ew.parse_result(stdout) if (stdout or "").strip() else {}
    said = ew.unfence(str(result.get("result") or ""))
    timed_out = child_exit is None
    failed = timed_out or child_exit != 0 or bool(result.get("is_error"))
    try:
        turns = int(result.get("num_turns") or 0)
    except (TypeError, ValueError):
        turns = 0
    why_failed = ew._last_lines(stderr) or ew._last_lines(said) or "no output"
    if said.startswith("TASK") or said.startswith("INTEGRATION"):
        code, packet = 0, said
    elif timed_out:
        code, packet = 1, (f"TASK {task_id} - partial\nChecks run: none quoted\n"
                           f"Claims: {agent} timed out after {timeout}s and was stopped\n"
                           "Risks: work may be half-applied; verify.py will show scope changes\nRetained: none - timeout")
    elif failed and turns <= 1 and PLAN_UNAVAILABLE.search(f"{said}\n{stderr}"):
        session_budget.refund(sroot, agent, model, via="plan")  # it never ran: the slot goes back
        code, packet = 3, _blocked(task_id, f"{agent} could not start on your Claude sign-in: {why_failed}",
                                   "unavailable")
    elif failed or not said:
        code, packet = 1, (f"TASK {task_id} - partial\n{said or '(worker returned no text)'}\n"
                           f"Claims: {agent} failed (exit {child_exit}): {why_failed}\n"
                           "Risks: work may be half-applied; verify.py will show scope changes")
    else:
        code, packet = 0, f"TASK {task_id} - partial\n{said}"
    if "Retained:" not in packet:
        packet += "\nRetained: none - worker did not report"
    print(packet)
    safe = ew._safe_id(task_id)
    record = {"task": task_id, "agent": agent, "model": model, "effort": effort, "provider": "Claude plan",
              "seconds": secs, "exit": child_exit, "timed_out": timed_out, "returned": code,
              "num_turns": result.get("num_turns"), "usage": result.get("usage"),
              "claude_code_estimate_usd": result.get("total_cost_usd"), "session_id": result.get("session_id"),
              "stderr_tail": (stderr or "")[-1500:], "card_added": ["memory"] if memory_added else [],
              "card": card, "packet": packet}
    try:
        ladder_state.ensure_gitignore(root)
        ladder_state.write_json(ladder_state.state_dir(root) / "evidence" / f"{safe}-plan.json", record)
    except OSError as e:
        print(f"external_coordinator: evidence not written: {e}", file=sys.stderr)
    result["openrouter_cost_usd"] = None  # the Claude plan: no dollar figure
    result["seconds"] = secs
    ew.record_ledger(root, task_id, agent, model, result, "claude plan", key=f"plan-{safe}")
    inp, out = _tokens(result)
    print(f"\nplan worker: {agent} on {model}/{effort} (your Claude sign-in), {result.get('num_turns', '?')} turns, "
          f"{secs}s, {inp:,} input tokens, {out:,} output")
    return code


def check(argv):
    cfg, o = config(os.getcwd())
    status = {"model": o["model"], "max_turns": o["max_turns"], "timeout": o["timeout"],
              "claude_cli": ew.claude_bin() is not None}
    key, source = ew.get_key(cfg)
    status["key"] = source if key else None
    if not key:
        status["error"] = source
        print(json.dumps(status, indent=2))
        return 3
    status["provider_key"] = ew.key_status(cfg, key)
    if "--probe" in argv:
        status["probe"] = ew.probe_model(cfg, key, o["model"])
    print(json.dumps(status, indent=2))
    ok = status["claude_cli"] and status["provider_key"].get("valid") and status.get("probe", {"ok": True}).get("ok")
    return 0 if ok else 3


RUN_FLAGS = {"--prompt-file", "--cwd", "--model", "--timeout", "--max-turns"}
DISPATCH_FLAGS = {"--card", "--id", "--agent", "--model", "--effort", "--cwd", "--timeout", "--max-turns"}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd in ("run", "dispatch"):
        args = ew.parse(argv[1:])
        unknown = sorted(set(args) - (RUN_FLAGS if cmd == "run" else DISPATCH_FLAGS))
        if unknown:
            print(f"external_coordinator: unknown option {', '.join(unknown)}", file=sys.stderr)
            return 1
        return run(args) if cmd == "run" else dispatch(args)
    if cmd == "check":
        return check(argv[1:])
    print(__doc__)
    return 1


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
