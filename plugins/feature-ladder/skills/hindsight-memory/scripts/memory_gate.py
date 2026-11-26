#!/usr/bin/env python3
"""
Deterministic gate for every Hindsight read and write feature-ladder makes.
Costs no model tokens. The rules are the same in Claude Code (a PreToolUse
hook applies them) and in the Claude app (the skill runs `check` before each
call).

Reads (`recall`): this repository's bank only (the bank hook routes it), tags
forced to the project's tags with `tags_match: any_strict` (untagged memories
never come back), at most MAX_RECALL_TOKENS of fact text, no
`observation` duplicates of the stored facts, at most RUN_RECALLS calls per
feature run (the scout, the coordinator, one worker). A call that already
carries exactly these arguments is passed through unchanged, so the hook has
nothing to rewrite.

Writes (`retain`): never during a feature run; once, at the end of the
session, from the queue the coordinator filled. Every memory must be ONE
concrete fact a future task can act on:
  - written as "[kind] fact", kind one of decision | convention | pitfall |
    interface | test-strategy (no "outcome": a feature summary is in git);
  - 25-280 characters, at most two sentences;
  - anchored to something in the repo: a path, an identifier, a command or a
    backticked token;
  - not narration ("we implemented", "successfully", "this session", ...);
  - not a duplicate of anything already retained from this checkout.
The app's architecture, settled in a brainstorm (brainstorm.py remember),
has its own queue (.feature-ladder/session-memory-architecture.md): its
"[decision] `arch:<id>` ..." facts pass the same checks, are written
outside the per-session cap and are tagged `architecture`.
A fact counts as retained only once the write has happened: the gate marks it
"in flight", and the PostToolUse hook (or flush itself) records it in
.feature-ladder/memory-ledger.jsonl. A write that failed can be retried.
Every other Hindsight tool (reflect, knowledge pages, bank edits) is denied
to agents: reflect is a free-form summary by the server's model.

    memory_gate.py check "<[kind] fact>" [--cwd DIR]   exit 0 ok, 1 rejected (reason printed)
    memory_gate.py flush [--cwd DIR] [--dry-run | --emit | --auto | --background]
                                                       retain the session queue directly over
                                                       MCP (no model), print what was written,
                                                       rejected and `deferred` (lines past the
                                                       per-session cap; they stay queued for
                                                       the next wrap); --dry-run only lists it,
                                                       with the exact `retain_calls` to make by
                                                       hand; --emit lists it and records it as
                                                       written (the Claude app makes the retain
                                                       calls itself); --auto writes only when it
                                                       is safe to do unattended (no active run,
                                                       not a GLM child, a key, something queued,
                                                       no failure in the last 10 minutes) and
                                                       prints nothing otherwise; --background
                                                       starts --auto detached and returns at once
                                                       (the SessionStart/SessionEnd hooks)
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "orchestration-ladder" / "scripts"))
import ladder_state  # noqa: E402

KINDS = ("decision", "convention", "pitfall", "interface", "test-strategy")
LINE_RE = re.compile(r"^\s*[-*]?\s*\[(" + "|".join(KINDS) + r")\]\s*(.+?)\s*$", re.I)
MIN_CHARS, MAX_CHARS, MAX_SENTENCES = 25, 280, 2
MAX_RECALL_TOKENS = 1000
RUN_RECALLS = 3
SESSION_RETAINS = 12
PENDING_SECONDS = 120          # how long a gated retain counts as "in flight"
RECALL_TYPES = ["world", "experience"]  # observations restate the stored facts: half the tokens for nothing
# OR over the project's tags, and never an untagged memory: plain "all"/"any" also
# return every untagged memory in the bank (Hindsight's documented behaviour).
TAGS_MATCH = "any_strict"
FLUSH_BACKOFF = 600            # seconds after a failed unattended flush before the next try
ARCH_TAG = "architecture"      # the second tag of an architecture fact (instead of feature:session)
ARCH_QUEUE = "session-memory-architecture.md"
ARCH_RETAINS = 40              # architecture facts one flush writes at most (one per detail)
ALLOWED_TOOLS = {"recall", "retain"}
ROUTING_TOOLS = {"list_banks", "create_bank"}  # resolve_bank.py's own calls
NARRATION = re.compile(
    r"\b(i|we)\s+(have\s+)?(implemented|added|built|fixed|created|updated|changed|worked)\b|"
    r"\bsuccessfully\b|\bthis (session|run|task|feature|pr)\b|\bin summary\b|"
    r"\bthe user\s+(asked|wanted|wants|requested|said|told|needs|needed|is working|was working|prefers)\b|"
    r"\boverall\b|\bwas (completed|done|implemented|added)\b|\bas requested\b|\bworks (well|fine)\b",
    re.I)
# Abbreviations whose full stop does not end a sentence.
ABBREVIATIONS = re.compile(r"\b(e\.g|i\.e|etc|vs|cf|approx|incl)\.", re.I)
# word/word that is prose, not a path
PROSE_SLASH = {"and/or", "either/or", "input/output", "read/write", "client/server", "true/false", "yes/no",
               "on/off", "pass/fail", "n/a", "w/o", "i/o", "ci/cd", "he/she", "his/her", "s/he", "km/h"}
PATHLIKE = re.compile(r"[\w.-]+/[\w./-]+")
ANCHOR = re.compile(
    r"`[^`]+`|"                                  # backticked token
    r"\b[\w-]+\.(py|ts|tsx|js|jsx|mjs|go|rs|java|rb|cs|gd|json|ya?ml|toml|sql|md|rules|sh)\b|"
    r"\b[a-z]+_[a-z0-9_]+\b|\b[a-z]+[A-Z]\w+\b|"  # snake_case / camelCase identifier
    r"\b\w+\(\)|"                                # call()
    r"--[a-z][\w-]+")                            # a CLI flag


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def repo_root(cwd):
    return ladder_state.state_root(cwd)


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
            except (ValueError, KeyError, TypeError):
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


def _sentences(fact):
    text = ABBREVIATIONS.sub(lambda m: m.group(0)[:-1], fact)  # "e.g." -> "e.g": not a sentence end
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _anchored(fact):
    if ANCHOR.search(fact):
        return True
    for m in PATHLIKE.finditer(fact):
        tok = m.group(0).strip(".,;:")
        if tok.lower() in PROSE_SLASH or not re.search(r"[A-Za-z]{2}", tok):
            continue  # "and/or", "24/7"
        return True
    return False


def validate_fact(fact):
    if not MIN_CHARS <= len(fact) <= MAX_CHARS:
        raise ValueError(f"one concrete fact of {MIN_CHARS}-{MAX_CHARS} characters ({len(fact)} given)")
    if len(_sentences(fact)) > MAX_SENTENCES:
        raise ValueError(f"at most {MAX_SENTENCES} sentences: one fact per memory")
    if NARRATION.search(fact):
        raise ValueError("progress narration or summary, not a fact a future task can act on")
    if not _anchored(fact):
        raise ValueError("name what it is about: a path, identifier, command or `token` in this repo")


def check_line(root, line, seen=None):
    kind, fact = parse_line(line)
    h = _hash(fact)
    if h in (seen if seen is not None else retained_hashes(root)):
        raise ValueError("already retained from this repo")
    return kind, fact, h


def _active_run(root):
    run = ladder_state.read_json(Path(root) / ".feature-ladder" / "run.json")
    return run if isinstance(run, dict) and run.get("active") else None


def _state(root):
    p = Path(root) / ".feature-ladder" / "memory-gate.json"
    st = ladder_state.read_json(p)
    return p, st if isinstance(st, dict) else {}


def _save_state(path, st):
    try:
        ladder_state.write_json(path, st, indent=None)
    except OSError:
        pass


def _recall_key(run):
    if run is None:
        return None
    return f"{run.get('slug')}@{run.get('started')}"


def commit_recall(root):
    """Count one recall against the active feature run after the gate (and any
    bank routing) has let the call through."""
    key = _recall_key(_active_run(root))
    if not key:
        return
    with ladder_state.locked(root, "memory-gate"):
        sp, st = _state(root)
        recalls = st.get("recalls") if isinstance(st.get("recalls"), dict) else {}
        used = _int(recalls.get(key), 0)
        if used >= RUN_RECALLS:
            return
        new = {key: used + 1}
        if new != recalls:
            st["recalls"] = new
            _save_state(sp, st)


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def project_names(project):
    names = [project] if isinstance(project, str) else [str(p) for p in (project or []) if p]
    return names or ["unknown"]


def project_tags(project):
    """`project:<name>` for each name (str or list), the write tag first."""
    return [f"project:{n}" for n in project_names(project)]


def retain_args(fact, kind, project, feature="feature:session", bank_id=None, metadata=None):
    """The exact `retain` arguments the gate lets through unchanged."""
    args = {"content": fact, "context": "feature-ladder",
            "tags": [project_tags(project)[0], feature, kind],
            "metadata": {**(metadata or {}), "source": "feature-ladder", "kind": kind}}
    if bank_id:
        args["bank_id"] = bank_id
    return args


def gate(tool, args, root, project):
    """Decide one Hindsight tool call. Returns (allowed, reason, updated_args)."""
    short = str(tool).rsplit("__", 1)[-1]
    args = dict(args or {})
    if short in ROUTING_TOOLS:
        return True, "", args
    if short not in ALLOWED_TOOLS:
        return False, (f"feature-ladder: Hindsight '{short}' is not used by the ladder (only recall at the start "
                       "of a feature and retain at /feature-ladder:wrap). Carry on without it."), args
    run = _active_run(root)
    if short == "recall":
        key = _recall_key(run)
        # Only the active run is counted. Outside a run nothing is written unless an
        # old run's counters are still on disk (they are dropped then), so a recall
        # in a repository that never ran the ladder leaves no file behind.
        if key or (Path(root) / ".feature-ladder" / "memory-gate.json").is_file():
            with ladder_state.locked(root, "memory-gate"):
                sp, st = _state(root)
                recalls = st.get("recalls") if isinstance(st.get("recalls"), dict) else {}
                used = _int(recalls.get(key), 0) if key else 0
                if key and used >= RUN_RECALLS:
                    return False, (f"feature-ladder: {RUN_RECALLS} recalls per feature are used; the Task Card's "
                                   "Memory line is the recall. Carry on from the card."), args
                if not key and recalls:
                    st["recalls"] = {}
                    _save_state(sp, st)
        args["tags"] = project_tags(project)
        args["tags_match"] = TAGS_MATCH
        args["max_tokens"] = max(0, min(_int(args.get("max_tokens"), MAX_RECALL_TOKENS), MAX_RECALL_TOKENS))
        args.setdefault("budget", "low")
        if not isinstance(args.get("types"), list) or not args["types"]:
            args["types"] = list(RECALL_TYPES)
        return True, "", args
    # retain
    if run is not None:
        return False, ("feature-ladder: no memory writes during a feature run. Put the fact under "
                       "'Memory candidates:' as '[kind] fact'; it is written once at /feature-ladder:wrap."), args
    content = str(args.get("content") or "")
    raw_tags = args.get("tags")
    tags = [str(t) for t in raw_tags] if isinstance(raw_tags, list) else ([str(raw_tags)] if raw_tags else [])
    meta = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    kinds = [t for t in tags if t in KINDS] or ([str(meta.get("kind"))] if meta.get("kind") in KINDS else [])
    line = content if LINE_RE.match(content) else (f"[{kinds[0]}] {content}" if kinds else content)
    try:
        kind, fact, h = check_line(root, line)
    except ValueError as e:
        return False, f"feature-ladder: memory rejected: {e}.", args
    with ladder_state.locked(root, "memory-gate"):
        sp, st = _state(root)
        pending = {k: v for k, v in (st.get("pending") or {}).items()
                   if isinstance(v, (int, float)) and time.time() - v < PENDING_SECONDS}
        if h in pending:
            return False, ("feature-ladder: memory rejected: this fact is already in flight (a retain for it was "
                           f"let through less than {PENDING_SECONDS} s ago). Do not send it twice."), args
        pending[h] = time.time()
        st["pending"] = pending
        _save_state(sp, st)
    feature = next((t for t in tags if t.startswith("feature:") or t == ARCH_TAG), "feature:session")
    args.update(retain_args(fact, kind, project, feature, metadata=meta))
    return True, "", args


def confirm(root, args):
    """The retain call ran: record the fact so it is never written twice.
    Called by the PostToolUse hook with the arguments the gate produced."""
    args = args if isinstance(args, dict) else {}
    fact = str(args.get("content") or "").strip()
    meta = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    tags = args.get("tags") if isinstance(args.get("tags"), list) else []
    kind = meta.get("kind") if meta.get("kind") in KINDS else next((t for t in tags if t in KINDS), None)
    if not fact or not kind:
        return False
    h = _hash(fact)
    with ladder_state.locked(root, "memory-gate"):
        if h not in retained_hashes(root):
            record(root, fact, kind)
        sp, st = _state(root)
        if h in (st.get("pending") or {}):
            st["pending"].pop(h, None)
            _save_state(sp, st)
    return True


def record(root, fact, kind):
    p = _ledger(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": now(), "hash": _hash(fact), "kind": kind, "fact": fact}) + "\n")
    ladder_state.ensure_gitignore(root)


def _flush_state(root, failed=None):
    """Read (failed=None) or set the time of the last failed unattended flush."""
    with ladder_state.locked(root, "memory-gate"):
        sp, st = _state(root)
        if failed is None:
            return st.get("flush_failed_at")
        if failed:
            st["flush_failed_at"] = time.time()
        else:
            st.pop("flush_failed_at", None)
        _save_state(sp, st)


def auto_skip(root):
    """Why an unattended flush must not run now, or None when it may."""
    if os.environ.get("FEATURE_LADDER_CHILD"):
        return "a GLM worker's own session stores nothing"
    fl = Path(root) / ".feature-ladder"
    if not ((fl / "session-memory.md").is_file() or (fl / ARCH_QUEUE).is_file()):
        return "nothing queued"
    if _active_run(root) is not None:
        return "a feature run is active: its facts are written after it finishes"
    import resolve_bank
    ok, why = resolve_bank.key_status(str(root))
    if not ok:
        return why
    last = _flush_state(root)
    if isinstance(last, (int, float)) and time.time() - last < FLUSH_BACKOFF:
        return "the last unattended write failed less than 10 minutes ago"
    return None


def background(root):
    """Start `flush --auto` detached and return at once: a hook must not wait on the network."""
    why = auto_skip(root)
    if why:
        return {"started": False, "reason": why}
    import subprocess
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "cwd": str(root), "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_GROUP | NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "flush", "--auto", "--cwd", str(root)], **kw)
    return {"started": True}


def flush(root, dry_run=False, emit=False, project=None, auto=False):
    """Retain the session queue straight over MCP: no model reads or rewrites it."""
    if auto:
        why = auto_skip(root)
        if why:
            return {"skipped": why}
        try:
            out = flush(root, project=project)
        except Exception as e:  # noqa: BLE001
            _flush_state(root, failed=True)
            return {"error": f"{type(e).__name__}: {e}"[:300]}
        _flush_state(root, failed=False)
        return out
    fl = Path(root) / ".feature-ladder"
    seen, rejected = retained_hashes(root), []

    def checked(name):
        q = fl / name
        lines = [l for l in (q.read_text(encoding="utf-8").splitlines() if q.is_file() else []) if l.strip()]
        keep = []
        for line in lines:
            try:
                kind, fact, h = check_line(root, line, seen)
                seen.add(h)
                keep.append((kind, fact))
            except ValueError as e:
                if "already retained" not in str(e) or name != ARCH_QUEUE:
                    rejected.append({"line": line.strip()[:200], "reason": str(e)})
        return lines, keep

    arch_lines, arch = checked(ARCH_QUEUE)       # the app's architecture first: features build on it
    lines, keep = checked("session-memory.md")
    arch, arch_over = arch[:ARCH_RETAINS], arch[ARCH_RETAINS:]
    keep, over = keep[:SESSION_RETAINS], keep[SESSION_RETAINS:]
    todo = [(k, f, ARCH_TAG) for k, f in arch] + [(k, f, "feature:session") for k, f in keep]
    out = {"queued": len(lines), "written": [], "rejected": rejected, "dry_run": dry_run,
           "deferred": [f"[{k}] {f}" for k, f in arch_over + over]}
    if arch_lines:
        out["architecture"] = [f"[{k}] {f}" for k, f in arch]
    if over:
        out["note"] = (f"{len(over)} line(s) are over the {SESSION_RETAINS}-per-session cap; they stay in the queue "
                       "for the next /feature-ladder:wrap.")
    if not todo or dry_run or emit:
        out["would_write"] = [f"[{k}] {f}" for k, f, _ in todo]
        if (dry_run or emit) and todo:
            bank = None
            if project is None:
                import resolve_bank
                project = resolve_bank.projects(str(root))
                try:
                    bank = (resolve_bank.resolve(str(root), offline=True) or {}).get("bank_id")
                except Exception:  # noqa: BLE001 - the connector picks its own bank then
                    bank = None
            out["retain_calls"] = [retain_args(f, k, project, tag, bank_id=bank) for k, f, tag in todo]
        if emit:
            for kind, fact, _ in todo:
                record(root, fact, kind)
        return out
    import resolve_bank
    r = resolve_bank.ensure_bank(str(root))  # the bank is created here, on the first real write
    project = project or resolve_bank.projects(str(root))
    mcp = resolve_bank.Mcp(root).start()
    for kind, fact, tag in todo:
        mcp.call("retain", retain_args(fact, kind, project, tag, bank_id=(r or {}).get("bank_id")))
        record(root, fact, kind)
        out["written"].append(f"[{kind}] {fact}")
    return out


def main(argv):
    ladder_state.utf8_stdio()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cwd = os.getcwd()
    if "--cwd" in argv:
        i = argv.index("--cwd")
        if i + 1 >= len(argv):
            print(json.dumps({"error": "--cwd needs a directory"}))
            return 1
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
            if "--background" in argv:
                try:
                    if not sys.stdin.isatty():  # a hook: the payload names the session's folder
                        payload = json.loads(sys.stdin.read().lstrip("\ufeff") or "{}")
                        if isinstance(payload, dict) and payload.get("cwd") and "--cwd" not in sys.argv:
                            root = repo_root(payload["cwd"])
                    background(root)
                except Exception:  # noqa: BLE001 - a hook never fails the session
                    pass
                return 0
            out = flush(root, "--dry-run" in argv, "--emit" in argv, auto="--auto" in argv)
            if "--auto" in argv and "skipped" in out:
                return 0
            print(json.dumps(out, indent=2))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}))
            return 3
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
