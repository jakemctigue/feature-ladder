#!/usr/bin/env python3
"""
The one way every feature-ladder mode reads Hindsight: recall, keep only the
facts that bear on the work, print the Memory: line. No model reads the raw
recall, and no model call is made.

    recall.py --query "<request or card title>" [--paths FILE ...] [--cwd DIR]
              [--out FILE] [--reuse FILE | --from FILE] [--cap N]

  --query   what the work is about (the feature request, or the card's title)
  --paths   the files the work touches (the intake's candidate files, a card's
            Scope); a memory naming one of them is relevant
  --out     where the raw recall is saved (default: the active run's
            runs/<slug>/recall.json, else .feature-ladder/recall.json); this is
            the file confidence.py --memories and external_worker.py read
  --reuse   use this saved recall if it exists (the scout's, from the intake),
            so the coordinator does not recall the same thing again; recalls
            live when the file is missing
  --from    only filter this saved recall (a recall made through the Hindsight
            MCP tool when the script cannot reach the server); no network
  --cap     facts in the Memory: line (default 5)

How the facts are chosen (confidence.select_relevant, shared with
confidence.py and external_worker.py):
  - Hindsight returns this repository's bank only, tagged with the project
    (`tags_match: any_strict`, so untagged memories never come back), at most
    1000 tokens of fact text, no `observation` restatements;
  - duplicates are dropped;
  - a memory is kept only if it names one of the paths (or a distinctive file
    stem) or at least two distinctive words of the query;
  - a memory whose named repository paths no longer exist is stale and dropped;
  - the rest are ranked by file hits, word hits, then newest first, and capped.

Output (JSON): memory (the line to paste, verbatim), facts, recalled, kept,
dropped {duplicate, stale, off_topic, over_cap}, source (live | reused | file |
none), saved_to, bank, reason (why there is no memory), and mcp_fallback (the
exact MCP `recall` arguments) when the script could not reach Hindsight itself:
make that one MCP call, save its result, and run this again with --from.
Always exits 0 (memory never stops a run) except for a usage error (1).
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "orchestration-ladder" / "scripts"))
import ladder_state  # noqa: E402
import memory_gate  # noqa: E402
import resolve_bank  # noqa: E402
import confidence  # noqa: E402

MULTI = {"--paths"}
SINGLE = {"--query", "--cwd", "--out", "--reuse", "--from", "--cap"}


def parse(argv):
    a, cur = {"--paths": []}, None
    for tok in argv:
        if tok in MULTI or tok in SINGLE:
            cur = tok
            if tok in SINGLE:
                a[tok] = None
            continue
        if cur in MULTI:
            a[cur] += [p for p in tok.replace(",", " ").split() if p]
        elif cur in SINGLE and a.get(cur) is None:
            a[cur] = tok
            cur = None
        else:
            raise ValueError(f"unexpected argument {tok!r}")
    for k in SINGLE:
        if k in a and a[k] is None:
            raise ValueError(f"{k} needs a value")
    return a


def default_out(root):
    fl = ladder_state.state_dir(root)
    run = ladder_state.read_json(fl / "run.json")
    if isinstance(run, dict) and run.get("active") and run.get("slug"):
        return fl / "runs" / str(run["slug"]) / "recall.json"
    return fl / "recall.json"


def live(query, cwd, root):
    """(raw result, bank, None) or (None, bank, reason, fallback_args)."""
    names = resolve_bank.projects(cwd)
    ok, where = resolve_bank.key_status(cwd)
    args = {"query": query, "max_tokens": memory_gate.MAX_RECALL_TOKENS}
    allowed, why, args = memory_gate.gate("recall", args, root, names)
    if not allowed:
        return None, None, why, None
    if not ok:
        # In Claude Code the plugin's MCP server reads the same environment, so it has no
        # key either: an MCP fallback would only spend a model call. The Claude app's
        # connector carries its own key, so there the fallback is the way in.
        return None, None, f"Hindsight is off: {where}", (None if os.environ.get("CLAUDECODE") else args)
    try:
        r = resolve_bank.resolve(cwd) or {}
    except Exception as e:  # noqa: BLE001
        return None, None, f"Hindsight not reachable ({type(e).__name__})", args
    bank = r.get("bank_id")
    if r.get("how") == "planned":
        return None, bank, "no memories yet for this repository (its bank is created by the first write)", None
    if bank:
        args["bank_id"] = bank
    memory_gate.commit_recall(root)
    try:
        return resolve_bank.Mcp(root, timeout=30).start().call("recall", args), bank, None, None
    except Exception as e:  # noqa: BLE001 - no memory is "unknown territory", never a failed run
        return None, bank, f"recall failed ({type(e).__name__}: {str(e)[:120]})", args


def memory_line(facts, recalled, dropped, reason=None):
    if facts:
        return "Memory: " + " | ".join(f"[{m['kind']}] {m['text']}" for m in facts)
    if reason:
        return f"Memory: none ({reason})"
    if not recalled:
        return "Memory: none (nothing stored about this area)"
    parts = ", ".join(f"{n} {k.replace('_', '-')}" for k, n in dropped.items() if n)
    return f"Memory: none ({recalled} recalled, none about this work: {parts})"


def run(a):
    cwd = a.get("--cwd") or os.getcwd()
    root = memory_gate.repo_root(cwd)
    query = a.get("--query") or ""
    if not query.strip():
        raise ValueError("--query is required")
    cap = int(a.get("--cap") or confidence.MAX_RELEVANT)
    out = Path(a["--out"]) if a.get("--out") else default_out(root)
    if not out.is_absolute():
        out = Path(cwd) / out
    raw, bank, reason, fallback, source = None, None, None, None, "none"
    src = a.get("--from") or a.get("--reuse")
    if src and Path(src).is_file():
        raw = Path(src).read_text(encoding="utf-8-sig")
        source = "file" if a.get("--from") else "reused"
    elif a.get("--from"):
        reason = f"recall file not found: {a['--from']}"
    else:
        res, bank, reason, fallback = live(query, cwd, root)
        if res is not None:
            raw, source = json.dumps(res, ensure_ascii=False), "live"
    saved = None
    if raw is not None:
        if source != "file" or Path(src).resolve() != out.resolve():
            ladder_state.write_text(out, raw)
            ladder_state.ensure_gitignore(root)
        saved = str(out)
        mems = confidence.parse_memories(raw)
    else:
        mems = []
    facts, dropped = confidence.select_relevant(mems, query, a["--paths"], root=root, cap=cap)
    result = {"memory": memory_line(facts, len(mems), dropped, reason), "facts": [f"[{m['kind']}] {m['text']}" for m in facts],
              "recalled": len(mems), "kept": len(facts), "dropped": dropped, "source": source,
              "saved_to": saved, "bank": bank}
    if reason:
        result["reason"] = reason
    if fallback:
        result["mcp_fallback"] = fallback
        result["note"] = ("make one Hindsight MCP `recall` with exactly mcp_fallback (plus bank_id if the "
                          "connector needs it), save its result to a file, and run recall.py again with --from")
    return result


def main(argv):
    ladder_state.utf8_stdio()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    try:
        a = parse(argv)
        print(json.dumps(run(a), indent=2, ensure_ascii=False))
        return 0
    except ValueError as e:
        print(f"recall.py: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
