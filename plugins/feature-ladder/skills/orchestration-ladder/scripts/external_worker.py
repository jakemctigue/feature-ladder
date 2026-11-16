#!/usr/bin/env python3
"""
feature-ladder external worker: run a Task Card on a non-Anthropic model
through OpenRouter, using Claude Code itself (`claude -p`) as the agent.

Why: cost scales with worker turns x context, and those tokens count against
the Claude plan. A child `claude -p` pointed at OpenRouter runs the same
tools, the same Evidence Packet contract and the same guard hook, but bills
OpenRouter credits (GLM 5.3 Flash: ~$0.15/M in, $0.50/M out) and leaves the
plan limit alone. Opus rungs and the Fable coordinator stay on Anthropic.

  external_worker.py check [--probe]
      CLI present? key readable? OpenRouter key valid (credits, limits)?
      --probe also sends one minimal request to each configured model.
      Exit 0 ok, 3 unavailable (install/sign-in/key), 1 error.

  external_worker.py run --card FILE --id T1 [--rung implementer|implementer-high|mechanic]
                         [--model ID] [--cwd DIR] [--timeout S] [--max-turns N]
      Runs the card. A card without a Memory: line gets one from the feature's
      saved recall.json (the file confidence.py scores), and every card gets a
      Bounds: line (turns, time, session budget) and the output contract inline.
      Prints the worker's Evidence Packet followed by one
      `external worker:` line (model, turns, cost). Writes
      .feature-ladder/evidence/<id>-external.json and a ledger row, both in
      the main checkout (a linked worktree shares them).
      Exit 0 packet returned,
           3 unavailable: no CLI, no key, or OpenRouter rejected the key or
             has no credits (the packet says "blocked"; run the card as a
             subagent instead),
           1 the child failed or timed out without a packet (the packet says
             "partial" and quotes the child's last error lines),
           4 refused by the session subagent budget (session_budget.py).
      A worker that never ran (exit 3, or exit 1 before its first turn) does
      not use a session budget slot.
      --timeout defaults to "timeout" in the configuration (1500 s). That is
      longer than one foreground tool call may last (600 s), so run this
      command in the background (the Bash tool's run_in_background), or pass
      --timeout 540 to keep it inside one call. At the limit the child and
      everything it started are killed and a "partial" packet is printed.

  external_worker.py config
      Prints the merged configuration (never the key).

Configuration, merged in this order (later wins):
  built-in defaults
  ~/.claude/feature-ladder/providers.json
  <repo>/.feature-ladder/providers.json
  env FEATURE_LADDER_WORKERS=openrouter|claude, FEATURE_LADDER_WORKER_MODEL=<id>

  {
    "workers": "auto",                           # default: GLM-tier cards run here when the
                                                 # CLI and a key are present; "openrouter"
                                                 # the same; "claude" turns the tier off
    "openrouter": {
      "preset": "glm",                           # or "jev" (typesafe/jev-router); a preset
                                                 # replaces "models"
      "base_url": "https://openrouter.ai/api",
      "key_env": "OPENROUTER_API_KEY",           # environment variable, if set
      "key_secret": "OPENROUTER_API_KEY",        # else Google Secret Manager name
      "models": {
        "mechanic":         "z-ai/glm-5.3-flash:nitro",
        "implementer":      "z-ai/glm-5.3-flash:nitro",
        "implementer-high": "z-ai/glm-5.3"
      },
      "context_1m": true,                        # append [1m] so Claude Code allows 1M context
      "max_turns": 40,
      "timeout": 1500
    }
  }
The key is never written anywhere; it lives only in the child's environment.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ladder_state  # noqa: E402

DEFAULTS = {
    "workers": "auto",
    "openrouter": {
        "base_url": "https://openrouter.ai/api",
        "key_env": "OPENROUTER_API_KEY",
        "key_secret": "OPENROUTER_API_KEY",
        "models": {
            "mechanic": "z-ai/glm-5.3-flash:nitro",
            "implementer": "z-ai/glm-5.3-flash:nitro",
            "implementer-high": "z-ai/glm-5.3",
        },
        "context_1m": True,
        "max_turns": 40,
        "timeout": 1500,  # longer than a foreground tool call: the coordinator runs this in the background
    },
}
# Presets for the cheap tier. "glm" is the default; "jev" hands each request to
# TypeSafe's Jev Router, which picks the model and effort per request (variable
# price; the ledger shows what each card cost).
PRESETS = {
    "glm": {"mechanic": "z-ai/glm-5.3-flash:nitro", "implementer": "z-ai/glm-5.3-flash:nitro",
            "implementer-high": "z-ai/glm-5.3"},
    "jev": {"mechanic": "typesafe/jev-router", "implementer": "typesafe/jev-router",
            "implementer-high": "typesafe/jev-router"},
}
EXTERNAL_RUNGS = {"mechanic", "implementer"}  # never senior-implementer, experts, Fable, scribe, codex-reviewer
# intake.py card sends a card here only when the router's provider is "glm"
# (T0 and implementer/medium with score <= route.GLM_MAX_SCORE).
TOOLS = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,mcp__plugin_feature-ladder_hindsight__retain,mcp__plugin_feature-ladder_hindsight__recall"
DISALLOWED = "Agent,Task,Skill,WebSearch,WebFetch,NotebookEdit"
# The child's own words when OpenRouter turned the request away: nothing ran, so the
# card should fall back to a subagent (exit 3) instead of being reported as an error.
UNAVAILABLE = re.compile(r"\b(401|402|403)\b|no auth credentials|invalid (x-)?api[- ]key|unauthori[sz]ed|"
                         r"authentication[_ ]error|insufficient credits|payment required|credit balance", re.I)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root(cwd):
    """Where .feature-ladder/ lives: the main checkout (a linked worktree maps to it),
    the same folder verify.py, the spawn rules and the hooks use."""
    return Path(ladder_state.state_root(cwd))


def _merge(a, b):
    out = dict(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(cwd):
    cfg = json.loads(json.dumps(DEFAULTS))
    for f in (Path.home() / ".claude" / "feature-ladder" / "providers.json",
              repo_root(cwd) / ".feature-ladder" / "providers.json"):
        if f.is_file():
            try:
                cfg = _merge(cfg, json.loads(f.read_text(encoding="utf-8-sig")))
            except ValueError as e:
                print(f"external_worker: ignoring {f}: {e}", file=sys.stderr)
    preset = str(cfg["openrouter"].get("preset") or "").lower()
    if preset in PRESETS:
        cfg["openrouter"]["models"] = dict(PRESETS[preset])
    if os.environ.get("FEATURE_LADDER_WORKERS"):
        cfg["workers"] = os.environ["FEATURE_LADDER_WORKERS"].strip().lower()
    if os.environ.get("FEATURE_LADDER_WORKER_MODEL"):
        m = os.environ["FEATURE_LADDER_WORKER_MODEL"].strip()
        cfg["openrouter"]["models"] = {k: m for k in cfg["openrouter"]["models"]}
    return cfg


def model_for(cfg, rung, effort=None, override=None):
    if override:
        return override
    models = cfg["openrouter"]["models"]
    key = rung
    if rung == "implementer" and (effort or "").lower() == "high":
        key = "implementer-high"
    return models.get(key) or models.get("implementer")


def with_context_marker(cfg, model):
    if cfg["openrouter"].get("context_1m") and not model.endswith("[1m]"):
        return model + "[1m]"
    return model


def claude_bin():
    for n in ("claude", "claude.cmd", "claude.exe"):
        p = shutil.which(n)
        if p:
            return p
    return None


def available(cfg):
    """Cheap readiness check with no network and no secret fetch: the CLI is
    on PATH and a key source exists (the env var, or gcloud for the secret)."""
    if not claude_bin():
        return False, "claude CLI not found on PATH"
    env_name = cfg["openrouter"].get("key_env")
    if env_name and os.environ.get(env_name):
        return True, f"env {env_name}"
    if cfg["openrouter"].get("key_secret"):
        try:
            import with_secrets  # noqa: E402
            if with_secrets.gcloud():
                return True, "Secret Manager " + cfg["openrouter"]["key_secret"]
        except Exception:  # noqa: BLE001
            pass
        return False, "no OPENROUTER_API_KEY in the environment and no gcloud CLI for the secret"
    return False, "no key_env or key_secret configured"


def get_key(cfg):
    env_name = cfg["openrouter"].get("key_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip(), f"env {env_name}"
    name = cfg["openrouter"].get("key_secret")
    if name:
        try:
            import with_secrets  # noqa: E402
            exe = with_secrets.gcloud()
            if exe:
                val, err = with_secrets.fetch(exe, name, with_secrets.project(None))
                if val:
                    return val.strip(), f"Secret Manager {name}"
                return None, f"Secret Manager {name}: {' '.join(err or [])}"
            return None, "gcloud CLI not found"
        except Exception as e:  # noqa: BLE001
            return None, f"secret lookup failed: {e}"
    return None, "no key_env or key_secret configured"


def key_status(cfg, key):
    """GET /api/v1/key: validates the key and reports limits. Never prints the key."""
    req = urllib.request.Request(cfg["openrouter"]["base_url"].rstrip("/") + "/v1/key",
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8")).get("data", {})
        return {"valid": True, "usage_usd": d.get("usage"),
                "limit_usd": d.get("limit"), "limit_remaining_usd": d.get("limit_remaining"),
                "is_free_tier": d.get("is_free_tier")}
    except urllib.error.HTTPError as e:
        return {"valid": False, "http": e.code, "error": e.read().decode("utf-8", "replace")[:200]}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"valid": False, "error": str(e)[:200]}


def probe_model(cfg, key, model):
    body = json.dumps({"model": model.replace("[1m]", ""), "max_tokens": 1,
                       "messages": [{"role": "user", "content": "ok"}]}).encode()
    req = urllib.request.Request(cfg["openrouter"]["base_url"].rstrip("/") + "/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        return {"ok": True, "provider": d.get("provider"), "model": d.get("model"),
                "seconds": round(time.time() - t0, 2)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "http": e.code, "error": e.read().decode("utf-8", "replace")[:200]}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"ok": False, "error": str(e)[:200]}


def child_env(cfg, key, model):
    env = dict(os.environ)
    for k in list(env):
        if k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"):
            env.pop(k, None)
    env["ANTHROPIC_BASE_URL"] = cfg["openrouter"]["base_url"]
    env["ANTHROPIC_AUTH_TOKEN"] = key
    env["ANTHROPIC_API_KEY"] = ""  # OpenRouter's docs: must be set and empty
    env["FEATURE_LADDER_CHILD"] = "1"  # the child's hooks must not reset the session budget
    m = with_context_marker(cfg, model)
    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = m
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = m
    env["ANTHROPIC_MODEL"] = m
    env["FEATURE_LADDER_EXTERNAL_WORKER"] = "1"
    return env


# The worker contract: the plugin's skill file, or worker-contract.md beside this
# script (the Claude app keeps every downloaded file in one folder).
CONTRACT_FILES = [HERE.parents[1] / "worker-contract" / "SKILL.md", HERE / "worker-contract.md"]


def contract_text():
    for f in CONTRACT_FILES:
        try:
            txt = f.read_text(encoding="utf-8")
            return re.sub(r"^---.*?---\s*", "", txt, count=1, flags=re.S).strip()
        except OSError:
            continue
    # Neither file is here: the rules that matter most, and the packet's exact shape.
    return ("Follow the Task Card exactly. Edit only the files in Scope; run every 'Done when' command and "
            "quote its exit code; say 'unverified' for anything you did not check; no subagents, no web.\n\n"
            + OUTPUT_CONTRACT)


OUTPUT_CONTRACT = """Output contract (reply with this and nothing else):
TASK <id> - <status: done | blocked | partial>
Changed: <file list with line ranges>
Checks run: <command -> exit code -> last 5 lines>
Claims: <each factual claim with how it was verified; "unverified" is allowed>
Risks: <anything you are unsure about>
Memory candidates: <"[kind] fact" lines, or "none">
Retained: deferred - <n> candidates   |   Retained: none - <reason>"""
MEMORY_SNIPPETS = 5


def _card_line(card, name):
    m = re.search(rf"^{name}:[ \t]*(.*)$", card, re.M)
    return None if m is None else m.group(1).strip()


def recall_file(root):
    """The feature's saved recall: runs/<slug>/recall.json (plugin) or recall.json (app)."""
    fl = ladder_state.state_dir(root)
    run = ladder_state.read_json(fl / "run.json")
    if not isinstance(run, dict):
        run = {}
    for f in ([fl / "runs" / run["slug"] / "recall.json"] if run.get("slug") else []) + [fl / "recall.json"]:
        if f.is_file():
            return f
    return None


def memory_line(root, scope):
    """(snippets, relevant) from the saved recall: memories that touch the card's
    scope first, at most MEMORY_SNIPPETS, verbatim as '[kind] fact'."""
    f = recall_file(root)
    if not f:
        return [], 0
    try:
        import confidence
        mems = confidence.parse_memories(f.read_text(encoding="utf-8-sig"))
        anch = confidence.anchors("", [p.strip() for p in re.split(r"[,\s]+", scope or "") if p.strip()])
    except Exception:  # noqa: BLE001 - a bad recall file is "no memory", never a failed card
        return [], 0

    rel = [m for m in mems if confidence.touching(m["text"], anch)]
    picked = (rel + [m for m in mems if m not in rel])[:MEMORY_SNIPPETS]
    return [f"[{m['kind']}] {m['text'][:160]}" for m in picked], len(rel)


def prepare_card(card, root, task_id, max_turns, timeout, budget=None):
    """The queue card the external worker is handed: the coordinator's card with
    a Memory: line from the saved recall when it has none, a Bounds: line and the
    output contract inline (cheap models weigh the card over the system prompt)."""
    card = card.rstrip()
    added = []
    mem = _card_line(card, "Memory")
    if mem is None or not mem or mem.lower() in ("none", "n/a", "-"):
        snippets, relevant = memory_line(root, _card_line(card, "Scope"))
        if snippets and (mem is None or not mem or relevant):
            text = "Memory: " + " | ".join(snippets)
            card = re.sub(r"^Memory:.*$", lambda _: text, card, count=1, flags=re.M) if mem is not None else card + "\n" + text
            added.append("memory")
        elif mem is None:
            card += "\nMemory: none"
            added.append("memory")
    if _card_line(card, "Bounds") is None:
        b = f"Bounds: at most {max_turns} turns and {timeout}s; edit only Scope; no subagents, no web"
        if budget and budget.get("left") is not None:
            b += f"; session subagent budget {budget['left']} of {budget.get('max')} left"
        card += "\n" + b
        added.append("bounds")
    if "Retained:" not in card:
        card += "\n\n" + OUTPUT_CONTRACT.replace("<id>", str(task_id), 1)
        added.append("contract")
    return card + "\n", added


def record_ledger(root, task_id, rung, model, result):
    """One row per card in runs/<slug>/usage.jsonl (the file hooks/scripts/ledger.py
    reports from). Written here directly, so it also works where the hook script is
    not installed (the Claude app). A line that cannot be read is kept, not fatal."""
    try:
        run = ladder_state.read_json(ladder_state.state_dir(root) / "run.json")
        if not isinstance(run, dict):
            return
        usage = result.get("usage") or {}
        entry = {"at": now(), "key": f"external-{task_id}", "slug": run.get("slug"),
                 "agent": f"{rung} (openrouter)", "rung": rung, "model": model, "effort": None,
                 "task": task_id, "turns": int(result.get("num_turns") or 0),
                 "input": int(usage.get("input_tokens") or 0),
                 "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                 "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
                 "output": int(usage.get("output_tokens") or 0),
                 "cost_usd": result.get("openrouter_cost_usd")}
        if result.get("openrouter_cost_shared"):
            entry["cost_shared"] = True  # other external cards ran on the key at the same time
        entry["total"] = entry["input"] + entry["cache_read"] + entry["cache_write"] + entry["output"]
        out = ladder_state.state_dir(root) / "runs" / (run.get("slug") or "no-slug") / "usage.jsonl"
        with ladder_state.locked(root, "ledger"):
            lines = []
            for line in (out.read_text(encoding="utf-8").splitlines() if out.is_file() else []):
                if not line.strip():
                    continue
                try:
                    if json.loads(line).get("key") == entry["key"]:
                        continue  # this card ran before: replace its row
                except (ValueError, AttributeError):
                    pass
                lines.append(line)
            lines.append(json.dumps(entry, sort_keys=True))
            ladder_state.write_text(out, "\n".join(lines) + "\n")
    except Exception:  # noqa: BLE001 - the ledger is best effort
        pass


def unfence(text):
    """Models often wrap the packet in a ``` fence; the packet starts at TASK."""
    t = text.strip()
    m = re.search(r"^TASK\b.*", t, re.M)
    if m:
        t = t[m.start():]
        t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


def usage_usd(cfg, key):
    """OpenRouter's own usage counter for this key; the difference around a
    run is the real charge (Claude Code's total_cost_usd assumes Anthropic prices)."""
    try:
        s = key_status(cfg, key)
        return float(s["usage_usd"]) if s.get("valid") and s.get("usage_usd") is not None else None
    except (TypeError, ValueError):
        return None


def parse_result(stdout):
    """`claude -p --output-format json` prints one result object on some
    versions and a list of messages (result last) on others."""
    try:
        data = json.loads(stdout)
    except ValueError:
        # fall back to the last JSON object on its own line
        for line in reversed(stdout.splitlines()):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
        else:
            return {"result": stdout.strip(), "unparsed": True}
    if isinstance(data, list):
        results = [m for m in data if isinstance(m, dict) and m.get("type") == "result"]
        data = results[-1] if results else (data[-1] if data and isinstance(data[-1], dict) else {})
    return data if isinstance(data, dict) else {"result": str(data), "unparsed": True}


def _spawn(cmd, cwd, env, card, timeout):
    """Run the child with the card on stdin. (exit code, stdout, stderr); the exit code
    is None when the timeout was reached, after the child's whole process tree was killed."""
    return ladder_state.run_tree(cmd, cwd=cwd, timeout=timeout, env=env, input=card)


def _safe_id(task_id):
    """The id as a file name, the same way verify.evidence_path spells it."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in str(task_id)) or "task"


def _refund(cwd, agent, model):
    """Give the session budget slot back: the worker never ran."""
    try:
        import session_budget  # noqa: E402
        root = session_budget.find_root(cwd)
        if hasattr(session_budget, "refund"):
            session_budget.refund(root, agent, model, via="external")
            return
        sess = session_budget.load(root)
        spawned = sess.get("spawned") or []
        for i in range(len(spawned) - 1, -1, -1):
            e = spawned[i]
            if e.get("via") == "external" and e.get("agent") == agent and e.get("model") == model:
                del spawned[i]
                session_budget.save(root, sess)
                return
    except Exception:  # noqa: BLE001 - budget bookkeeping must not break a worker
        pass


def _others_ran(root, safe, since, window):
    """True when another external card is running, or finished since `since`: the key's
    usage counter then covers their spend too, so the delta is not this card's alone."""
    d = ladder_state.state_dir(root)
    now_ = time.time()
    try:
        for f in d.glob("external-*.lock"):
            if f.name != f"external-{safe}.lock" and now_ - f.stat().st_mtime < window:
                return True
        for f in (d / "evidence").glob("*-external.json"):
            if f.name != f"{safe}-external.json" and f.stat().st_mtime >= since:
                return True
    except OSError:
        pass
    return False


def _last_lines(text, n=3):
    lines = [l.strip() for l in str(text or "").splitlines() if l.strip()]
    return " | ".join(lines[-n:])[:400]


def _int_arg(args, name, default):
    v = args.get(name)
    if v is None:
        return int(default)
    if v is True or not str(v).strip().isdigit() or int(v) < 1:
        raise ValueError(f"{name} needs a whole number of 1 or more")
    return int(v)


def run(args):
    cwd = args.get("--cwd") or os.getcwd()
    cfg = load_config(cwd)
    card_path = args.get("--card")
    task_id = args.get("--id") or "task"
    rung = (args.get("--rung") or "implementer").lower()
    effort = "high" if rung.endswith("-high") else None
    rung = rung.replace("-high", "")
    if rung not in EXTERNAL_RUNGS:
        print(f"external_worker: rung {rung!r} never runs externally (only {sorted(EXTERNAL_RUNGS)})", file=sys.stderr)
        return 1
    if not card_path or not Path(card_path).is_file():
        print("external_worker: --card FILE is required", file=sys.stderr)
        return 1
    exe = claude_bin()
    if not exe:
        print("external_worker: claude CLI not found on PATH", file=sys.stderr)
        return 3
    key, source = get_key(cfg)
    if not key:
        print(f"external_worker: no OpenRouter key ({source})", file=sys.stderr)
        return 3
    model = model_for(cfg, rung, effort, args.get("--model"))
    try:
        timeout = _int_arg(args, "--timeout", cfg["openrouter"]["timeout"])
        max_turns = _int_arg(args, "--max-turns", cfg["openrouter"]["max_turns"])
    except (TypeError, ValueError) as e:
        print(f"external_worker: {e}", file=sys.stderr)
        return 1
    slot_taken = False
    try:
        import session_budget  # noqa: E402
        ok, why = session_budget.check(session_budget.find_root(cwd), rung, model, via="external")
        slot_taken = ok
    except Exception:  # noqa: BLE001 - budget bookkeeping must not break a worker
        ok, why = True, ""
    if not ok:
        print(f"TASK {task_id} - blocked\nChanged: none\nChecks run: none\nClaims: {why}\n"
              "Risks: none\nMemory candidates: none\nRetained: none - budget")
        return 4
    root = repo_root(cwd)
    safe = _safe_id(task_id)
    try:
        budget = session_budget.status(session_budget.find_root(cwd))
    except Exception:  # noqa: BLE001
        budget = None
    card, added = prepare_card(Path(card_path).read_text(encoding="utf-8-sig"), root, task_id,
                               max_turns, timeout, budget)
    # The contract goes in a file: an npm-installed `claude.cmd` passes its arguments
    # through cmd.exe, which cuts a multi-line argument at the first newline.
    fd, contract_file = tempfile.mkstemp(prefix="fl-contract-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(contract_text() + "\n")
    cmd = [exe, "-p", "--model", "sonnet", "--output-format", "json", "--max-turns", str(max_turns),
           "--permission-mode", "acceptEdits", "--allowedTools", TOOLS, "--disallowedTools", DISALLOWED,
           "--append-system-prompt-file", contract_file]
    usage_before = usage_usd(cfg, key)
    marker = ladder_state.state_dir(root) / f"external-{safe}.lock"  # "this card is running", for the cost note
    t0 = time.time()
    shared = _others_ran(root, safe, t0, timeout + 120)
    try:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        try:
            child_exit, stdout, stderr = _spawn(cmd, cwd, child_env(cfg, key, model), card, timeout)
        except OSError as e:
            child_exit, stdout, stderr = 127, "", f"could not start {exe}: {e}"
    finally:
        for f in (Path(contract_file), marker):
            try:
                f.unlink()
            except OSError:
                pass
    secs = round(time.time() - t0)
    timed_out = child_exit is None
    result = parse_result(stdout) if stdout.strip() else {}
    said = unfence(str(result.get("result") or ""))
    has_packet = said.startswith("TASK")
    failed = timed_out or child_exit != 0 or bool(result.get("is_error"))
    why_failed = _last_lines(stderr) or _last_lines(said) or "no output"
    # The exit code is decided from what the child did, before any packet is made up for it.
    if has_packet:
        code, packet = 0, said
    elif timed_out:
        code = 1
        packet = (f"TASK {task_id} - partial\nChecks run: none quoted\n"
                  f"Claims: external worker timed out after {timeout}s and was stopped\n"
                  "Risks: work may be half-applied; verify.py will show scope changes\nRetained: none - timeout")
    elif failed and UNAVAILABLE.search(f"{said}\n{stderr}"):
        code = 3
        packet = (f"TASK {task_id} - blocked\nChanged: none\nChecks run: none\n"
                  f"Claims: external worker unavailable, nothing ran: {why_failed}\n"
                  "Risks: none\nMemory candidates: none\nRetained: none - unavailable")
    elif failed or not said:
        code = 1
        packet = (f"TASK {task_id} - partial\n{said or '(worker returned no text)'}\n"
                  f"Claims: external worker failed (exit {child_exit}): {why_failed}\n"
                  "Risks: work may be half-applied; verify.py will show scope changes")
    else:  # it answered, but not in the contract's shape: hand the text on as a partial packet
        code, packet = 0, f"TASK {task_id} - partial\n{said}"
    if "Retained:" not in packet:
        packet += "\nRetained: none - external worker did not report"
    usage_after = usage_usd(cfg, key)
    charged = (round(usage_after - usage_before, 4) if usage_before is not None and usage_after is not None else None)
    if charged is not None and (shared or _others_ran(root, safe, t0, timeout + 120)):
        result["openrouter_cost_shared"] = True
    result["openrouter_cost_usd"] = charged  # what OpenRouter billed; total_cost_usd is Claude Code's Anthropic-price guess
    try:
        turns = int(result.get("num_turns") or 0)
    except (TypeError, ValueError):
        turns = 0
    if slot_taken and code in (1, 3) and not timed_out and turns <= 1 and not charged:
        _refund(cwd, rung, model)  # it never ran: the slot goes back
    print(packet)  # first: nothing below may lose a packet that was paid for
    record = {"task": task_id, "rung": rung, "model": model, "key_source": source, "seconds": secs,
              "exit": child_exit, "timed_out": timed_out, "returned": code,
              "is_error": result.get("is_error"), "num_turns": result.get("num_turns"),
              "usage": result.get("usage"), "openrouter_cost_usd": charged,
              "openrouter_cost_shared": bool(result.get("openrouter_cost_shared")),
              "claude_code_estimate_usd": result.get("total_cost_usd"),
              "session_id": result.get("session_id"), "stderr_tail": (stderr or "")[-1500:], "card_added": added, "card": card, "packet": packet}
    try:
        ladder_state.ensure_gitignore(root)
        ladder_state.write_json(ladder_state.state_dir(root) / "evidence" / f"{safe}-external.json", record)
    except OSError as e:
        print(f"external_worker: evidence not written: {e}", file=sys.stderr)
    record_ledger(root, task_id, rung, model, result)
    u = result.get("usage") or {}
    toks = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
    if charged is None:
        cost_txt = "cost not available"
    elif result.get("openrouter_cost_shared"):
        cost_txt = f"OpenRouter charged ${charged:.4f} on this key meanwhile (other external cards ran too: their spend is in it)"
    else:
        cost_txt = f"OpenRouter charged ${charged:.4f}"
    print(f"\nexternal worker: {model} via OpenRouter, {result.get('num_turns', '?')} turns, {secs}s, "
          f"{toks:,} input tokens, {int(u.get('output_tokens') or 0):,} output, {cost_txt}")
    return code


def check(args):
    cfg = load_config(os.getcwd())
    status = {"workers": cfg["workers"], "preset": cfg["openrouter"].get("preset") or "glm",
              "claude_cli": claude_bin() is not None,
              "models": cfg["openrouter"]["models"]}
    key, source = get_key(cfg)
    status["key"] = source if key else None
    if not key:
        status["error"] = source
        print(json.dumps(status, indent=2))
        return 3
    status["openrouter_key"] = key_status(cfg, key)
    if "--probe" in args:
        status["probes"] = {m: probe_model(cfg, key, m) for m in sorted(set(cfg["openrouter"]["models"].values()))}
    print(json.dumps(status, indent=2))
    ok = status["claude_cli"] and status["openrouter_key"].get("valid") and \
        all(p.get("ok") for p in status.get("probes", {}).values())
    return 0 if ok else 3


RUN_FLAGS = {"--card", "--id", "--rung", "--model", "--cwd", "--timeout", "--max-turns"}


def parse(argv):
    a, i = {}, 0
    while i < len(argv):
        if argv[i].startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                a[argv[i]] = argv[i + 1]
                i += 2
                continue
            a[argv[i]] = True
        i += 1
    return a


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "run":
        args = parse(argv[1:])
        unknown = sorted(set(args) - RUN_FLAGS)
        if unknown:  # a misspelt flag used to be dropped without a word
            print(f"external_worker: unknown option {', '.join(unknown)} (run takes {', '.join(sorted(RUN_FLAGS))})",
                  file=sys.stderr)
            return 1
        return run(args)
    if argv[0] == "check":
        return check(argv[1:])
    if argv[0] == "config":
        print(json.dumps(load_config(os.getcwd()), indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
