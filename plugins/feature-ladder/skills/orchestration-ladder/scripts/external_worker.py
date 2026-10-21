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
      Runs the card. Prints the worker's Evidence Packet followed by one
      `external worker:` line (model, turns, cost). Writes
      .feature-ladder/evidence/<id>-external.json and a ledger row.
      Exit 0 packet returned, 3 unavailable, 1 error/timeout.

  external_worker.py config
      Prints the merged configuration (never the key).

Configuration, merged in this order (later wins):
  built-in defaults
  ~/.claude/feature-ladder/providers.json
  <repo>/.feature-ladder/providers.json
  env FEATURE_LADDER_WORKERS=openrouter|claude, FEATURE_LADDER_WORKER_MODEL=<id>

  {
    "workers": "openrouter",                     # or "claude" (in-session subagents)
    "openrouter": {
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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2] / "hooks" / "scripts"))

DEFAULTS = {
    "workers": "claude",
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
        "timeout": 1500,
    },
}
EXTERNAL_RUNGS = {"mechanic", "implementer"}  # never senior-implementer, experts, Fable, scribe, codex-reviewer
TOOLS = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,mcp__plugin_feature-ladder_hindsight__retain,mcp__plugin_feature-ladder_hindsight__recall"
DISALLOWED = "Agent,Task,Skill,WebSearch,WebFetch,NotebookEdit"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root(cwd):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except OSError:
        pass
    return Path(cwd).resolve()


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
    m = with_context_marker(cfg, model)
    for tier in ("OPUS", "SONNET", "HAIKU", "FABLE"):
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = m
    env["CLAUDE_CODE_SUBAGENT_MODEL"] = m
    env["ANTHROPIC_MODEL"] = m
    env["FEATURE_LADDER_EXTERNAL_WORKER"] = "1"
    return env


def contract_text():
    f = HERE.parents[1] / "worker-contract" / "SKILL.md"
    try:
        txt = f.read_text(encoding="utf-8")
        return re.sub(r"^---.*?---\s*", "", txt, count=1, flags=re.S).strip()
    except OSError:
        return "Follow the Task Card exactly and end with an Evidence Packet whose last line starts with 'Retained:'."


def record_ledger(root, task_id, rung, model, result):
    try:
        import ledger  # noqa: E402
        _, run_file = ledger.find_run(str(root))
        if not run_file:
            return
        run = json.loads(run_file.read_text(encoding="utf-8"))
        usage = result.get("usage") or {}
        entry = {"at": now(), "key": f"external-{task_id}", "slug": run.get("slug"),
                 "agent": f"{rung} (openrouter)", "rung": rung, "model": model, "effort": None,
                 "task": task_id, "turns": int(result.get("num_turns") or 0),
                 "input": int(usage.get("input_tokens") or 0),
                 "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                 "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
                 "output": int(usage.get("output_tokens") or 0),
                 "cost_usd": result.get("total_cost_usd")}
        entry["total"] = entry["input"] + entry["cache_read"] + entry["cache_write"] + entry["output"]
        out = ledger.ledger_path(root, run.get("slug"))
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [l for l in (out.read_text(encoding="utf-8").splitlines() if out.is_file() else [])
                 if not (l.strip() and json.loads(l).get("key") == entry["key"])]
        lines.append(json.dumps(entry, sort_keys=True))
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - the ledger is best effort
        pass


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
    timeout = int(args.get("--timeout") or cfg["openrouter"]["timeout"])
    max_turns = int(args.get("--max-turns") or cfg["openrouter"]["max_turns"])
    card = Path(card_path).read_text(encoding="utf-8-sig")
    cmd = [exe, "-p", "--model", "sonnet", "--output-format", "json", "--max-turns", str(max_turns),
           "--permission-mode", "acceptEdits", "--allowedTools", TOOLS, "--disallowedTools", DISALLOWED,
           "--append-system-prompt", contract_text()]
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=cwd, env=child_env(cfg, key, model), input=card, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"TASK {task_id} - partial\nChecks run: none quoted\nClaims: external worker timed out after {timeout}s\n"
              f"Risks: work may be half-applied; verify.py will show scope changes\nRetained: none - timeout")
        return 1
    secs = round(time.time() - t0)
    result = parse_result(r.stdout)
    packet = str(result.get("result") or "").strip()
    if not packet.startswith("TASK"):
        packet = f"TASK {task_id} - partial\n{packet or '(worker returned no text)'}"
    if "Retained:" not in packet:
        packet += "\nRetained: none - external worker did not report"
    root = repo_root(cwd)
    ev = root / ".feature-ladder" / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    record = {"task": task_id, "rung": rung, "model": model, "key_source": source, "seconds": secs,
              "exit": r.returncode, "is_error": result.get("is_error"), "num_turns": result.get("num_turns"),
              "usage": result.get("usage"), "total_cost_usd": result.get("total_cost_usd"),
              "session_id": result.get("session_id"), "stderr_tail": (r.stderr or "")[-1500:], "packet": packet}
    (ev / f"{task_id}-external.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    record_ledger(root, task_id, rung, model, result)
    print(packet)
    cost = result.get("total_cost_usd")
    print(f"\nexternal worker: {model} via OpenRouter, {result.get('num_turns', '?')} turns, {secs}s, "
          f"cost ${cost:.4f}" if isinstance(cost, (int, float)) else
          f"\nexternal worker: {model} via OpenRouter, {result.get('num_turns', '?')} turns, {secs}s")
    if r.returncode != 0 and not packet.startswith("TASK"):
        return 1
    return 0


def check(args):
    cfg = load_config(os.getcwd())
    status = {"workers": cfg["workers"], "claude_cli": claude_bin() is not None,
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
        return run(parse(argv[1:]))
    if argv[0] == "check":
        return check(argv[1:])
    if argv[0] == "config":
        print(json.dumps(load_config(os.getcwd()), indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
