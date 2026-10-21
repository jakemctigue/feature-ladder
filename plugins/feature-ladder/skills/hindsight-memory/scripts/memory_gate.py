#!/usr/bin/env python3
"""
Deterministic gate for every Hindsight read and write feature-ladder makes.
Costs no model tokens. The rules are the same in Claude Code (a PreToolUse
hook applies them) and in the Claude app (the skill runs `check` before each
call).

Reads (`recall`): this repository's bank only (the bank hook routes it), tags
forced to `project:<name>`, at most MAX_RECALL_TOKENS of context, at most
RUN_RECALLS calls per feature run (the scout, the coordinator, one worker).

Writes (`retain`): never during a feature run; once, at the end of the
session, from the queue the coordinator filled. Every memory must be ONE
concrete fact a future task can act on:
  - written as "[kind] fact", kind one of decision | convention | pitfall |
    interface | test-strategy (no "outcome": a feature summary is in git);
  - 25-280 characters, at most two sentences;
  - anchored to something in the repo: a path, an identifier, a command or a
    backticked token;
  - not narration ("we implemented", "successfully", "this session", ...);
  - not a duplicate of anything already retained from this repo.
Every other Hindsight tool (reflect, knowledge pages, bank edits) is denied
to agents: reflect is a free-form summary by the server's model.

    memory_gate.py check "<[kind] fact>" [--cwd DIR]   exit 0 ok, 1 rejected (reason printed)
    memory_gate.py flush [--cwd DIR] [--dry-run | --emit]
                                                       retain the session queue directly over
                                                       MCP (no model), print what was written;
                                                       --dry-run only lists it; --emit lists it
                                                       and records it as written (the Claude
                                                       app makes the retain calls itself)
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

KINDS = ("decision", "convention", "pitfall", "interface", "test-strategy")
LINE_RE = re.compile(r"^\s*[-*]?\s*\[(" + "|".join(KINDS) + r")\]\s*(.+?)\s*$", re.I)
MIN_CHARS, MAX_CHARS, MAX_SENTENCES = 25, 280, 2
MAX_RECALL_TOKENS = 1000
RUN_RECALLS = 3
SESSION_RETAINS = 12
ALLOWED_TOOLS = {"recall", "retain"}
ROUTING_TOOLS = {"list_banks", "create_bank"}  # resolve_bank.py's own calls
NARRATION = re.compile(
    r"\b(i|we)\s+(have\s+)?(implemented|added|built|fixed|created|updated|changed|worked)\b|"
    r"\bsuccessfully\b|\bthis (session|run|task|feature|pr)\b|\bthe user\b|\bin summary\b|"
    r"\boverall\b|\bwas (completed|done|implemented|added)\b|\bas requested\b|\bworks (well|fine)\b",
    re.I)
ANCHOR = re.compile(
    r"`[^`]+`|"                                  # backticked token
    r"[\w.-]+/[\w./-]+|"                         # a path
    r"\b[\w-]+\.(py|ts|tsx|js|jsx|mjs|go|rs|java|rb|cs|gd|json|ya?ml|toml|sql|md|rules|sh)\b|"
    r"\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z]\w+\b|"  # snake_case / camelCase identifier
    r"\b\w+\(\)|"                                # call()
    r"--[a-z][\w-]+")                            # a CLI flag


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root(cwd):
    sys.path.insert(0, str(HERE.parents[1] / "orchestration-ladder" / "scripts"))
    import session_budget
    return session_budget.find_root(cwd)


def _ledger(root):
    return Path(root) / ".feature-ladder" / "memory-ledger.jsonl"


def _norm(text):
    return " ".join(re.sub(r"[^a-z0-9/._` -]+", " ", text.lower()).split())


def _hash(text):
    return hashlib.sha256(_norm(text).encode()).hexdigest()[:16]


def retained_hashes(root):
    out = set()
    p = _ledger(root)
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.add(json.loads(line)["hash"])
            except (ValueError, KeyError):
                pass
    return out


def parse_line(line):
    """'[kind] fact' -> (kind, fact) or raises ValueError with the reason."""
    m = LINE_RE.match(str(line or ""))
    if not m:
        raise ValueError("write it as '[kind] fact' with kind one of " + ", ".join(KINDS))
    kind, fact = m.group(1).lower(), m.group(2)
    validate_fact(fact)
    return kind, fact


def validate_fact(fact):
    if not MIN_CHARS <= len(fact) <= MAX_CHARS:
        raise ValueError(f"one concrete fact of {MIN_CHARS}-{MAX_CHARS} characters ({len(fact)} given)")
    if len([s for s in re.split(r"(?<=[.!?])\s+", fact) if s.strip()]) > MAX_SENTENCES:
        raise ValueError(f"at most {MAX_SENTENCES} sentences: one fact per memory")
    if NARRATION.search(fact):
        raise ValueError("progress narration or summary, not a fact a future task can act on")
    if not ANCHOR.search(fact):
        raise ValueError("name what it is about: a path, identifier, command or `token` in this repo")


def check_line(root, line, seen=None):
    kind, fact = parse_line(line)
    h = _hash(fact)
    if h in (seen if seen is not None else retained_hashes(root)):
        raise ValueError("already retained from this repo")
    return kind, fact, h


def _active_run(root):
    try:
        run = json.loads((Path(root) / ".feature-ladder" / "run.json").read_text(encoding="utf-8"))
        return run if run.get("active") else None
    except (OSError, ValueError):
        return None


def _state(root):
    p = Path(root) / ".feature-ladder" / "memory-gate.json"
    try:
        return p, json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return p, {}


def gate(tool, args, root, project):
    """Decide one Hindsight tool call. Returns (allowed, reason, updated_args)."""
    short = str(tool).rsplit("__", 1)[-1]
    args = dict(args or {})
    if short in ROUTING_TOOLS:
        return True, "", args
    if short not in ALLOWED_TOOLS:
        return False, (f"feature-ladder: Hindsight '{short}' is not used by the ladder (only recall at the start "
                       "of a feature and retain at /feature-ladder:wrap). Carry on without it."), args
    tag = f"project:{project}"
    run = _active_run(root)
    if short == "recall":
        sp, st = _state(root)
        key = (run or {}).get("slug") or "no-run"
        used = int(st.get("recalls", {}).get(key, 0))
        if run is not None and used >= RUN_RECALLS:
            return False, (f"feature-ladder: {RUN_RECALLS} recalls per feature are used; the Task Card's Memory "
                           "line is the recall. Carry on from the card."), args
        st.setdefault("recalls", {})[key] = used + 1
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(st) + "\n", encoding="utf-8")
        except OSError:
            pass
        args["tags"] = [tag]
        args["tags_match"] = "all"
        args["max_tokens"] = min(int(args.get("max_tokens") or MAX_RECALL_TOKENS), MAX_RECALL_TOKENS)
        args.setdefault("budget", "low")
        return True, "", args
    # retain
    if run is not None:
        return False, ("feature-ladder: no memory writes during a feature run. Put the fact under "
                       "'Memory candidates:' as '[kind] fact'; it is written once at /feature-ladder:wrap."), args
    content = str(args.get("content") or "")
    tags = [str(t) for t in (args.get("tags") or [])]
    kinds = [t for t in tags if t in KINDS]
    line = content if LINE_RE.match(content) else (f"[{kinds[0]}] {content}" if kinds else content)
    try:
        kind, fact, h = check_line(root, line)
    except ValueError as e:
        return False, f"feature-ladder: memory rejected: {e}.", args
    feature = next((t for t in tags if t.startswith("feature:")), "feature:session")
    args.update({"content": fact, "context": "feature-ladder",
                 "tags": [tag, feature, kind],
                 "metadata": {**(args.get("metadata") or {}), "source": "feature-ladder", "kind": kind}})
    record(root, fact, kind)
    return True, "", args


def record(root, fact, kind):
    p = _ledger(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now(), "hash": _hash(fact), "kind": kind, "fact": fact}) + "\n")


def flush(root, dry_run=False, emit=False):
    """Retain the session queue straight over MCP: no model reads or rewrites it."""
    import resolve_bank
    q = Path(root) / ".feature-ladder" / "session-memory.md"
    lines = q.read_text(encoding="utf-8").splitlines() if q.is_file() else []
    seen, keep, rejected = retained_hashes(root), [], []
    for line in lines:
        if not line.strip():
            continue
        try:
            kind, fact, h = check_line(root, line, seen)
            seen.add(h)
            keep.append((kind, fact))
        except ValueError as e:
            rejected.append({"line": line.strip()[:200], "reason": str(e)})
    keep = keep[:SESSION_RETAINS]
    out = {"queued": len(lines), "written": [], "rejected": rejected, "dry_run": dry_run}
    if not keep or dry_run or emit:
        out["would_write"] = [f"[{k}] {f}" for k, f in keep]
        if emit:
            for kind, fact in keep:
                record(root, fact, kind)
        return out
    r = resolve_bank.resolve(str(root))
    ident = resolve_bank.identity(str(root))
    mcp = resolve_bank.Mcp().start()
    for kind, fact in keep:
        args = {"content": fact, "context": "feature-ladder",
                "tags": [f"project:{ident['project']}", "feature:session", kind],
                "metadata": {"source": "feature-ladder", "kind": kind}}
        if r and r.get("bank_id"):
            args["bank_id"] = r["bank_id"]
        mcp.call("retain", args)
        record(root, fact, kind)
        out["written"].append(f"[{kind}] {fact}")
    return out


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cwd = os.getcwd()
    if "--cwd" in argv:
        i = argv.index("--cwd")
        cwd = argv[i + 1]
        del argv[i:i + 2]
    root = repo_root(cwd)
    if argv[0] == "check":
        try:
            kind, fact, _ = check_line(root, " ".join(argv[1:]))
            print(json.dumps({"ok": True, "kind": kind, "fact": fact}))
            return 0
        except ValueError as e:
            print(json.dumps({"ok": False, "reason": str(e)}))
            return 1
    if argv[0] == "flush":
        try:
            print(json.dumps(flush(root, "--dry-run" in argv, "--emit" in argv), indent=2))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}))
            return 3
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
