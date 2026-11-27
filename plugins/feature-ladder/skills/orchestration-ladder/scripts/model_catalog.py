#!/usr/bin/env python3
"""
feature-ladder model catalogue: which OpenRouter model each rung runs on at a
budget level, what a thread cost, and what is left on the OpenRouter account.
Costs no model tokens.

Budget level ("budget" in providers.json, or FEATURE_LADDER_BUDGET): the best
model for each rung that the level can afford. The table is fixed (same input,
same route, always); `check` compares it with OpenRouter's live catalogue.

    level      mechanic (T0)      implementer (T2 small)   implementer-high    evidence collector (T1)
    low        Inkling 2 Mini     GLM 5.3 Flash            GLM 5.3             Inkling 2 Mini
    standard   GLM 5.3 Flash      GLM 5.3 Flash            GLM 5.3             Inkling 2 Small
    high       GLM 5.3 Flash      GLM 5.3                  Kimi K3.1 Pro       Inkling

Where the table comes from: scripts/bench/ladder_bench.py run --compare
(2026-11-26, two runs per task and model). Every candidate passed every card
of its rung, so on bounded cards the cheapest passing model is the best one a
level can buy, and a higher level buys headroom for harder cards, not a
better pass rate here. Measured cost per card at list price: mechanic Inkling
2 Mini $0.004, GLM 5.3 Flash $0.007, GLM 5.3 $0.010; implementer GLM 5.3
Flash $0.008, GLM 5.3 $0.011, Inkling 2 Small $0.015, Kimi K3.1 Pro $0.040;
evidence collector Inkling 2 Mini $0.003, Inkling 2 Small $0.010, Inkling
$0.047. Run the benchmark again before changing a row.

The orchestrator is GLM 5.3 at every level. A model named under
"openrouter": {"models": {...}} in providers.json, a "preset", or
FEATURE_LADDER_WORKER_MODEL wins over the table for its rung. Each rung also
has a next-best model (NEXT): a card whose model OpenRouter no longer serves,
or that is busy before doing any work, moves to it once.

    model_catalog.py levels                 the table above as JSON, with each model's live price
    model_catalog.py check [--refresh]      every model of the active level: listed? tools? price?
                                            Exit 0 all listed, 3 one is missing or the catalogue
                                            could not be read
    model_catalog.py price MODEL            $ per million tokens (input, output, cache read)
    model_catalog.py balance                what is left on the OpenRouter account and key

Prices: OpenRouter's public GET /api/v1/models, cached for a day in
~/.claude/feature-ladder/cache/openrouter-models.json (a stale copy is used
when the network is down). Claude prices are Anthropic's list prices: a thread
on the Claude plan is not billed per token, so its figure is only what the same
tokens would cost through the API.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

FLASH = "z-ai/glm-5.3-flash:nitro"
GLM = "z-ai/glm-5.3"
KIMI = "moonshotai/kimi-k3.1-pro"
INKLING = "thinkingmachines/inkling"
INKLING_SMALL = "thinkingmachines/inkling-2-small"
INKLING_MINI = "thinkingmachines/inkling-2-mini"
RUNGS = ("mechanic", "implementer", "implementer-high", "evidence-collector")
LEVELS = {
    "low": {"mechanic": INKLING_MINI, "implementer": FLASH, "implementer-high": GLM, "evidence-collector": INKLING_MINI},
    "standard": {"mechanic": FLASH, "implementer": FLASH, "implementer-high": GLM, "evidence-collector": INKLING_SMALL},
    "high": {"mechanic": FLASH, "implementer": GLM, "implementer-high": KIMI, "evidence-collector": INKLING},
}
DEFAULT_LEVEL = "standard"
# The next-best model when one is gone or busy before doing any work.
NEXT = {KIMI: GLM, GLM: FLASH, INKLING: INKLING_SMALL, INKLING_SMALL: INKLING_MINI, INKLING_MINI: FLASH}
ORCHESTRATOR = GLM

# Anthropic list prices, $ per million tokens (the claude-api reference, 2026-11). Cache
# writes are not listed there: 1.25x input, the usual five-minute rate, is assumed.
CLAUDE_PRICES = {
    "fable": {"in": 8.00, "out": 40.00, "cache_read": 0.20},
    "opus": {"in": 4.00, "out": 20.00, "cache_read": 0.20},
    "sonnet": {"in": 2.00, "out": 10.00, "cache_read": 0.10},
    "haiku": {"in": 0.10, "out": 0.50, "cache_read": 0.01},
}
CACHE_WRITE_FACTOR = 1.25
CACHE_SECONDS = 24 * 3600
MODELS_URL = "https://openrouter.ai/api/v1/models"
CREDITS_URL = "https://openrouter.ai/api/v1/credits"


def level_name(value=None):
    v = str(os.environ.get("FEATURE_LADDER_BUDGET") or value or DEFAULT_LEVEL).strip().lower()
    return v if v in LEVELS else DEFAULT_LEVEL


def models_for(level=None):
    return dict(LEVELS[level_name(level)])


def fallbacks_for(models):
    """{model: next-best} for the models in use (one hop each)."""
    return {m: NEXT[m] for m in set(models.values()) if m in NEXT}


def cache_file():
    return Path.home() / ".claude" / "feature-ladder" / "cache" / "openrouter-models.json"


def _fetch(url, key=None, timeout=20):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _per_million(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f * 1e6, 6) if f >= 0 else None  # a router's price is -1: it varies per request


def _rows(data):
    out = {}
    for m in data.get("data") or []:
        if not isinstance(m, dict) or not m.get("id"):
            continue
        p = m.get("pricing") or {}
        out[m["id"]] = {"in": _per_million(p.get("prompt")), "out": _per_million(p.get("completion")),
                        "cache_read": _per_million(p.get("input_cache_read")),
                        "tools": "tools" in (m.get("supported_parameters") or []),
                        "context": m.get("context_length"), "name": m.get("name")}
    return out


def catalog(refresh=False, offline=False):
    """({model id: row}, fetched_at or None). Never raises."""
    f = cache_file()
    cached = None
    try:
        cached = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    fresh = cached and time.time() - float(cached.get("at") or 0) < CACHE_SECONDS
    if cached and (offline or (fresh and not refresh)):
        return cached.get("models") or {}, cached.get("at")
    if offline:
        return {}, None
    try:
        rows = _rows(_fetch(MODELS_URL))
        if rows:
            try:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps({"at": time.time(), "models": rows}), encoding="utf-8")
            except OSError:
                pass
            return rows, time.time()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    return ((cached.get("models") or {}), cached.get("at")) if cached else ({}, None)


def bare(model):
    """The catalogue id: no [1m] marker, no :nitro / :floor routing suffix."""
    m = str(model or "").replace("[1m]", "").strip()
    head, sep, tail = m.rpartition(":")
    return head if sep and tail in ("nitro", "floor", "online", "exacto") else m


def claude_family(model):
    m = str(model or "").lower()
    if "/" in m:
        return None
    return next((k for k in CLAUDE_PRICES if k in m), None)


def price(model, rows=None):
    """{"in", "out", "cache_read", "source"} in $ per million tokens, or None."""
    fam = claude_family(model)
    if fam:
        return dict(CLAUDE_PRICES[fam], source="anthropic list price")
    if rows is None:
        rows, _ = catalog()
    row = rows.get(bare(model))
    if not row or row.get("in") is None or row.get("out") is None:
        return None
    return {"in": row["in"], "out": row["out"],
            "cache_read": row["cache_read"] if row.get("cache_read") is not None else row["in"],
            "source": "openrouter catalogue"}


def estimate_usd(model, tokens, rows=None):
    """What these tokens cost at the model's listed price, or None when it is not
    listed. `tokens`: input, cache_read, cache_write, output (a ledger row), or
    Claude Code's usage names."""
    p = price(model, rows)
    if not p or not isinstance(tokens, dict):
        return None

    def n(*names):
        for k in names:
            try:
                if tokens.get(k):
                    return int(tokens[k])
            except (TypeError, ValueError):
                pass
        return 0
    cost = (n("input", "input_tokens") * p["in"] + n("output", "output_tokens") * p["out"]
            + n("cache_read", "cache_read_input_tokens") * p["cache_read"]
            + n("cache_write", "cache_creation_input_tokens") * p["in"] * CACHE_WRITE_FACTOR) / 1e6
    return round(cost, 6)


def share(charged, estimates):
    """Split one real charge over the threads that shared it, in proportion to
    their estimates (evenly when none has one). Returns a list, same order."""
    if charged is None:
        return [None] * len(estimates)
    known = [e or 0.0 for e in estimates]
    total = sum(known)
    if total <= 0:
        return [round(charged / len(estimates), 6) for _ in estimates] if estimates else []
    return [round(charged * e / total, 6) for e in known]


def openrouter_balance(key, base_url="https://openrouter.ai/api", fetch=_fetch):
    """What is left: the account's credits (bought minus used) and this key's own
    limit. A key without permission to read credits still reports its limit."""
    out = {"account_left_usd": None, "account_bought_usd": None, "account_used_usd": None,
           "key_left_usd": None, "key_limit_usd": None, "key_used_usd": None}
    if not key:
        return dict(out, error="no OpenRouter key")
    base = str(base_url or "https://openrouter.ai/api").rstrip("/")
    try:
        d = (fetch(base + "/v1/credits", key) or {}).get("data") or {}
        if d.get("total_credits") is not None:
            out.update(account_bought_usd=float(d["total_credits"]), account_used_usd=float(d.get("total_usage") or 0))
            out["account_left_usd"] = round(out["account_bought_usd"] - out["account_used_usd"], 4)
    except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
        out["credits_error"] = str(e)[:120]
    try:
        d = (fetch(base + "/v1/key", key) or {}).get("data") or {}
        out.update(key_used_usd=d.get("usage"), key_limit_usd=d.get("limit"), key_left_usd=d.get("limit_remaining"))
    except (urllib.error.URLError, OSError, ValueError, TypeError) as e:
        out["error"] = str(e)[:120]
    return out


def check(level=None, refresh=False, models=None):
    rows, at = catalog(refresh=refresh)
    models = models or models_for(level)
    report = {"level": level_name(level), "catalogue_age_hours": round((time.time() - at) / 3600, 1) if at else None,
              "models": {}}
    ok = bool(rows)
    for rung, m in list(models.items()) + [("orchestrator", ORCHESTRATOR)]:
        row = rows.get(bare(m))
        report["models"][rung] = {"model": m, "listed": bool(row), "tools": bool(row and row.get("tools")),
                                  "in_usd_per_m": row.get("in") if row else None,
                                  "out_usd_per_m": row.get("out") if row else None, "next": NEXT.get(m)}
        ok = ok and bool(row) and bool(row.get("tools"))
    if not rows:
        report["error"] = "OpenRouter's catalogue could not be read and there is no cached copy"
    return report, ok


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    if cmd == "levels":
        rows, _ = catalog()
        print(json.dumps({lvl: {r: {"model": m, **{k: (price(m, rows) or {}).get(k) for k in ("in", "out")}}
                                for r, m in table.items()} for lvl, table in LEVELS.items()}, indent=2))
        return 0
    if cmd == "check":
        import external_worker as ew  # noqa: E402 - the active configuration (level, pinned models)
        cfg = ew.load_config(os.getcwd(), no_bs=False)
        models = {r: cfg["openrouter"]["models"].get(r) for r in RUNGS if cfg["openrouter"]["models"].get(r)}
        report, ok = check(cfg.get("budget"), refresh="--refresh" in argv, models=models)
        print(json.dumps(report, indent=2))
        return 0 if ok else 3
    if cmd == "price" and len(argv) > 1:
        print(json.dumps(price(argv[1]), indent=2))
        return 0
    if cmd == "balance":
        import external_worker as ew  # noqa: E402
        cfg = ew.load_config(os.getcwd(), no_bs=False)
        key, source = ew.get_key(cfg)
        print(json.dumps(openrouter_balance(key, cfg["openrouter"]["base_url"]), indent=2))
        return 0 if key else 3
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
