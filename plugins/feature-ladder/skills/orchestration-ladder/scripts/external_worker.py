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
                         [--model ID] [--cwd DIR] [--timeout S] [--max-turns N] [--score N]
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

      --score is the router's score; with --no-bs it picks the tier's model
      (score 7 and up is the intermediate band, Kimi K3.1 Pro). With --no-bs
      `--rung implementer-high` has no Silk model (T3 and up stay on Claude):
      exit 3, run the card as a subagent.

  external_worker.py config [--no-bs]
      Prints the merged configuration (never the key).

  check and config take --no-bs to show the Silk Compute setup without a run.

Configuration, merged in this order (later wins):
  built-in defaults
  ~/.claude/feature-ladder/providers.json
  <repo>/.feature-ladder/providers.json
  --no-bs on the active run (run.json switches), or FEATURE_LADDER_NO_BS=1 as
      a manual-run aid (a hand-started `run`/`check` with no run.json, or a test;
      set it per command, nothing here ever saves it):
      the cheap tier moves to Silk Compute (SILK below, adjusted by a "silk"
      block in providers.json). Never a saved default: only runs started with
      /feature-ladder:feature ... --no-bs use it.
  env FEATURE_LADDER_WORKERS=openrouter|claude, FEATURE_LADDER_WORKER_MODEL=<id>

The key: the key_env variable, else that name in <repo>/.env, else in
~/.claude/feature-ladder/.env, else the key_secret in Google Secret Manager.
Whatever the child printed is shown and saved with the key masked ("***").

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
        "name": "OpenRouter",
        "slug": "openrouter",
        "meter": "key",  # cost = the change in GET /v1/key usage
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
# --no-bs: Silk Compute (https://silkcompute.com/docs/claude-code), an
# Anthropic-compatible endpoint serving abliterated open-weight models. Used only
# for a run started with --no-bs, never as a saved default. Each tier gets a Silk
# model that is capable of it (Silk's model guide, and the Vals AI scores in its
# Hy4 post: Terminal-Bench 4.0 Kimi 63.6, Hy4 39.4, GLM-5.3 38.9, DeepSeek 31.8):
#   mechanic (T0)                 DeepSeek V4.1 Flash 1017 Fast  mechanical patches, cheapest call
#   implementer, score <= 6       DeepSeek V4.1 Flash 1017       Silk's pick for long agent runs
#   implementer/medium, 7-14      Kimi K3.1 Pro                  the strongest; the intermediate band
#   implementer/high, T3 and up   stay on Claude (the router's rung)
# "fallbacks" take a card whose model was busy (429/503) before it did any work;
# if that one is busy too, the card runs on its Claude rung. A 429
# concurrent_request_limit is account-wide (8 per account), so it first waits and
# retries the SAME model (BUSY_WAITS) before the fallback. GLM-5.3 has no slot:
# Hy4 scores the same, costs less and also reads images (GLM-5.3 is text only).
SILK = {
    "name": "Silk Compute",
    "slug": "silk",
    "preset": "no-bs",
    "meter": "balance",  # cost = the drop in GET /v1/balance
    "base_url": "https://api.silkcompute.com",
    "key_env": "SILK_API_KEY",
    "key_secret": "SILK_API_KEY",
    "context_1m": False,  # Silk's Claude Code setup sets the window instead of a [1m] model id
    "env": {"CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1048576"},
    "reach": 14,     # implementer/medium cards up to this score run here (the router stops "glm" at 6)
    "mid_from": 7,   # implementer cards from this score get "implementer-mid"
    "models": {
        "mechanic": "deepseek-v4.1-flash-1017-fast-uncensored",
        "implementer": "deepseek-v4.1-flash-1017-uncensored",
        "implementer-mid": "kimi-k3.1-pro-uncensored",
        # no "implementer-high": T3 and up stay on Claude (model_for returns None)
    },
    "fallbacks": {
        "deepseek-v4.1-flash-1017-fast-uncensored": "glm-5.3-flash-uncensored",
        "deepseek-v4.1-flash-1017-uncensored": "hy4-uncensored",
        "kimi-k3.1-pro-uncensored": "hy4-uncensored",
    },
}
EXTERNAL_RUNGS = {"mechanic", "implementer"}  # never senior-implementer, experts, Fable, scribe, codex-reviewer
# intake.py card sends a card here when takes() says so: the router's provider
# "glm" (T0 and implementer/medium with score <= route.GLM_MAX_SCORE), and with
# --no-bs also implementer/medium up to SILK["reach"].
TOOLS = "Bash,Read,Edit,Write,MultiEdit,Glob,Grep,mcp__plugin_feature-ladder_hindsight__retain,mcp__plugin_feature-ladder_hindsight__recall"
DISALLOWED = "Agent,Task,Skill,WebSearch,WebFetch,NotebookEdit"
# The child's own words when OpenRouter turned the request away: nothing ran, so the
# card should fall back to a subagent (exit 3) instead of being reported as an error.
UNAVAILABLE = re.compile(r"\b(401|402|403)\b|no auth credentials|invalid (x-)?api[- ]key|unauthori[sz]ed|"
                         r"authentication[_ ]error|insufficient[_ ]credits|payment required|credit balance", re.I)
# The provider was busy (Silk: 429 concurrent_request_limit / rate_limited, 503
# under_capacity). Only counts when the child did no work: then the card moves to
# the model's fallback, or to its Claude rung.
BUSY = re.compile(r"\b(429|503|529)\b|under[_ ]capacity|concurrent[_ ]request[_ ]limit|rate[_ ]limit|"
                  r"model is busy|overloaded", re.I)
# Account-wide: another model would hit the same limit, so wait and retry the same one first.
CONCURRENT = re.compile(r"concurrent[_ ]request[_ ]limit", re.I)
RETRY_AFTER = re.compile(r"retry[-_ ]after\D{0,4}(\d+)", re.I)
BUSY_WAITS = (5, 10, 15)  # seconds before each same-model retry: 30 s in all at most
MAX_WAIT = 15             # a Retry-After hint is honoured up to this many seconds


def _sleep(seconds):
    time.sleep(seconds)  # patched in tests


def redact(text, key):
    """`text` with the key masked: the child can read its own environment."""
    return str(text or "").replace(key, "***") if key and len(key) >= 8 else str(text or "")


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


def no_bs_requested(cwd):
    """--no-bs is on for this run: the active run.json carries the switch (intake.py
    apply -> pr_checkpoint.py start), or FEATURE_LADDER_NO_BS=1 for a manual run."""
    if os.environ.get("FEATURE_LADDER_NO_BS", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    try:
        run = ladder_state.read_json(ladder_state.state_dir(repo_root(cwd)) / "run.json")
    except Exception:  # noqa: BLE001 - an unreadable run file means no switch
        return False
    return isinstance(run, dict) and bool(run.get("active")) and "no_bs" in (run.get("switches") or [])


def load_config(cwd, no_bs=None):
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
    cfg["no_bs"] = bool(no_bs_requested(cwd) if no_bs is None else no_bs)
    if cfg["no_bs"]:
        # The whole provider block becomes Silk's (only the run limits carry over);
        # a "silk" block in providers.json may adjust it, e.g. a tier's model.
        silk = _merge(SILK, cfg.get("silk") if isinstance(cfg.get("silk"), dict) else {})
        keep = {k: cfg["openrouter"][k] for k in ("max_turns", "timeout") if k in cfg["openrouter"]}
        cfg["openrouter"] = _merge(keep, silk)
        if cfg["workers"] == "claude":
            cfg["workers"] = "auto"  # the run asked for Silk explicitly
    if os.environ.get("FEATURE_LADDER_WORKERS"):
        cfg["workers"] = os.environ["FEATURE_LADDER_WORKERS"].strip().lower()
    if os.environ.get("FEATURE_LADDER_WORKER_MODEL"):
        m = os.environ["FEATURE_LADDER_WORKER_MODEL"].strip()
        cfg["openrouter"]["models"] = {k: m for k in cfg["openrouter"]["models"]}
    return cfg


def _score(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def model_for(cfg, rung, effort=None, override=None, score=None):
    if override:
        return override
    p = cfg["openrouter"]
    models = p["models"]
    key = rung
    s = _score(score)
    if rung == "implementer" and (effort or "").lower() == "high":
        key = "implementer-high"
    elif rung == "implementer" and "implementer-mid" in models and s is not None and s >= int(p.get("mid_from") or 7):
        key = "implementer-mid"
    if key == "implementer-high" and key not in models and cfg.get("no_bs"):
        return None  # Silk has no model for T3 and up: they stay on Claude
    return models.get(key) or models.get("implementer")


def takes(cfg, route):
    """Does the cheap tier take this routed card? Always the router's provider "glm"
    cards (T0 and small T2); with --no-bs also implementer/medium cards up to the
    provider's "reach" score (the intermediate T2 band). Never T3 and up."""
    if route.get("provider") == "glm":
        return True
    reach, s = _score(cfg["openrouter"].get("reach")), _score(route.get("score"))
    return (reach is not None and s is not None and s <= reach
            and route.get("agent") == "implementer" and route.get("effort") == "medium")


def fallback_for(cfg, model):
    fb = (cfg["openrouter"].get("fallbacks") or {}).get(model)
    return fb if fb and fb != model else None


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


def dotenv_value(name, cwd=None):
    """`name` from <repo>/.env (the main checkout), then ~/.claude/feature-ladder/.env.
    Only that one line is read; returns (value, file) or (None, None)."""
    files = []
    try:
        files.append(repo_root(cwd or os.getcwd()) / ".env")
    except Exception:  # noqa: BLE001
        pass
    files.append(Path.home() / ".claude" / "feature-ladder" / ".env")
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            s = line.strip()
            if s.startswith("export "):
                s = s[7:].lstrip()
            k, sep, v = s.partition("=")
            if sep and k.strip() == name:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                if v:
                    return v, str(f)
    return None, None


def available(cfg, cwd=None):
    """Cheap readiness check with no network and no secret fetch: the CLI is on
    PATH and a key source exists (the env var, a .env file, or gcloud for the secret)."""
    if not claude_bin():
        return False, "claude CLI not found on PATH"
    env_name = cfg["openrouter"].get("key_env")
    if env_name and os.environ.get(env_name):
        return True, f"env {env_name}"
    if env_name:
        val, f = dotenv_value(env_name, cwd)
        if val:
            return True, f"{env_name} in {f}"
    if cfg["openrouter"].get("key_secret"):
        try:
            import with_secrets  # noqa: E402
            if with_secrets.gcloud():
                return True, "Secret Manager " + cfg["openrouter"]["key_secret"]
        except Exception:  # noqa: BLE001
            pass
        return False, f"no {env_name} in the environment or a .env file and no gcloud CLI for the secret"
    return False, "no key_env or key_secret configured"


def get_key(cfg, cwd=None):
    env_name = cfg["openrouter"].get("key_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip(), f"env {env_name}"
    if env_name:
        val, f = dotenv_value(env_name, cwd)
        if val:
            return val, f"{env_name} in {f}"
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
    """OpenRouter GET /api/v1/key (usage and limits), or for a "balance" meter
    (Silk) GET /v1/balance. Validates the key. Never prints the key."""
    balance = cfg["openrouter"].get("meter") == "balance"
    req = urllib.request.Request(cfg["openrouter"]["base_url"].rstrip("/") + ("/v1/balance" if balance else "/v1/key"),
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        if balance:
            return {"valid": True, "balance_usd": d.get("balance")}
        d = d.get("data", {})
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
    for k, v in (cfg["openrouter"].get("env") or {}).items():
        env[str(k)] = str(v)
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


def record_ledger(root, task_id, rung, model, result, provider="openrouter"):
    """One row per card in runs/<slug>/usage.jsonl (the file hooks/scripts/ledger.py
    reports from). Written here directly, so it also works where the hook script is
    not installed (the Claude app). A line that cannot be read is kept, not fatal."""
    try:
        run = ladder_state.read_json(ladder_state.state_dir(root) / "run.json")
        if not isinstance(run, dict):
            return
        usage = result.get("usage") or {}
        entry = {"at": now(), "key": f"external-{task_id}", "slug": run.get("slug"),
                 "agent": f"{rung} ({provider})", "rung": rung, "model": model, "effort": None,
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
    """A spend counter that only goes up: OpenRouter's usage for this key, or the
    negated Silk balance. The difference around a run is the real charge (Claude
    Code's total_cost_usd assumes Anthropic prices)."""
    try:
        s = key_status(cfg, key)
        if not s.get("valid"):
            return None
        if s.get("balance_usd") is not None:
            return -float(s["balance_usd"])
        return float(s["usage_usd"]) if s.get("usage_usd") is not None else None
    except (TypeError, ValueError):
        return None


def busy_before_work(child_exit, stdout, stderr):
    """The provider turned the child away as busy (429/503) before it did any work:
    the card can move to the fallback model or its Claude rung with nothing lost."""
    if child_exit is None:
        return False
    res = parse_result(stdout) if (stdout or "").strip() else {}
    said = unfence(str(res.get("result") or ""))
    if said.startswith("TASK"):
        return False
    try:
        turns = int(res.get("num_turns") or 0)
    except (TypeError, ValueError):
        turns = 0
    try:
        out_tokens = int((res.get("usage") or {}).get("output_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        out_tokens = 0
    failed = child_exit != 0 or bool(res.get("is_error"))
    # Output tokens mean the model answered, maybe with an edit: never retry that elsewhere.
    return failed and turns <= 1 and out_tokens == 0 and bool(BUSY.search(f"{said}\n{stderr}"))


def retry_wait(stdout, stderr, n):
    """Seconds to wait before same-model retry number n (0-based) after a 429
    concurrent_request_limit, or None: not that error, or the tries are used up."""
    text = f"{stdout}\n{stderr}"
    if n >= len(BUSY_WAITS) or not CONCURRENT.search(text):
        return None
    m = RETRY_AFTER.search(text)
    return min(int(m.group(1)), MAX_WAIT) if m else BUSY_WAITS[n]


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
    pname = cfg["openrouter"].get("name") or "OpenRouter"
    key, source = get_key(cfg, cwd)
    if not key:
        print(f"external_worker: no {pname} key ({source})", file=sys.stderr)
        return 3
    model = model_for(cfg, rung, effort, args.get("--model"), args.get("--score"))
    if not model:
        print(f"TASK {task_id} - blocked\nChanged: none\nChecks run: none\n"
              f"Claims: {pname} has no model for {rung}{'-high' if effort else ''} cards: run it on its Claude rung\n"
              "Risks: none\nMemory candidates: none\nRetained: none - unavailable")
        return 3
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
    # A model that is busy before doing any work hands the card to its fallback
    # once (not when the coordinator pinned a model with --model).
    attempts = [model] + ([fallback_for(cfg, model)] if fallback_for(cfg, model) and not args.get("--model") else [])
    used, busy_models = model, []
    try:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass
        for i, m in enumerate(attempts):
            used = m
            tries = 0
            while True:
                try:
                    child_exit, stdout, stderr = _spawn(cmd, cwd, child_env(cfg, key, m), card, timeout)
                except OSError as e:
                    child_exit, stdout, stderr = 127, "", f"could not start {exe}: {e}"
                stdout, stderr = redact(stdout, key), redact(stderr, key)
                if not busy_before_work(child_exit, stdout, stderr):
                    break
                wait = retry_wait(stdout, stderr, tries)
                if wait is None:
                    break
                tries += 1
                _sleep(wait)  # account-wide limit: the same model, a moment later
            if not busy_before_work(child_exit, stdout, stderr):
                break
            busy_models.append(m)
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
    elif failed and (UNAVAILABLE.search(f"{said}\n{stderr}") or used in busy_models):
        code = 3
        busy = f" ({', '.join(busy_models)} busy)" if used in busy_models else ""
        packet = (f"TASK {task_id} - blocked\nChanged: none\nChecks run: none\n"
                  f"Claims: external worker unavailable, nothing ran{busy}: {why_failed}\n"
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
    if charged is not None and charged < 0:
        charged = None  # the balance was topped up meanwhile: the cost is unknown, never negative
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
    record = {"task": task_id, "rung": rung, "model": used, "provider": pname, "key_source": source, "seconds": secs,
              "busy_models": busy_models, "no_bs": bool(cfg.get("no_bs")),
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
    record_ledger(root, task_id, rung, used, result, cfg["openrouter"].get("slug") or "openrouter")
    u = result.get("usage") or {}
    toks = int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
    if charged is None:
        cost_txt = "cost unknown"
    elif cfg["openrouter"].get("meter") == "balance":
        cost_txt = (f"{pname} balance dropped about ${charged:.4f} (approximate: every card on the account "
                    "shares one balance)")
    elif result.get("openrouter_cost_shared"):
        cost_txt = f"{pname} charged ${charged:.4f} on this key meanwhile (other external cards ran too: their spend is in it)"
    else:
        cost_txt = f"{pname} charged ${charged:.4f}"
    fell = f" (fallback: {', '.join(busy_models)} busy)" if busy_models and used not in busy_models else ""
    print(f"\nexternal worker: {used} via {pname}{fell}, {result.get('num_turns', '?')} turns, {secs}s, "
          f"{toks:,} input tokens, {int(u.get('output_tokens') or 0):,} output, {cost_txt}")
    return code


def check(args):
    cfg = load_config(os.getcwd(), no_bs=True if "--no-bs" in args else None)
    p = cfg["openrouter"]
    status = {"workers": cfg["workers"], "provider": p.get("name") or "OpenRouter",
              "preset": p.get("preset") or "glm", "no_bs": cfg["no_bs"],
              "claude_cli": claude_bin() is not None, "models": p["models"]}
    if p.get("fallbacks"):
        status["fallbacks"] = p["fallbacks"]
    key, source = get_key(cfg)
    status["key"] = source if key else None
    if not key:
        status["error"] = source
        print(json.dumps(status, indent=2))
        return 3
    status["provider_key"] = key_status(cfg, key)
    if "--probe" in args:
        models = set(p["models"].values()) | set((p.get("fallbacks") or {}).values())
        status["probes"] = {m: probe_model(cfg, key, m) for m in sorted(models)}
    print(json.dumps(status, indent=2))
    ok = status["claude_cli"] and status["provider_key"].get("valid") and \
        all(r.get("ok") for r in status.get("probes", {}).values())
    return 0 if ok else 3


RUN_FLAGS = {"--card", "--id", "--rung", "--model", "--cwd", "--timeout", "--max-turns", "--score"}


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
        print(json.dumps(load_config(os.getcwd(), no_bs=True if "--no-bs" in argv[1:] else None), indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
