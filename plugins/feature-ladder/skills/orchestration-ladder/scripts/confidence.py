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
per line ("[kind] fact"). A file that is missing or cannot be read counts as
no memories (Hindsight was down), with a note in the output. --live performs the recall itself over MCP
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
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "hindsight-memory" / "scripts"))

import ladder_state  # noqa: E402

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
# File stems that name a role, not a place: "index", "main" or "package" in a path
# says nothing about which memories are about it.
GENERIC_STEMS = {"index", "main", "app", "init", "__init__", "mod", "lib", "src", "test", "tests", "spec",
                 "util", "utils", "helper", "helpers", "common", "base", "core", "config", "settings",
                 "constants", "types", "type", "model", "models", "schema", "setup", "readme", "package",
                 "makefile", "dockerfile", "server", "client", "routes", "router", "api", "data", "file",
                 "files", "page", "pages", "component", "components", "style", "styles"}
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
              ladder_state.state_dir(ladder_state.state_root(cwd)) / "providers.json"):
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
                        "metadata": obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {},
                        "at": obj.get("mentioned_at") or obj.get("mentionedAt") or obj.get("occurred_start")})
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
            out.append({"kind": kind, "text": " ".join(fact.split()), "tags": r.get("tags") or [],
                        **({"at": r["at"]} if r.get("at") else {})})
    return out


def live_recall(query, cwd):
    """One scoped recall over MCP, counted by memory_gate.py. ([], reason) on any problem."""
    try:
        import memory_gate
        import resolve_bank
        root = memory_gate.repo_root(cwd)
        args = {"query": query, "max_tokens": memory_gate.MAX_RECALL_TOKENS}
        ok, why, args = memory_gate.gate("recall", args, root, resolve_bank.projects(cwd))
        if not ok:
            return [], why
        resolved = resolve_bank.resolve(cwd) or {}
        if resolved.get("how") == "planned":  # the bank is only created by the first memory write
            return [], "no memories yet for this repository"
        bank = resolved.get("bank_id")
        if bank:
            args["bank_id"] = bank
        memory_gate.commit_recall(root)
        res = resolve_bank.Mcp(root).start().call("recall", args)
        return parse_memories(res), None
    except Exception as e:  # noqa: BLE001 - no memory is "unknown territory", not an error
        return [], f"recall unavailable: {e}"[:200]


# ----------------------------------------------------------------- surface --
def _distinctive(word):
    return len(word) >= 3 and word not in STOP and word not in GENERIC_STEMS and not word.isdigit()


def anchors(action, paths):
    """What a memory must mention to count as touching this card. From the action:
    its words (3+ letters, no stop words). From each path: the path itself, its last
    two components, the file name with its extension, and the file's stem when the
    stem is distinctive. A bare extension ("json", "ts") or a generic stem ("index",
    "main", "package") is never an anchor: it would match unrelated memories."""
    toks = {t for t in WORD.findall(str(action).lower()) if len(t) >= 3 and t not in STOP}
    for p in paths or []:
        p = str(p).replace("\\", "/").lower().strip().strip("/")
        while p.startswith("./"):
            p = p[2:]
        if not p:
            continue
        parts = p.split("/")
        base = parts[-1]
        toks.add(p)
        if len(parts) >= 2:
            toks.add("/".join(parts[-2:]))
        stem = base.split(".")[0] if "." in base.strip(".") else base
        if "." in base and len(stem) >= 2:
            toks.add(base)  # "maproutes.ts": matched whole, see touching()
        if _distinctive(stem):
            toks.add(stem)
            if len(parts) >= 2:
                toks.add(f"{parts[-2]}/{stem}")  # "server/maproutes", as a test command names it
    return {t for t in toks if t}


def touching(text, anch):
    """How many anchors the text mentions. A word anchor must be a whole word; a
    path or file name must appear whole (not inside a longer name)."""
    low = str(text).lower()
    toks = set(WORD.findall(low))
    n = 0
    for a in anch:
        if "/" in a or "." in a:
            if re.search(r"(?<![a-z0-9_])" + re.escape(a) + r"(?![a-z0-9_])", low):
                n += 1
        elif a in toks:
            n += 1
    return n


# A memory is relevant to a card when it names one of the card's files (or a
# distinctive file stem), or at least RELEVANT_WORDS distinct words of the
# action. One shared word ("deploy", "user") is how unrelated memories got in.
RELEVANT_WORDS = 2
MAX_RELEVANT = 5           # facts a Memory: line carries at most
FILE_TOKEN = re.compile(r"(?<![\w./-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z0-9]{1,8})(?![\w/])")


def split_anchors(action, paths):
    """(file anchors, word anchors): what a memory can name to count as touching the card."""
    files = anchors("", paths)
    words = {t for t in anchors(action, []) if t not in files and _distinctive(t)}
    return files, words


def relevance(text, files, words):
    """(file hits, word hits) for one memory text."""
    return touching(text, files), touching(text, words)


def is_relevant(hits):
    return hits[0] > 0 or hits[1] >= RELEVANT_WORDS


def _tracked(root):
    """Files git tracks under root, as lower-case '/' paths ([] when not a repository)."""
    try:
        r = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), capture_output=True, timeout=20)
        if r.returncode != 0:
            return None
        return [f.decode("utf-8", "replace").lower() for f in r.stdout.split(b"\0") if f]
    except (OSError, subprocess.SubprocessError):
        return None


def stale(text, root, tracked):
    """True when the memory names repository paths (with a folder part) and none of
    them exists any more: it is about code that was moved or deleted. A memory that
    names no such path, or one path that still exists, is never stale."""
    if tracked is None:
        return False
    named = [m.group(1).strip(".").replace("\\", "/").lower() for m in FILE_TOKEN.finditer(str(text))]
    named = [n for n in named if not n.startswith(("http", "www."))]
    if not named:
        return False
    for n in named:
        n = n.lstrip("./")
        if (Path(root) / n).exists() or any(t == n or t.endswith("/" + n) for t in tracked):
            return False
    return True


def _norm_fact(text):
    return " ".join(re.sub(r"[^a-z0-9/._` -]+", " ", str(text).lower()).split())


# The app's architecture, stored by brainstorm.py remember as one
# "`arch:<detail>` Header: value" decision per detail (tag `architecture`).
# A feature rarely repeats the words of its architecture ("add a sign-up page"
# never says "Firebase Auth"), so these facts count as relevant when the work
# touches their area: the area's words below (prefixes), or the detail's own
# id. The stack, layout and tests bear on every card. They have their own
# allowance (ARCH_CAP), so they never push a pitfall off the Memory: line.
ARCH_RE = re.compile(r"`arch:([a-z0-9_]+)`")
ARCH_CAP = 4
ARCH_ALWAYS = {"stack", "structure", "testing"}
ARCH_WORDS = {
    "platform": ("web", "mobile", "desktop", "cli", "ios", "android", "browser"),
    "frontend": ("ui", "page", "screen", "component", "form", "button", "view", "style", "css", "layout", "front",
                 "client", "modal", "dashboard", "navigat"),
    "backend": ("api", "endpoint", "route", "server", "handler", "service", "rest", "graphql", "rpc", "function",
                "backend", "webhook"),
    "data_store": ("data", "database", "db", "store", "storage", "save", "persist", "schema", "table", "collection",
                   "model", "migrat", "query", "record", "firestore", "sql", "postgres", "mongo", "cache"),
    "auth": ("auth", "login", "logout", "sign", "session", "password", "account", "oauth", "token", "register",
             "invite"),
    "hosting": ("deploy", "host", "environment", "production", "staging", "domain", "docker"),
    "config_secrets": ("secret", "config", "setting", "credential", "env", "key"),
    "background": ("job", "queue", "schedul", "cron", "worker", "realtime", "websocket", "background", "event",
                   "notif"),
    "files_media": ("file", "upload", "image", "photo", "video", "media", "attach", "download"),
    "observability": ("log", "monitor", "metric", "analytic", "error", "alert", "trace"),
    "ci_cd": ("ci", "pipeline", "workflow", "release", "build", "lint"),
    "performance": ("perf", "fast", "slow", "cache", "scale", "load", "latency", "pagina"),
    "security_privacy": ("secur", "privacy", "valid", "sanitiz", "rate", "encrypt", "gdpr", "pii"),
    "dependencies": ("librar", "depend", "package", "install", "sdk"),
    "rollout": ("flag", "rollout", "migrat", "rollback", "launch"),
    "entities": ("data", "model", "schema", "entity", "record", "table", "collection", "field", "crud", "list",
                 "create", "edit", "delete", "save"),
    "roles": ("role", "permission", "admin", "access", "owner", "member", "team", "share", "private", "visib"),
}


def arch_detail(memory):
    """The architecture detail a memory records, or None for an ordinary memory."""
    m = ARCH_RE.search(memory["text"])
    return m.group(1) if m else None


def arch_area_hits(detail, action, paths):
    """How many words of the work fall in the detail's area (prefix match)."""
    words = set(WORD.findall((str(action) + " " + " ".join(str(p) for p in paths or [])).lower()))
    keys = ARCH_WORDS.get(detail, ()) + tuple(w for w in detail.split("_") if len(w) >= 3)
    return sum(1 for w in words if any(w.startswith(k) for k in keys))


def select_relevant(memories, action, paths, root=None, cap=MAX_RELEVANT, arch_cap=ARCH_CAP):
    """Only the memories that bear on this card: duplicates, stale memories (their
    paths no longer exist) and off-topic ones are dropped; the rest are ranked by
    file hits, then word hits, then recency (mentioned_at), then recall order, and
    capped. Architecture facts (arch_detail) are chosen apart: the newest per
    detail, relevant by area (ARCH_WORDS) as well, at most `arch_cap` of them,
    listed first. Returns (kept, dropped) where dropped counts each reason."""
    files, words = split_anchors(action, paths)
    tracked = _tracked(root) if root else None
    seen, ranked, arch = set(), [], {}
    dropped = {"duplicate": 0, "stale": 0, "off_topic": 0, "over_cap": 0}
    for i, m in enumerate(memories):
        key = _norm_fact(m["text"])
        if key in seen:
            dropped["duplicate"] += 1
            continue
        seen.add(key)
        detail = arch_detail(m)
        if detail:
            hits = relevance(m["text"], files, words)
            area = arch_area_hits(detail, action, paths)
            if not (is_relevant(hits) or area or detail in ARCH_ALWAYS):
                dropped["off_topic"] += 1
                continue
            row = (-hits[0], -(hits[1] + area), _neg_time(m.get("at")), i, m)
            old = arch.get(detail)
            if old is not None:   # an older decision about the same detail was superseded
                dropped["duplicate"] += 1
                if old[2] <= row[2]:
                    continue
            arch[detail] = row
            continue
        hits = relevance(m["text"], files, words)
        if not is_relevant(hits):
            dropped["off_topic"] += 1
            continue
        if root and stale(m["text"], root, tracked):
            dropped["stale"] += 1
            continue
        ranked.append((-hits[0], -hits[1], _neg_time(m.get("at")), i, m))
    ranked.sort(key=lambda r: r[:4])
    kept = [r[-1] for r in ranked[:cap]] if cap else [r[-1] for r in ranked]
    dropped["over_cap"] = len(ranked) - len(kept)
    rows = sorted(arch.values(), key=lambda r: r[:4])
    arch_kept = [r[-1] for r in rows[:arch_cap]] if arch_cap else []
    dropped["over_cap"] += len(rows) - len(arch_kept)
    return arch_kept + kept, dropped


def _neg_time(at):
    """Sort key: newer first; a memory without a date sorts after dated ones."""
    try:
        return -datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return math.inf


def surface(memories, action, paths, scorer=None, cfg=None):
    """Only memories relevant to the card (is_relevant) move the sentiment; the
    rest are listed but weigh nothing, so a pitfall about another part of the
    repository never makes this card look risky. Only the relevant ones are
    sent to a remote scorer."""
    cfg = cfg or {}
    files, words = split_anchors(action, paths)
    hits = [relevance(m["text"], files, words) for m in memories]
    rel = [i for i, h in enumerate(hits) if is_relevant(h)]
    pols = [polarity(m["text"]) for m in memories]
    used, note = "lexicon", None
    if rel:
        got, used, note = sentiments([memories[i] for i in rel], scorer, cfg)
        for i, pol in zip(rel, got):
            pols[i] = pol
    items = []
    for mem, pol, h in zip(memories, pols, hits):
        text = mem["text"]
        matches = h[0] + h[1]
        score = clamp(KIND_PRIOR.get(mem["kind"], 0.0) + 0.6 * pol, -1.0, 1.0)
        items.append({"kind": mem["kind"], "text": text[:160], "polarity": pol, "score": round(score, 2),
                      "weight": 1 + min(2, matches), "relevant": is_relevant(h)})
    counted = [i for i in items if i["relevant"]]
    if counted:
        s = round(sum(i["score"] * i["weight"] for i in counted) / sum(i["weight"] for i in counted), 3)
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
    if not any(i["relevant"] for i in items):
        return f"{len(items)} memories recalled, none about this card: unknown territory"
    items = [i for i in items if i["relevant"]]
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
    """The command's tier against this repository's run.json: the one in the main
    checkout, whichever subfolder or worktree the call came from."""
    try:
        from irreversible import classify
        run = ladder_state.read_json(ladder_state.state_dir(ladder_state.state_root(cwd)) / "run.json")
        return classify(action, run if isinstance(run, dict) else {}).get("tier", "none")
    except Exception:  # noqa: BLE001
        return "none"


def _number(a, flag, kind, lo, hi=None, default=None):
    """A numeric flag, or ValueError naming the flag. A flag given without a value
    is an error, not a silent default."""
    if flag not in a:
        return default
    v = a[flag]
    try:
        if v is True:
            raise ValueError
        n = kind(v)
        if n != n or n < lo or (hi is not None and n > hi):
            raise ValueError
        return n
    except (TypeError, ValueError):
        rng = f"{lo} to {hi}" if hi is not None else f"{lo} or more"
        raise ValueError(f"{flag} needs a number ({rng}); got {'nothing' if v is True else repr(v)}") from None


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
    try:
        passed = _number(a, "--checks-passed", int, 0, default=0)
        failed = _number(a, "--checks-failed", int, 0, default=0)
        threshold = _number(a, "--threshold", float, 0.0, 1.0)
    except ValueError as e:
        print(f"confidence.py: {e}", file=sys.stderr)
        return 1
    memories, note = [], None
    if a.get("--memories") and a["--memories"] is not True:
        try:
            memories = parse_memories(Path(a["--memories"]).read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError) as e:  # Hindsight was down, nothing was saved: unknown territory
            note = f"recall file not read ({Path(a['--memories']).name}: {type(e).__name__}); scored as no memories"
    elif a.get("--live"):
        memories, note = live_recall(a.get("--query") or action, cwd)
    tier = a.get("--tier") or (classify_tier(action, cwd) if mode == "action" else None)
    out = decide(mode, action, a.get("--paths") or [], a.get("--size"), tier, memories,
                 passed, failed, threshold, a.get("--scorer"), load_providers(cwd), note)
    text = json.dumps(out, indent=2)
    if a.get("--out"):
        Path(a["--out"]).parent.mkdir(parents=True, exist_ok=True)
        Path(a["--out"]).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if out["decision"] == "proceed" else 5


if __name__ == "__main__":
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
