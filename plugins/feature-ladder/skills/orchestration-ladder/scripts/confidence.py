#!/usr/bin/env python3
"""
feature-ladder confidence surface: what the gates used to decide, decided
from the project's own memory instead. Costs no model tokens by default.

Before a Task Card is dispatched (and, as advice, before a command with
effects outside the repo), the coordinator asks this script how confident
the repository's history is about the action. One Hindsight recall becomes
an "emotionally textured surface": every remembered fact is scored for
sentiment (a pitfall about these files reads as a warning, a convention or
a test strategy as steady ground), weighted by how closely it touches the
files and commands at hand, and folded with the card's size, the command's
tier and the evidence gathered so far into one number in [0, 1]:

  >= threshold (0.60)  proceed
  <  threshold          reshard: split the card into smaller cards (fewer
                        files, one concern each) and recall again per shard.
                        For a command: run its preview or dry-run form first
                        when the tool has one, then the real command.

It never blocks anything. The audit hook logs what ran; this is advice
about size and previews, written into FEATURE-REPORT.md next to the action.

  confidence.py card   --action "<card title>" [--paths p ...] [--size S|M|L]
                       [--memories FILE | --live --query "<text>"]
                       [--checks-passed N] [--checks-failed N]
                       [--scorer lexicon|jev|openai] [--threshold 0.60]
                       [--cwd DIR] [--out FILE]
  confidence.py action --action "<command>" [--tier mundane|risky|never]
                       [--memories FILE | --live --query "<text>"] [...]
                       (without --tier the command is classified by irreversible.py)
  confidence.py score  "<text>" ...          sentiment of each text (debug)

--memories FILE is the recall as the Hindsight MCP tool returned it (JSON of
any shape; every object with a text/content/fact field counts) or one memory
per line ("[kind] fact"). --live performs the recall itself over MCP
(HINDSIGHT_MCP_URL / HINDSIGHT_API_KEY, this repository's bank) and counts it
against the feature's recall budget through memory_gate.py.

--scorer jev (or openai) asks an OpenAI-compatible chat endpoint for the
sentiment numbers instead of the lexicon: one request, temperature 0, JSON
out. Configured under "confidence" in ~/.claude/feature-ladder/providers.json
(or <repo>/.feature-ladder/providers.json):
  {"confidence": {"scorer": "jev", "base_url": "https://openrouter.ai/api/v1",
                  "model": "typesafe/jev-router", "key_env": "OPENROUTER_API_KEY"}}
Any failure falls back to the lexicon and the output says which scorer ran.

Output (JSON): confidence, threshold, decision (proceed | reshard), prior,
surface {memories, positive, negative, neutral, pitfalls, sentiment, mood,
texture, items}, reasons, reshard (advice when the decision is reshard),
scorer. Exit 0 proceed, 5 reshard, 1 error.
"""
import json
import math
import os
import re
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "hindsight-memory" / "scripts"))

DEFAULT_THRESHOLD = 0.60
KINDS = ("decision", "convention", "pitfall", "interface", "test-strategy")
# What a memory of each kind says about the ground before its words are read.
KIND_PRIOR = {"pitfall": -0.6, "decision": 0.2, "convention": 0.25, "interface": 0.15,
              "test-strategy": 0.35, "note": 0.0}
SIZE_PRIOR = {"S": 0.75, "M": 0.65, "L": 0.55}
TIER_PRIOR = {"none": 0.72, "mundane": 0.72, "risky": 0.55, "never": 0.50, "secret": 0.05}
MOODS = ((-0.3, "wary"), (0.3, "steady"), (math.inf, "confident"))

POSITIVE = {
    "works", "worked", "working", "pass", "passes", "passed", "passing", "green", "stable", "reliable",
    "safe", "safely", "fixed", "fix", "fixes", "resolved", "resolves", "succeeded", "succeeds", "success",
    "successful", "clean", "fast", "simple", "consistent", "idempotent", "correct", "correctly", "verified",
    "healthy", "ok", "okay", "good", "solid", "deterministic", "ready", "covered", "tested", "robust",
    "straightforward", "cheap", "quick", "smooth",
}
NEGATIVE = {
    "fail", "fails", "failed", "failing", "failure", "failures", "broke", "broken", "breaks", "break", "bug",
    "bugs", "crash", "crashed", "crashes", "timeout", "timeouts", "timed", "hang", "hangs", "hung", "flaky",
    "regression", "regressed", "lost", "loss", "corrupt", "corrupted", "corruption", "wrong", "incorrect",
    "denied", "refused", "blocked", "outage", "down", "slow", "leak", "leaks", "missing", "stale",
    "conflict", "conflicts", "error", "errors", "exception", "unrefunded", "expired", "inverted",
    "overwrote", "overwrites", "dangerous", "unsafe", "risky", "pitfall", "gotcha", "beware", "careful",
    "avoid", "mismatch", "stuck", "unbounded", "silently", "surprising", "surprise", "unexpected",
    "costly", "expensive",
}
NEGATORS = {"not", "no", "never", "without", "isnt", "wasnt", "doesnt", "didnt", "cannot", "cant", "wont"}
STOP = {"the", "and", "for", "with", "that", "this", "from", "into", "use", "add", "new", "when", "then",
        "than", "are", "was", "were", "has", "have", "had", "its", "it", "an", "a", "to", "of", "in", "on",
        "by", "as", "at", "be", "is", "or", "so", "if", "we", "our", "all", "any", "each", "map", "maps"}
WORD = re.compile(r"[a-z0-9_]+")
LINE_RE = re.compile(r"^\s*[-*]?\s*\[(" + "|".join(KINDS) + r")\]\s*(.+?)\s*$", re.I)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- sentiment --
def polarity(text):
    """Lexicon sentiment in [-1, 1]; a negator within two words flips a hit."""
    toks = WORD.findall(str(text).lower())
    pos = neg = 0
    for i, t in enumerate(toks):
        s = 1 if t in POSITIVE else -1 if t in NEGATIVE else 0
        if not s:
            continue
        if any(w in NEGATORS for w in toks[max(0, i - 2):i]):
            s = -s
        if s > 0:
            pos += 1
        else:
            neg += 1
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 3)


def load_providers(cwd):
    cfg = {}
    for f in (Path.home() / ".claude" / "feature-ladder" / "providers.json",
              Path(cwd) / ".feature-ladder" / "providers.json"):
        try:
            if f.is_file():
                cfg.update(json.loads(f.read_text(encoding="utf-8-sig")).get("confidence") or {})
        except (OSError, ValueError):
            pass
    return cfg


JEV_PROMPT = ("Each item is a memory about a software codebase, sent as {\"i\": index, \"kind\": tag, "
              "\"text\": fact}. Score each one on the same scale as a sentiment lexicon: -1 is a clear "
              "failure or pitfall (a deploy that timed out, a test that fails), 0 is a neutral fact, +1 is a "
              "clear success or something that works (a convention that holds, a passing test). Use values "
              'in between for mixed facts. Reply with JSON {"scores": [{"i": 0, "score": 0.4}, ...]}, one '
              "entry per item, echoing each item's index, and nothing else.")


def _http_transport(payload, cfg):
    """Default transport: the OpenRouter (OpenAI-compatible) call. Returns the response body text."""
    base = str(cfg.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    key_env = cfg.get("key_env") or "OPENROUTER_API_KEY"
    key = os.environ.get(key_env, "")
    if not key:
        raise RuntimeError(f"no {key_env} in the environment")
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def _parse_scores(body, n):
    """Response body (text or dict) -> list of n floats-or-None. None = no usable score for that fact.
    Never raises: anything unparseable gives all None."""
    out = [None] * n
    try:
        data = json.loads(body) if isinstance(body, (str, bytes)) else body
        if isinstance(data, dict) and "choices" in data:
            data = data["choices"][0]["message"]["content"]
            data = json.loads(data) if isinstance(data, str) else data
        entries = data["scores"]
        if not isinstance(entries, list):
            return out
    except (ValueError, KeyError, IndexError, TypeError):
        return out
    seen, dup = set(), set()
    for e in entries:
        i = e.get("i") if isinstance(e, dict) else None
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < n:
            continue
        if i in seen:
            dup.add(i)
        seen.add(i)
        sc = e.get("score")
        if isinstance(sc, bool) or not isinstance(sc, (int, float)) or not math.isfinite(sc):
            continue
        out[i] = clamp(float(sc), -1.0, 1.0)
    for i in dup:
        out[i] = None
    return out


def remote_sentiments(memories, cfg, transport=None):
    """Ask an OpenAI-compatible endpoint (Jev through OpenRouter by default) for one number per
    memory. Returns a list with a float in [-1, 1] per memory, or None where the model gave no usable
    score for it. `transport(payload)` returns the response body (text or dict); the default is the
    OpenRouter call. Transport errors raise; the caller falls back to the lexicon."""
    model = cfg.get("model") or "typesafe/jev-router"
    payload = {"model": model, "temperature": 0, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": JEV_PROMPT},
                            {"role": "user", "content": json.dumps(
                                [{"i": i, "kind": m.get("kind") or "fact", "text": m["text"]}
                                 for i, m in enumerate(memories)])}]}
    body = transport(payload) if transport else _http_transport(payload, cfg)
    return _parse_scores(body, len(memories))


def sentiments(memories, scorer, cfg, transport=None):
    """(scores, scorer actually used, note). Facts the model did not score fall back to polarity()."""
    scorer = (scorer or cfg.get("scorer") or "lexicon").lower()
    lex = [polarity(m["text"]) for m in memories]
    if scorer in ("jev", "openai") and memories:
        try:
            got = remote_sentiments(memories, cfg, transport)
        except Exception as e:  # noqa: BLE001 - advice must never fail because a model did
            return lex, "lexicon", f"{scorer} scorer unavailable ({e}); lexicon used"
        miss = sum(1 for g in got if g is None)
        if miss == len(got):
            return lex, "lexicon", f"{scorer} scorer unavailable (no usable scores in the response); lexicon used"
        note = f"{scorer} scorer gave no usable score for {miss} of {len(got)} facts; lexicon used for those" if miss else None
        return [l if g is None else g for g, l in zip(got, lex)], scorer, note
    return lex, "lexicon", None


# ------------------------------------------------------------------ memory --
def _decoded(text):
    """A string that holds a JSON object/array (an MCP text envelope) -> the parsed value, else None."""
    t = text.strip()
    if t[:1] in ("{", "["):
        try:
            return json.loads(t)
        except ValueError:
            return None
    return None


def _items_from(obj, out):
    if isinstance(obj, str):
        inner = _decoded(obj)
        if inner is not None:
            _items_from(inner, out)
        return
    if isinstance(obj, dict):
        # MCP envelopes: {"result": "<json>"} and {"content": [{"type": "text", "text": "<json>"}]}
        if isinstance(obj.get("result"), str) and _decoded(obj["result"]) is not None:
            _items_from(obj["result"], out)
            return
        content = obj.get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "text" for c in content):
            for c in content:
                if isinstance(c, dict) and isinstance(c.get("text"), str):
                    inner = _decoded(c["text"])
                    if inner is not None:
                        _items_from(inner, out)
                    elif c["text"].strip():
                        out.append({"text": c["text"], "tags": [], "metadata": {}})
            return
        text = next((obj[k] for k in ("text", "content", "fact", "memory") if isinstance(obj.get(k), str)), None)
        if text:
            out.append({"text": text, "tags": [str(t) for t in obj.get("tags") or []],
                        "metadata": obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}})
            return
        for v in obj.values():
            _items_from(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _items_from(v, out)


def _kind_of(text, tags, metadata):
    m = LINE_RE.match(text)
    if m:
        return m.group(1).lower(), m.group(2)
    kind = next((t for t in tags if t in KINDS), None) or str(metadata.get("kind") or "").lower()
    return (kind if kind in KINDS else "note"), text.strip()


def parse_memories(source):
    """A recall result (any JSON shape) or '[kind] fact' lines -> [{kind, text, tags}]."""
    raw = []
    if isinstance(source, (dict, list)):
        _items_from(source, raw)
    else:
        text = str(source)
        try:
            _items_from(json.loads(text), raw)
        except ValueError:
            raw = [{"text": l.strip().lstrip("-* ").strip(), "tags": [], "metadata": {}}
                   for l in text.splitlines() if l.strip()]
    out = []
    for r in raw:
        kind, fact = _kind_of(r["text"], r.get("tags") or [], r.get("metadata") or {})
        if fact:
            out.append({"kind": kind, "text": " ".join(fact.split()), "tags": r.get("tags") or []})
    return out


def live_recall(query, cwd):
    """One scoped recall over MCP, counted by memory_gate.py. ([], reason) on any problem."""
    try:
        import memory_gate
        import resolve_bank
        ident = resolve_bank.identity(cwd)
        root = memory_gate.repo_root(cwd)
        args = {"query": query, "max_tokens": memory_gate.MAX_RECALL_TOKENS}
        ok, why, args = memory_gate.gate("recall", args, root, ident.get("project") or "feature-ladder")
        if not ok:
            return [], why
        bank = (resolve_bank.resolve(cwd) or {}).get("bank_id")
        if bank:
            args["bank_id"] = bank
        res = resolve_bank.Mcp().start().call("recall", args)
        return parse_memories(res), None
    except Exception as e:  # noqa: BLE001 - no memory is "unknown territory", not an error
        return [], f"recall unavailable: {e}"[:200]


# ----------------------------------------------------------------- surface --
def anchors(action, paths):
    toks = {t for t in WORD.findall(str(action).lower()) if len(t) >= 3 and t not in STOP}
    for p in paths or []:
        p = str(p).replace("\\", "/").lower()
        base = p.rsplit("/", 1)[-1]
        toks.update({p, base, base.split(".")[0]})
        toks.update(t for t in WORD.findall(base) if len(t) >= 3 and t not in STOP)
    return {t for t in toks if t}


def surface(memories, action, paths, scorer=None, cfg=None):
    cfg = cfg or {}
    anch = anchors(action, paths)
    pols, used, note = sentiments(memories, scorer, cfg)
    items = []
    for mem, pol in zip(memories, pols):
        text = mem["text"]
        low = text.lower()
        toks = set(WORD.findall(low))
        matches = sum(1 for a in anch if a in toks or ("/" in a and a in low))
        score = clamp(KIND_PRIOR.get(mem["kind"], 0.0) + 0.6 * pol, -1.0, 1.0)
        items.append({"kind": mem["kind"], "text": text[:160], "polarity": pol, "score": round(score, 2),
                      "weight": 1 + min(2, matches), "relevant": matches > 0})
    if items:
        s = round(sum(i["score"] * i["weight"] for i in items) / sum(i["weight"] for i in items), 3)
        mood = next(m for lim, m in MOODS if s < lim)
    else:
        s, mood = None, "unknown"
    return {"memories": len(items),
            "positive": sum(1 for i in items if i["score"] > 0.1),
            "negative": sum(1 for i in items if i["score"] < -0.1),
            "neutral": sum(1 for i in items if -0.1 <= i["score"] <= 0.1),
            "pitfalls": sum(1 for i in items if i["kind"] == "pitfall"),
            "relevant_pitfalls": sum(1 for i in items if i["relevant"] and i["score"] < 0),
            "sentiment": s, "mood": mood, "texture": texture(items, mood), "items": items,
            "scorer": used, "note": note}


def texture(items, mood):
    if not items:
        return "no memories of this area: unknown territory"
    kinds = {}
    for i in items:
        kinds[i["kind"]] = kinds.get(i["kind"], 0) + 1
    parts = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    top = sorted(items, key=lambda i: (-abs(i["score"] * i["weight"]), i["text"]))[:3]
    quotes = "; ".join(f"'{i['text'][:80]}' ({i['kind']} {i['score']:+.2f}{', touches this' if i['relevant'] else ''})"
                       for i in top)
    return f"{len(items)} memories ({parts}): {mood} - {quotes}"


# -------------------------------------------------------------- confidence --
def confidence(prior, surf, passed=0, failed=0):
    c, reasons = prior, [f"prior {prior:.2f}"]
    s = surf.get("sentiment")
    if s is None:
        c -= 0.03
        reasons.append("no memory of this area (-0.03)")
    else:
        d = 0.25 * s
        c += d
        reasons.append(f"memory sentiment {s:+.2f} ({d:+.2f}, {surf['mood']})")
    rp = int(surf.get("relevant_pitfalls") or 0)
    if rp:
        d = -min(0.15, 0.05 * rp)
        c += d
        reasons.append(f"{rp} pitfall(s) touching these files ({d:+.2f})")
    if passed:
        d = min(0.15, 0.05 * passed)
        c += d
        reasons.append(f"{passed} check(s) passed ({d:+.2f})")
    if failed:
        d = -min(0.45, 0.15 * failed)
        c += d
        reasons.append(f"{failed} check(s) failed ({d:+.2f})")
    return round(clamp(c, 0.05, 0.98), 2), reasons


def reshard_advice(mode, paths):
    if mode == "card":
        n = max(2, min(4, math.ceil(len(paths) / 2))) if paths else 2
        return {"shards": n,
                "advice": (f"split the card into {n} smaller cards, one concern each, by independent file sets "
                           "(at most budget.workers + 1 cards, sequential when the budget allows one worker); "
                           "recall again per shard; one reshard per feature, then proceed and record the confidence")}
    return {"preview_first": True,
            "advice": ("run the preview or dry-run form first when the tool has one (--dry-run, --preview, "
                       "hosting:channel:deploy, the emulator), read its output, then run the real command; "
                       "record both next to the action in FEATURE-REPORT.md")}


def decide(mode, action, paths=(), size=None, tier=None, memories=(), passed=0, failed=0,
           threshold=None, scorer=None, cfg=None, note=None):
    paths = list(paths or [])
    threshold = float(threshold if threshold is not None
                      else os.environ.get("FEATURE_LADDER_CONFIDENCE_THRESHOLD", DEFAULT_THRESHOLD))
    if mode == "card":
        prior = SIZE_PRIOR.get(str(size or "S").upper()[:1], SIZE_PRIOR["S"])
    else:
        prior = TIER_PRIOR.get(str(tier or "none").lower(), TIER_PRIOR["none"])
    surf = surface(list(memories), action, paths, scorer, cfg)
    c, reasons = confidence(prior, surf, passed, failed)
    decision = "proceed" if c >= threshold else "reshard"
    out = {"mode": mode, "action": action, "paths": paths, "size": size, "tier": tier,
           "confidence": c, "threshold": threshold, "decision": decision, "prior": prior,
           "surface": surf, "reasons": reasons, "scorer": surf.pop("scorer"),
           "reshard": reshard_advice(mode, paths) if decision == "reshard" else None}
    notes = [n for n in (note, surf.pop("note", None)) if n]
    if notes:
        out["notes"] = notes
    return out


# -------------------------------------------------------------------- main --
def parse(argv):
    a, pos, i = {}, [], 0
    while i < len(argv):
        t = argv[i]
        if t.startswith("--"):
            if t in ("--live",):
                a[t] = True
                i += 1
                continue
            vals = []
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                vals.append(argv[i])
                i += 1
            if t == "--paths":
                a.setdefault(t, []).extend(vals)
            else:
                a[t] = " ".join(vals) if vals else True
            continue
        pos.append(t)
        i += 1
    return a, pos


def classify_tier(action, cwd):
    try:
        from irreversible import classify
        run = {}
        p = Path(cwd) / ".feature-ladder" / "run.json"
        if p.is_file():
            run = json.loads(p.read_text(encoding="utf-8"))
        return classify(action, run).get("tier", "none")
    except Exception:  # noqa: BLE001
        return "none"


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    mode, a, pos = argv[0], *parse(argv[1:])
    cwd = a.get("--cwd") or os.getcwd()
    if mode == "score":
        print(json.dumps([{"text": t, "polarity": polarity(t)} for t in pos], indent=2))
        return 0
    if mode not in ("card", "action"):
        print(__doc__)
        return 1
    action = a.get("--action") or " ".join(pos)
    if not action:
        print("--action is required", file=sys.stderr)
        return 1
    memories, note = [], None
    if a.get("--memories"):
        memories = parse_memories(Path(a["--memories"]).read_text(encoding="utf-8-sig"))
    elif a.get("--live"):
        memories, note = live_recall(a.get("--query") or action, cwd)
    tier = a.get("--tier") or (classify_tier(action, cwd) if mode == "action" else None)
    out = decide(mode, action, a.get("--paths") or [], a.get("--size"), tier, memories,
                 int(a.get("--checks-passed") or 0), int(a.get("--checks-failed") or 0),
                 a.get("--threshold"), a.get("--scorer"), load_providers(cwd), note)
    text = json.dumps(out, indent=2)
    if a.get("--out"):
        Path(a["--out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(a["--out"]).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if out["decision"] == "proceed" else 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
