#!/usr/bin/env python3
"""
Token ledger for feature-ladder runs. Costs no model tokens.

As a SubagentStop hook it reads the finished subagent's transcript, sums its
token usage, and appends one record to
    <repo>/.feature-ladder/runs/<slug>/usage.jsonl
(slug from .feature-ladder/run.json; nothing is written when there is no
run.json, so repositories that never ran the ladder are untouched). Only a
subagent of the run is recorded: the run is active, or the subagent started
while it was (the coordinator stops just after `finish`). A subagent spawned
days later in the same repository is not added to the old run. A record
carries the rung (agent type), model, effort and task id parsed from the Task
Card, turns, and input / cache-read / cache-write / output token counts.
Claude Code writes one transcript line per content block of an assistant
message, each repeating the message's usage, so usage is counted once per
message id and `turns` is the number of API messages. The same subagent
stopping twice (the retain gate bounced it once) replaces its earlier record
instead of double counting.

    ledger.py report [--slug S] [--cwd DIR] [--json]
        Markdown table by rung and model for FEATURE-REPORT.md: agents, turns,
        tokens by kind, and each rung's share of output tokens (the expensive
        kind). --json prints the aggregate instead.

The hook never blocks and never prints to stdout; any failure is silent.
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "skills" / "orchestration-ladder" / "scripts"))

RUNG_RE = re.compile(r"Rung:\s*([\w-]+)\s*\(\s*([\w.-]+)\s*/\s*([\w-]+)\s*\)")
TASK_RE = re.compile(r"\bTASK\s+([A-Za-z0-9._-]+)\s*[:\-]")
USAGE_KEYS = {"input_tokens": "input", "cache_read_input_tokens": "cache_read",
              "cache_creation_input_tokens": "cache_write", "output_tokens": "output"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def summarize_transcript(path):
    """Sum usage over the API messages of a JSONL transcript.

    One assistant message is written as several lines (one per content block)
    that share the message id and repeat its usage: the last line per id is the
    message's usage. Lines without an id each count as their own message."""
    by_id, order = {}, 0
    model, first_user, started = None, "", None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if started is None and isinstance(rec.get("timestamp"), str):
                started = rec["timestamp"]
            msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
            role = msg.get("role")
            if role == "user" and not first_user:
                first_user = _text(msg.get("content"))
            if role != "assistant":
                continue
            usage = msg.get("usage") or rec.get("usage")
            if not isinstance(usage, dict) or not usage:
                continue
            model = msg.get("model") or model
            order += 1
            by_id[msg.get("id") or rec.get("requestId") or f"line-{order}"] = usage
    totals = {k: 0 for k in USAGE_KEYS.values()}
    for usage in by_id.values():
        for k, name in USAGE_KEYS.items():
            try:
                totals[name] += int(usage.get(k) or 0)
            except (TypeError, ValueError):
                pass
    totals["total"] = totals["input"] + totals["cache_read"] + totals["cache_write"] + totals["output"]
    return {"turns": len(by_id), "model": model, "first_user": first_user, "started": started, **totals}


def _when(text):
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def belongs_to_run(run, started):
    """Is a subagent whose transcript began at `started` part of this run?"""
    if run.get("active"):
        return True
    begun, ended = _when(run.get("started")), _when(run.get("finished") or run.get("aborted"))
    at = _when(started)
    return bool(begun and ended and at and begun <= at <= ended)


def find_run(cwd):
    from guard_irreversible import find_run as _find
    return _find(cwd)


def ledger_path(root, slug):
    return Path(root) / ".feature-ladder" / "runs" / (slug or "no-slug") / "usage.jsonl"


def record(payload):
    import ladder_state
    agent_type = str(payload.get("agent_type") or payload.get("subagent_type") or "").split(":")[-1]
    # Only the subagent's own transcript: `transcript_path` is the parent session's,
    # and summing that would book the whole session to one worker.
    path = payload.get("agent_transcript_path") or ""
    if not path or not os.path.exists(path):
        return None
    root, run_file = find_run(payload.get("cwd") or os.getcwd())
    if not run_file:
        return None
    run = ladder_state.read_json(run_file)
    if not isinstance(run, dict):
        return None
    summary = summarize_transcript(path)
    if not belongs_to_run(run, summary.pop("started", None)):
        return None
    card = summary.pop("first_user", "")
    m = RUNG_RE.search(card)
    rung = m.group(1) if m else agent_type or "unknown"
    task = TASK_RE.search(card)
    entry = {
        "at": now(),
        "key": str(payload.get("agent_id") or Path(path).stem),
        "slug": run.get("slug"),
        "agent": agent_type or rung,
        "rung": rung,
        "model": summary.pop("model") or (m.group(2) if m else None),
        "effort": m.group(3) if m else None,
        "task": task.group(1) if task else None,
        **summary,
    }
    out = ledger_path(root, run.get("slug"))
    # Parallel workers stop at the same moment: read-modify-write under the lock.
    with ladder_state.locked(root, "ledger"):
        lines = []
        if out.is_file():
            for line in out.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("key") == entry["key"]:
                        continue
                except (ValueError, AttributeError):
                    pass  # keep a line we cannot parse; it is not ours to drop
                lines.append(line)
        lines.append(json.dumps(entry, sort_keys=True))
        ladder_state.write_text(out, "\n".join(lines) + "\n")
    ladder_state.ensure_gitignore(root)
    return entry


# ------------------------------------------------------------------ report ---
def load(root, slug=None):
    if slug is None:
        run_file = Path(root) / ".feature-ladder" / "run.json"
        if run_file.is_file():
            try:
                slug = json.loads(run_file.read_text(encoding="utf-8")).get("slug")
            except ValueError:
                slug = None
    p = ledger_path(root, slug)
    entries = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                entries.append(e)
    return slug, entries


def aggregate(entries):
    rows = {}
    for e in entries:
        key = (e.get("rung") or "unknown", e.get("model") or "?", e.get("effort") or "")
        r = rows.setdefault(key, {"agents": 0, "turns": 0, "input": 0, "cache_read": 0,
                                  "cache_write": 0, "output": 0, "total": 0})
        r["agents"] += 1
        for k in ("turns", "input", "cache_read", "cache_write", "output", "total"):
            r[k] += int(e.get(k) or 0)
    return rows


def _n(v):
    return f"{v:,}"


def table(slug, entries):
    rows = aggregate(entries)
    if not rows:
        return f"## Token usage\nNo ledger for run `{slug or '-'}` (no subagent has finished yet, or hooks are off).\n"
    out_total = sum(r["output"] for r in rows.values()) or 1
    head = ["## Token usage (per rung, from the SubagentStop ledger)", "",
            "| Rung | Model / effort | Agents | Turns | Input | Cache read | Cache write | Output | Output share |",
            "|---|---|---|---|---|---|---|---|---|"]
    body = []
    for (rung, model, effort), r in sorted(rows.items(), key=lambda kv: -kv[1]["output"]):
        me = f"{model}/{effort}" if effort else model
        body.append(f"| {rung} | {me} | {r['agents']} | {r['turns']} | {_n(r['input'])} | {_n(r['cache_read'])} | "
                    f"{_n(r['cache_write'])} | {_n(r['output'])} | {100 * r['output'] // out_total}% |")
    tot = {k: sum(r[k] for r in rows.values()) for k in ("agents", "turns", "input", "cache_read", "cache_write", "output")}
    body.append(f"| **all** | | {tot['agents']} | {tot['turns']} | {_n(tot['input'])} | {_n(tot['cache_read'])} | "
                f"{_n(tot['cache_write'])} | {_n(tot['output'])} | 100% |")
    tail = ["", "Counts come from each subagent's transcript when it stopped. A rung that is still running "
            "(usually the coordinator) is not in the table until it finishes; the slash command prints the "
            "complete table after the run."]
    return "\n".join(head + body + tail) + "\n"


def main(argv):
    if argv and argv[0] == "report":
        import ladder_state
        ladder_state.utf8_stdio()
        cwd, slug, as_json = os.getcwd(), None, False
        it = iter(argv[1:])
        for a in it:
            if a == "--cwd":
                cwd = next(it, cwd)
            elif a == "--slug":
                slug = next(it, None)
            elif a == "--json":
                as_json = True
        root, _ = find_run(cwd)
        root = root or Path(cwd)
        slug, entries = load(root, slug)
        if as_json:
            print(json.dumps({"slug": slug, "rows": {"/".join(k): v for k, v in aggregate(entries).items()},
                              "entries": entries}, indent=2))
        else:
            sys.stdout.write(table(slug, entries))
        return 0
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    # hook mode: payload on stdin; never block, never print
    try:
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
        record(payload)
    except Exception:  # noqa: BLE001 - a ledger must never break a run
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
