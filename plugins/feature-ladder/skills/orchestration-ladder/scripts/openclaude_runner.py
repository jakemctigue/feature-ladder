#!/usr/bin/env python3
"""
feature-ladder OpenClaude runner: start several agents on OpenRouter at once
through OpenClaude (`openclaude`, the `oc` command; npm @gitlawb/openclaude),
a Claude Code fork that talks to any OpenAI-compatible endpoint.

Why: a Claude Code subagent can only run on a Claude model, and a child
`claude -p` pointed at OpenRouter has no working Agent tool. OpenClaude has
both: each process is one OpenRouter agent with the same tools and the same
Evidence Packet contract, and inside one OpenClaude session the Agent tool
starts more agents, each routed to its own OpenRouter model. Claude (the main
session or the coordinator) uses this to run many agents on OpenRouter; every
agent bills OpenRouter credits, never the Claude plan, whatever it costs.

  openclaude_runner.py check [--probe]
      openclaude on PATH (and its version)? OpenRouter key readable and valid?
      --probe also sends one minimal request to each model used here.
      Exit 0 ok, 3 unavailable.

  openclaude_runner.py fanout --card FILE --id T1 --rung RUNG [--model ID]
                              [--card FILE --id T2 --rung RUNG [--model ID] ...]
                              [--cwd DIR] [--timeout S] [--max-turns N] [--parallel N]
      Runs every card at the same time, one OpenClaude agent each (at most
      --parallel at once, default 4). Each --card starts a new card; the --id,
      --rung and --model after it belong to that card. RUNG is mechanic,
      implementer, implementer-high, evidence-collector (read only, web
      allowed) or general. The model defaults to the rung's OpenRouter model
      in providers.json (external_worker.py's models; general uses GLM 5.3).
      Each card takes one session budget slot (session_budget.check, the same
      rules as a subagent) before anything starts. Prints every Evidence
      Packet in the order given, each followed by an `openclaude worker:` line,
      then one `openclaude fanout:` line with the OpenRouter charge.
      Writes .feature-ladder/evidence/<id>-openclaude.json and a ledger row per card.
      Exit 0 every card returned a packet,
           1 a card failed or timed out (its packet says "partial"),
           4 a card was refused by the budget or the spawn rules,
           3 unavailable: no openclaude, no key, OpenRouter refused the key,
             or a --no-bs run (Silk is not OpenAI-compatible here: use
             external_worker.py). Run those cards on their Claude rung.
      --timeout defaults to 1500 s per card: run this in the background, or
      pass --timeout 540 to stay inside one foreground tool call.

  openclaude_runner.py team --prompt-file FILE --agents N [--model ID] [--cwd DIR]
                            [--timeout S] [--max-turns N]
      One OpenClaude session on GLM 5.3 (or --model) that leads a team: its
      Agent tool starts up to N agents (1 to 8), several at once, each on an
      OpenRouter model (mechanic and implementer on the GLM tier, the
      evidence collector on Inkling, anything else on the lead's model). A
      PreToolUse hook (`gate` below) holds the team to N and charges each
      spawn to the session budget with the same rules as a subagent. Prints
      the lead's report, then one `openclaude team:` line.
      Exit 0 report, 1 failed or timed out, 3 unavailable.

  openclaude_runner.py gate --team FILE --cwd DIR
      The team's PreToolUse hook (reads the hook payload on stdin). Not for
      direct use.

Configuration: the "openrouter" block of providers.json (endpoint, key, models)
as external_worker.py reads it, and the "orchestrator" block's model for the
team lead and general cards. OpenClaude reads its own ~/.openclaude config,
never ~/.claude; every value it needs is passed on the command line or in its
environment, so no OpenClaude setup is needed beyond installing it.
The key lives only in the child's environment and is masked in what is saved.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import external_coordinator as ec  # noqa: E402
import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402
import model_catalog  # noqa: E402

PACKAGE = "@gitlawb/openclaude"
INSTALL = f"npm install -g {PACKAGE}"
FANOUT_RUNGS = set(ew.EXTERNAL_RUNGS) | {"general"}
TEAM_RUNGS = ("mechanic", "implementer", "evidence-collector")  # the plugin agents a team lead can start
MAX_TEAM = 8
DEFAULT_PARALLEL = 4
TOOLS = ["Bash", "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"]
TOOLS_READ_ONLY = ["Bash", "Read", "Glob", "Grep", "WebFetch", "WebSearch"]
LEAD_TOOLS = ["Agent", "Bash", "Read", "Write", "Edit", "Glob", "Grep"]
DISALLOWED = ["Agent", "Task", "Skill", "NotebookEdit"]  # a worker never starts agents of its own
# Taken out of the child's environment: the Claude Code session it was started
# from, and any other OpenAI-compatible setup that would win over OpenRouter.
STRIP_PREFIXES = ("CLAUDE", "ANTHROPIC", "OPENAI_", "OPENGATEWAY", "GEMINI_", "GITHUB_COPILOT")
KEEP = {"CLAUDE_CONFIG_DIR"}  # harmless; OpenClaude ignores it
BASH_TIMEOUT_MS = "1800000"
# The key's usage counter trails a request by up to a minute: zero is "not yet", not "free".
NOT_YET = "OpenRouter's usage counter had not moved yet (session_summary.py prices these threads)"
TEAM_MEMORY = """
# Memory (this repository's Hindsight recall, filtered to this task)

{memory}

Every Task Card you write carries a `Memory:` line: copy the facts above that
name that card's files or its concern, verbatim, or write `Memory: none`.
Never paraphrase a fact and never add one that is not listed here.
"""
TEAM_NOTE = """
# You lead a team of OpenRouter agents

feature-ladder's openclaude_runner.py started you in OpenClaude on OpenRouter.
Start agents with the Agent tool, at most {n} in all: {rungs} (each runs on its
own OpenRouter model), or general-purpose for anything else. Start agents whose
work does not overlap in one message so they run at the same time, and give
each a Task Card (TASK id, Scope, Done when). A denied Agent call means the
team or session budget is spent: finish with what you have and say what is
left. Never start Claude Code, `claude -p` or a reviewer. Your last message is
your report: what each agent returned (its packet's first line), what changed,
the checks that ran, and anything unfinished.
"""


def openclaude_cmd():
    """The command that starts OpenClaude, as a list. An npm install on Windows is a
    .cmd shim that goes through cmd.exe (which cuts arguments at a newline), so the
    package's bin script is run with node directly when it can be found."""
    for name in ("openclaude", "openclaude.cmd", "openclaude.exe"):
        p = shutil.which(name)
        if not p:
            continue
        if p.lower().endswith((".cmd", ".ps1", ".bat")) or os.name == "nt":
            js = Path(p).parent / "node_modules" / "@gitlawb" / "openclaude" / "bin" / "openclaude"
            node = shutil.which("node")
            if js.is_file() and node:
                return [node, str(js)]
        return [p]
    return None


def version(cmd):
    try:
        r = subprocess.run(cmd + ["--version"], capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL, env=child_env({"openrouter": {"base_url": ""}}, "", ""))
        out = (r.stdout or "").strip().splitlines()
        return out[-1] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def base_url(cfg):
    """OpenRouter's OpenAI-compatible endpoint (the Anthropic one plus /v1)."""
    b = str(cfg["openrouter"].get("base_url") or "https://openrouter.ai/api").rstrip("/")
    return b if b.endswith("/v1") else b + "/v1"


def bare_model(model):
    return str(model or "").replace("[1m]", "")


def child_env(cfg, key, model, extra=None):
    env = {k: v for k, v in os.environ.items() if k in KEEP or not k.upper().startswith(STRIP_PREFIXES)}
    env.update({"CLAUDE_CODE_USE_OPENAI": "1", "OPENAI_BASE_URL": base_url(cfg) if key else "",
                "OPENAI_API_KEY": key, "OPENAI_MODEL": bare_model(model),
                "BASH_DEFAULT_TIMEOUT_MS": BASH_TIMEOUT_MS, "BASH_MAX_TIMEOUT_MS": BASH_TIMEOUT_MS,
                "FEATURE_LADDER_CHILD": "1",  # its hooks must not reset the session budget
                "FEATURE_LADDER_OPENCLAUDE": "1"})
    env.update(extra or {})
    return env


def config(cwd):
    cfg, o = ec.config(cwd)  # never Silk: --no-bs runs use external_worker.py
    return cfg, o


def rung_model(cfg, o, rung, override=None):
    if override:
        return override
    if rung == "general":
        return o["model"]
    effort = "high" if rung.endswith("-high") else None
    return ew.model_for(cfg, rung.replace("-high", ""), effort)


def role_text(rung):
    """The agent's instructions and the worker contract, paths filled in."""
    text = ec.agent_file(rung.replace("-high", "")) if rung != "general" else None
    role = ec.with_plugin_root(ec._strip_frontmatter(text)) if text else ""
    return ((role + "\n\n") if role else "") + ec.with_plugin_root(ew.contract_text())


def _blocked(task_id, why, kind):
    return ec._blocked(task_id, why, kind)


def unavailable(cfg, cwd):
    """Why nothing can start, or None."""
    if not openclaude_cmd():
        return f"openclaude not found on PATH (install it: {INSTALL})"
    key, source = ew.get_key(cfg, cwd)
    if not key:
        return f"no OpenRouter key ({source})"
    return None


# ---------------------------------------------------------------- fanout

def parse_fanout(argv):
    """Global flags anywhere; --card starts a card and the --id/--rung/--model after it are its own."""
    opts, cards, i = {}, [], 0
    per_card = {"--id", "--rung", "--model"}
    glob = {"--cwd", "--timeout", "--max-turns", "--parallel"}
    while i < len(argv):
        a = argv[i]
        val = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else None
        if a == "--card":
            if val is None:
                raise ValueError("--card needs a file")
            cards.append({"--card": val})
        elif a in per_card:
            if not cards:
                raise ValueError(f"{a} must follow a --card")
            if val is None:
                raise ValueError(f"{a} needs a value")
            cards[-1][a] = val
        elif a in glob:
            if val is None:
                raise ValueError(f"{a} needs a value")
            opts[a] = val
        else:
            raise ValueError(f"unknown option {a}")
        i += 2
    if not cards:
        raise ValueError("at least one --card FILE is required")
    return opts, cards


def run_card(oc, cfg, root, cwd, item, max_turns, timeout, key):
    """One card on one OpenClaude agent. Returns a result dict (the packet is in it)."""
    task_id, rung, model = item["id"], item.get("agent") or item["rung"], item["model"]
    read_only = rung in ew.READ_ONLY_RUNGS
    try:
        import session_budget  # noqa: E402
        budget = session_budget.status(session_budget.find_root(cwd))
    except Exception:  # noqa: BLE001
        budget = None
    card, added = ew.prepare_card(Path(item["card"]).read_text(encoding="utf-8-sig"), root, task_id,
                                  max_turns, timeout, budget, web=read_only, why=item.get("no_memory"))
    system_file = ec._write_temp(role_text(rung), f"fl-oc-{rung}-")
    tools = TOOLS_READ_ONLY if read_only else TOOLS
    disallowed = DISALLOWED + (["Edit", "Write", "MultiEdit"] if read_only else ["WebFetch", "WebSearch"])
    cmd = oc + ["-p", "--model", bare_model(model), "--output-format", "json", "--max-turns", str(max_turns),
                "--permission-mode", "acceptEdits", "--tools", ",".join(tools), "--allowedTools", ",".join(tools),
                "--disallowedTools", ",".join(disallowed), "--append-system-prompt-file", system_file,
                "--strict-mcp-config", "--no-session-persistence"]
    t0 = time.time()
    try:
        try:
            child_exit, stdout, stderr = ew._spawn(cmd, cwd, child_env(cfg, key, model), card, timeout)
        except OSError as e:
            child_exit, stdout, stderr = 127, "", f"could not start openclaude: {e}"
    finally:
        ec._unlink(system_file)
    stdout, stderr = ew.redact(stdout, key), ew.redact(stderr, key)
    secs = round(time.time() - t0)
    result = ew.parse_result(stdout) if (stdout or "").strip() else {}
    said = ew.unfence(str(result.get("result") or ""))
    timed_out = child_exit is None
    failed = timed_out or child_exit != 0 or bool(result.get("is_error"))
    try:
        turns = int(result.get("num_turns") or 0)
    except (TypeError, ValueError):
        turns = 0
    why = ew._last_lines(stderr) or ew._last_lines(said) or "no output"
    if said.startswith("TASK"):
        code, packet = 0, said
    elif timed_out:
        code, packet = 1, (f"TASK {task_id} - partial\nChecks run: none quoted\n"
                           f"Claims: OpenClaude agent timed out after {timeout}s and was stopped\n"
                           "Risks: work may be half-applied; verify.py will show scope changes\nRetained: none - timeout")
    elif failed and turns <= 1 and ew.UNAVAILABLE.search(f"{said}\n{stderr}"):
        code, packet = 3, _blocked(task_id, f"OpenRouter turned the OpenClaude agent away, nothing ran: {why}",
                                   "unavailable")
    elif failed or not said:
        code, packet = 1, (f"TASK {task_id} - partial\n{said or '(agent returned no text)'}\n"
                           f"Claims: OpenClaude agent failed (exit {child_exit}): {why}\n"
                           "Risks: work may be half-applied; verify.py will show scope changes")
    else:
        code, packet = 0, f"TASK {task_id} - partial\n{said}"
    if "Retained:" not in packet:
        packet += "\nRetained: none - agent did not report"
    if code in (1, 3) and not timed_out and turns <= 1 and item.get("charged"):
        try:
            import session_budget  # noqa: E402
            session_budget.refund(session_budget.find_root(cwd), rung, model, via="openclaude")
        except Exception:  # noqa: BLE001
            pass
    return {"task": task_id, "rung": rung, "model": model, "provider": "OpenRouter (OpenClaude)",
            "seconds": secs, "exit": child_exit, "timed_out": timed_out, "returned": code,
            "is_error": result.get("is_error"), "num_turns": result.get("num_turns"), "usage": result.get("usage"),
            "session_id": result.get("session_id"), "stderr_tail": (stderr or "")[-1500:], "card_added": added,
            "card": card, "packet": packet, "_result": result}


def fanout(argv):
    try:
        opts, cards = parse_fanout(argv)
    except ValueError as e:
        print(f"openclaude_runner: {e}", file=sys.stderr)
        return 1
    cwd = opts.get("--cwd") or os.getcwd()
    cfg, o = config(cwd)
    try:
        timeout = ec._int(opts, "--timeout", cfg["openrouter"]["timeout"])
        max_turns = ec._int(opts, "--max-turns", cfg["openrouter"]["max_turns"])
        parallel = ec._int(opts, "--parallel", DEFAULT_PARALLEL)
    except (TypeError, ValueError) as e:
        print(f"openclaude_runner: {e}", file=sys.stderr)
        return 1
    items = []
    for n, c in enumerate(cards, 1):
        rung = str(c.get("--rung") or "implementer").lower()
        if rung not in FANOUT_RUNGS and rung.replace("-high", "") not in FANOUT_RUNGS:
            print(f"openclaude_runner: rung {rung!r} does not run on OpenClaude (only "
                  f"{sorted(FANOUT_RUNGS | {'implementer-high'})})", file=sys.stderr)
            return 1
        if not Path(c["--card"]).is_file():
            print(f"openclaude_runner: card not found: {c['--card']}", file=sys.stderr)
            return 1
        items.append({"card": c["--card"], "id": c.get("--id") or f"T{n}", "rung": rung,
                      "agent": rung.replace("-high", ""), "model": rung_model(cfg, o, rung, c.get("--model"))})
    if len({i["id"] for i in items}) != len(items):
        print("openclaude_runner: every card needs its own --id", file=sys.stderr)
        return 1
    if ew.no_bs_requested(cwd):
        for i in items:
            print(_blocked(i["id"], "this run is --no-bs: its cheap tier is Silk Compute, which OpenClaude does "
                           "not reach here. Run the card with external_worker.py.", "unavailable") + "\n")
        return 3
    why = unavailable(cfg, cwd)
    if why:
        for i in items:
            print(_blocked(i["id"], f"OpenClaude unavailable: {why}. Run the card on its Claude rung.",
                           "unavailable") + "\n")
        return 3
    oc = openclaude_cmd()
    key, source = ew.get_key(cfg, cwd)
    root = ew.repo_root(cwd)
    import session_budget  # noqa: E402
    sroot = session_budget.find_root(cwd)
    out = {}
    runnable = []
    for i in items:  # the budget decides before anything starts
        if not i["model"]:
            out[i["id"]] = {"returned": 3, "packet": _blocked(i["id"], f"no OpenRouter model for {i['rung']} cards: "
                                                              "run it on its Claude rung", "unavailable")}
            continue
        ok, why = session_budget.check(sroot, i["agent"], i["model"], via="openclaude")
        if not ok:
            out[i["id"]] = {"returned": 4, "packet": _blocked(i["id"], why, "budget")}
            continue
        i["charged"] = True
        runnable.append(i)
    if runnable:  # one recall for the whole fanout (when the feature has none saved), before the cards start
        texts = [Path(i["card"]).read_text(encoding="utf-8-sig") for i in runnable]
        why = ew.ensure_recall(root, cwd, " / ".join(ew.card_title(t) for t in texts)[:300],
                               [p for t in texts for p in ew.scope_paths(ew._card_line(t, "Scope"))])
        for i in runnable:
            i["no_memory"] = why  # a card that gets no memory says why
    before = ew.usage_usd(cfg, key)
    ew.note_usage_baseline(root, before)
    t0 = time.time()
    if runnable:
        with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(runnable)))) as pool:
            futures = {i["id"]: pool.submit(run_card, oc, cfg, root, cwd, i, max_turns, timeout, key)
                       for i in runnable}
            for tid, f in futures.items():
                try:
                    out[tid] = f.result()
                except Exception as e:  # noqa: BLE001 - one card's crash must not lose the others' packets
                    out[tid] = {"returned": 1, "packet": f"TASK {tid} - partial\nClaims: runner error: {e}\n"
                                                         "Retained: none - runner error"}
    secs = round(time.time() - t0)
    after = ew.usage_usd(cfg, key)
    charged = round(after - before, 4) if before is not None and after is not None and after >= before else None
    ran = [i for i in items if "_result" in out.get(i["id"], {})]
    for i in items:  # packets first, in the order given: nothing below may lose one
        r = out[i["id"]]
        print(r["packet"])
        if "_result" in r:
            u = r.get("usage") or {}
            toks = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens"))
            print(f"\nopenclaude worker: {i['id']} {i['rung']} on {bare_model(i['model'])}, "
                  f"{r.get('num_turns', '?')} turns, {r['seconds']}s"
                  + (f", {toks:,} input tokens" if toks else ""))
        print()
    ladder_state.ensure_gitignore(root)
    # One charge covers every card: each card's share is in proportion to what its
    # tokens cost at the model's listed price (evenly when no price is known).
    group = f"openclaude-fanout-{int(t0)}"
    estimates = [model_catalog.estimate_usd(i["model"], out[i["id"]].get("usage") or {}) for i in ran]
    shares = model_catalog.share(charged, estimates)
    for i, mine in zip(ran, shares):
        r = out[i["id"]]
        result = r.pop("_result")
        result["openrouter_cost_usd"] = mine
        result["seconds"] = r.get("seconds")
        if len(ran) > 1:
            result["openrouter_cost_shared"] = True
            result["cost_group"] = group
        try:
            ladder_state.write_json(ladder_state.state_dir(root) / "evidence" / f"{ew._safe_id(i['id'])}-openclaude.json",
                                    dict(r, key_source=source, openrouter_share_usd=result["openrouter_cost_usd"],
                                         openrouter_cost_usd=charged if len(ran) == 1 else None))
        except OSError as e:
            print(f"openclaude_runner: evidence not written: {e}", file=sys.stderr)
        ew.record_ledger(root, i["id"], i["agent"], bare_model(i["model"]), result, "openclaude",
                         key=f"openclaude-{ew._safe_id(i['id'])}")
    if len(ran) > 1 and charged is not None:  # the whole charge, for the coordinator's own-share sum
        try:
            ladder_state.write_json(ladder_state.state_dir(root) / "evidence" / f"fanout-{int(t0)}-openclaude.json",
                                    {"role": "fanout", "cards": [i["id"] for i in ran], "seconds": secs,
                                     "openrouter_cost_usd": charged, "shares": dict(zip([i["id"] for i in ran], shares))})
        except OSError:
            pass
    codes = {out[i["id"]]["returned"] for i in items}
    cost = ("cost unknown" if charged is None else NOT_YET if charged == 0 and ran
            else f"OpenRouter charged ${charged:.4f} for all of them")
    print(f"openclaude fanout: {len(ran)} of {len(items)} cards ran on OpenRouter through OpenClaude, "
          f"{secs}s, {cost}")
    if codes == {0}:
        return 0
    for c in (1, 4, 3):
        if c in codes:
            return c
    return 1


# ---------------------------------------------------------------- team

def team_agents(cfg):
    """The plugin agents a lead can start, as OpenClaude's --agents JSON, and the
    routes that put each on its OpenRouter model."""
    agents, models, routing = {}, {}, {}
    for rung in TEAM_RUNGS:
        model = ew.model_for(cfg, rung)
        if not model:
            continue
        text = ec.agent_file(rung) or ""
        desc = re.search(r"^description:\s*(.+)$", text, re.M)
        read_only = rung in ew.READ_ONLY_RUNGS
        agents[rung] = {"description": (desc.group(1).strip().strip("'\"") if desc else rung)[:400],
                        "prompt": role_text(rung),
                        "tools": TOOLS_READ_ONLY if read_only else TOOLS}
        models[f"fl-{rung}"] = {"model": bare_model(model)}
        routing[rung] = f"fl-{rung}"
    return agents, {"agentModels": models, "agentRouting": routing}


def team_memory(root, cwd, prompt):
    """The lead's memory block: the feature's recall (made now when there is none),
    filtered to the files and words of the team's task. The lead copies from it
    onto each card, so every agent it starts conforms to the same recall."""
    import confidence  # noqa: E402
    title = ew.card_title(prompt)[:300]
    paths = list(dict.fromkeys(confidence.FILE_TOKEN.findall(prompt)))[:40]
    why = ew.ensure_recall(root, cwd, title, paths)
    facts, _ = ew.memory_line(root, " ".join(paths), title)
    if facts:
        return "\n".join(f"- {f}" for f in facts)
    return f"Memory: none ({why})" if why else "Memory: none (nothing stored about this task)"


def _hook_command(state_file, cwd):
    py = Path(sys.executable).as_posix()
    me = (HERE / "openclaude_runner.py").as_posix()
    return f'"{py}" "{me}" gate --team "{Path(state_file).as_posix()}" --cwd "{Path(cwd).as_posix()}"'


def team(args):
    cwd = args.get("--cwd") or os.getcwd()
    cfg, o = config(cwd)
    model = args.get("--model") or o["model"]
    prompt_file = args.get("--prompt-file")
    if not prompt_file or not Path(prompt_file).is_file():
        print("openclaude_runner: --prompt-file FILE is required", file=sys.stderr)
        return 1
    try:
        n = ec._int(args, "--agents", 0) if args.get("--agents") is not None else None
        timeout = ec._int(args, "--timeout", 1800)
        max_turns = ec._int(args, "--max-turns", 60)
    except ValueError as e:
        print(f"openclaude_runner: {e}", file=sys.stderr)
        return 1
    if not n or n > MAX_TEAM:
        print(f"openclaude_runner: --agents N is required, 1 to {MAX_TEAM}", file=sys.stderr)
        return 1
    why = unavailable(cfg, cwd)
    if why:
        print(f"openclaude team unavailable: {why}. Start the agents as subagents or external_worker.py cards.")
        return 3
    oc = openclaude_cmd()
    key, source = ew.get_key(cfg, cwd)
    root = ew.repo_root(cwd)
    t0 = time.time()
    state = ladder_state.state_dir(root) / "openclaude" / f"team-{int(t0)}-{os.getpid()}.json"
    ladder_state.write_json(state, {"max": n, "spawned": []})
    agents, settings = team_agents(cfg)
    settings["hooks"] = {"PreToolUse": [{"matcher": "Agent|Task", "hooks": [
        {"type": "command", "command": _hook_command(state, cwd), "timeout": 30}]}]}
    settings_file = ec._write_temp(json.dumps(settings), "fl-oc-settings-")
    prompt = Path(prompt_file).read_text(encoding="utf-8-sig")
    system_file = ec._write_temp(TEAM_NOTE.format(n=n, rungs=", ".join(agents) or "general-purpose")
                                 + TEAM_MEMORY.format(memory=team_memory(root, cwd, prompt)), "fl-oc-lead-")
    cmd = oc + ["-p", "--model", bare_model(model), "--output-format", "json", "--max-turns", str(max_turns),
                "--permission-mode", "acceptEdits", "--tools", ",".join(LEAD_TOOLS),
                "--allowedTools", ",".join(LEAD_TOOLS), "--settings", settings_file,
                "--agents", json.dumps(agents), "--append-system-prompt-file", system_file,
                "--strict-mcp-config", "--no-session-persistence"]
    before = ew.usage_usd(cfg, key)
    ew.note_usage_baseline(root, before)
    try:
        try:
            child_exit, stdout, stderr = ew._spawn(cmd, cwd, child_env(cfg, key, model), prompt, timeout)
        except OSError as e:
            child_exit, stdout, stderr = 127, "", f"could not start openclaude: {e}"
    finally:
        ec._unlink(settings_file)
        ec._unlink(system_file)
    stdout, stderr = ew.redact(stdout, key), ew.redact(stderr, key)
    secs = round(time.time() - t0)
    result = ew.parse_result(stdout) if (stdout or "").strip() else {}
    said = str(result.get("result") or "").strip()
    timed_out = child_exit is None
    failed = timed_out or child_exit != 0 or bool(result.get("is_error"))
    try:
        turns = int(result.get("num_turns") or 0)
    except (TypeError, ValueError):
        turns = 0
    why = ew._last_lines(stderr) or ew._last_lines(said) or "no output"
    spawned = (ladder_state.read_json(state) or {}).get("spawned") or []
    ec._unlink(state)  # the evidence record below keeps the spawns
    if failed and not timed_out and turns <= 1 and not spawned and ew.UNAVAILABLE.search(f"{said}\n{stderr}"):
        print(f"openclaude team unavailable, nothing ran: {why}. Start the agents another way.")
        return 3
    if timed_out:
        code, report = 1, (f"{said}\n\n" if said else "") + f"Team stopped: the lead ran past {timeout}s and was stopped."
    elif failed or not said:
        code, report = 1, (f"{said}\n\n" if said else "") + f"Team lead failed (exit {child_exit}): {why}."
    else:
        code, report = 0, said
    after = ew.usage_usd(cfg, key)
    charged = round(after - before, 4) if before is not None and after is not None and after >= before else None
    result["openrouter_cost_usd"] = charged
    result["seconds"] = secs
    print(report)
    tid = f"team-{int(t0)}"
    record = {"role": "team", "lead_model": model, "agents_max": n, "spawned": spawned,
              "routes": settings["agentRouting"] if "agentRouting" in settings else {},
              "provider": "OpenRouter (OpenClaude)", "key_source": source, "seconds": secs, "exit": child_exit,
              "timed_out": timed_out, "returned": code, "num_turns": result.get("num_turns"),
              "openrouter_cost_usd": charged, "session_id": result.get("session_id"),
              "stderr_tail": (stderr or "")[-1500:], "report": report}
    try:
        ladder_state.ensure_gitignore(root)
        ladder_state.write_json(ladder_state.state_dir(root) / "evidence" / f"{tid}-openclaude.json", record)
    except OSError as e:
        print(f"openclaude_runner: evidence not written: {e}", file=sys.stderr)
    ew.record_ledger(root, tid, "team", bare_model(model), result, "openclaude", key=f"openclaude-{tid}")
    cost = ("cost unknown" if charged is None else NOT_YET if charged == 0 and turns
            else f"OpenRouter charged ${charged:.4f} for the lead and its agents")
    print(f"\nopenclaude team: lead {bare_model(model)}, {len(spawned)} of {n} agents started, "
          f"{result.get('num_turns', '?')} turns, {secs}s, {cost}")
    return code


def _deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def gate_decision(payload, state_file, cwd):
    """None to allow the Agent call, or the hook's deny output."""
    if payload.get("tool_name") not in ("Agent", "Task"):
        return None
    ti = payload.get("tool_input") or {}
    agent = str(ti.get("subagent_type") or "general-purpose").split(":")[-1]
    model = ti.get("model")
    root = ew.repo_root(cwd)
    with ladder_state.locked(root, "openclaude-team"):
        st = ladder_state.read_json(Path(state_file)) or {}
        spawned = st.get("spawned") or []
        cap = int(st.get("max") or 0)
        if len(spawned) >= cap:
            return _deny(f"feature-ladder: this team's budget is {cap} agents and they are used. Finish with "
                         "what you have and say what is left.")
        import session_budget  # noqa: E402
        ok, why = session_budget.check(session_budget.find_root(cwd), agent, model, via="openclaude")
        if not ok:
            return _deny(why)
        spawned.append({"at": ew.now(), "agent": agent, "model": model})
        st["spawned"] = spawned
        ladder_state.write_json(Path(state_file), st)
    return None


def gate(argv):
    args = ew.parse(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    try:
        out = gate_decision(payload, args.get("--team"), args.get("--cwd") or payload.get("cwd") or os.getcwd())
    except Exception as e:  # noqa: BLE001 - a broken gate denies rather than lets the team run unbounded
        out = _deny(f"feature-ladder: the team budget could not be checked ({e}); finish with what you have.")
    if out:
        print(json.dumps(out))
    return 0


# ---------------------------------------------------------------- check

def check(argv):
    cfg, o = config(os.getcwd())
    oc = openclaude_cmd()
    status = {"openclaude": " ".join(oc) if oc else None, "version": version(oc) if oc else None,
              "endpoint": base_url(cfg), "lead_model": o["model"],
              "models": {r: rung_model(cfg, o, r) for r in ("mechanic", "implementer", "implementer-high",
                                                            "evidence-collector", "general")}}
    if not oc:
        status["error"] = f"openclaude not found on PATH (install it: {INSTALL})"
    key, source = ew.get_key(cfg)
    status["key"] = source if key else None
    if not key:
        status.setdefault("error", source)
        print(json.dumps(status, indent=2))
        return 3
    status["provider_key"] = ew.key_status(cfg, key)
    if "--probe" in argv:
        models = {bare_model(m) for m in status["models"].values() if m} | {bare_model(o["model"])}
        status["probes"] = {m: ew.probe_model(cfg, key, m) for m in sorted(models)}
    print(json.dumps(status, indent=2))
    ok = bool(oc) and status["provider_key"].get("valid") and all(r.get("ok") for r in status.get("probes", {}).values())
    return 0 if ok else 3


TEAM_FLAGS = {"--prompt-file", "--agents", "--model", "--cwd", "--timeout", "--max-turns"}


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "fanout":
        return fanout(argv[1:])
    if cmd == "team":
        args = ew.parse(argv[1:])
        unknown = sorted(set(args) - TEAM_FLAGS)
        if unknown:
            print(f"openclaude_runner: unknown option {', '.join(unknown)}", file=sys.stderr)
            return 1
        return team(args)
    if cmd == "gate":
        return gate(argv[1:])
    if cmd == "check":
        return check(argv[1:])
    print(__doc__)
    return 1


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
