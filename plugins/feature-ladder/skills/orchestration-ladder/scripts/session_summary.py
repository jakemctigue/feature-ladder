#!/usr/bin/env python3
"""
feature-ladder session summary: what every thread of the session cost, and what
is left on the OpenRouter and Hindsight accounts. Costs no model tokens.

    session_summary.py [--cwd DIR] [--slug SLUG | --all] [--json] [--offline]

  (default)  every thread since this coding session started (session.json),
             over all of its feature runs; with no session, the last run
  --slug     one feature run (the block a feature's own report ends with)
  --all      every run this repository has a ledger for
  --offline  no network: prices from the cached catalogue, no balances

A thread is one model session: the orchestrator (the GLM 5.3 coordinator, or a
Claude coordinator subagent), each worker card (OpenRouter through
external_worker.py or OpenClaude, Silk with --no-bs, or a Claude subagent),
each design or integration step, and this conversation itself when its
transcript can be found. One row each, from the run ledgers
(.feature-ladder/runs/<slug>/usage.jsonl).

Cost, per thread:
  OpenRouter   the thread's tokens at the model's listed price, marked "est.".
               OpenRouter's usage counter trails each request by up to a
               minute, so the difference around one thread can be zero or
               hold the thread before it; that difference is used (marked
               "key") only for a thread that reported no tokens. Under the
               table is what the key was really charged since the session's
               first OpenRouter thread: that total is exact.
  Silk         the drop in the Silk balance around the thread (approximate).
  Claude plan  no money is charged per token; the figure in brackets is what
               the same tokens would cost at Anthropic's list prices.

Balances:
  OpenRouter   credits left on the account (bought minus used) and what is
               left of this key's own limit.
  Hindsight    Hindsight Cloud has no balance endpoint: the summary shows that
               the key works, this repository's bank, how many memories it
               holds, what this session read and wrote, and where the prepaid
               credit balance is shown (the Billing page).
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ladder_state  # noqa: E402
import model_catalog  # noqa: E402

BILLING_PAGE = "the Billing page of Hindsight Cloud"
PROVIDERS = (("(openrouter)", "OpenRouter"), ("(openclaude)", "OpenRouter"), ("(silk)", "Silk"),
             ("(claude plan)", "Claude plan"))


def billed_to(entry):
    agent = str(entry.get("agent") or "").lower()
    for mark, name in PROVIDERS:
        if mark in agent:
            return name
    return "Claude plan" if model_catalog.claude_family(entry.get("model")) or "/" not in str(entry.get("model") or "") \
        else "OpenRouter"


def thread_cost(entry, rows):
    """{"usd", "basis"} for one ledger row. basis: charged | share | estimate |
    plan (an API-price equivalent, nothing is charged) | unknown."""
    who = billed_to(entry)
    est = model_catalog.estimate_usd(entry.get("model"), entry, rows)
    charged = entry.get("cost_usd")
    try:
        charged = float(charged) if charged is not None else None
    except (TypeError, ValueError):
        charged = None
    if who == "Claude plan":
        return {"usd": est, "basis": "plan" if est is not None else "unknown"}
    if who == "Silk":  # no listed prices: the balance drop around the thread is all there is
        return {"usd": charged, "basis": "charged" if charged is not None else "unknown"}
    if est is not None and entry.get("total"):
        return {"usd": est, "basis": "estimate"}
    if charged and charged > 0:
        return {"usd": charged, "basis": "share" if entry.get("cost_group") else "charged"}
    return {"usd": None, "basis": "unknown"}


def _entries(file):
    out = []
    try:
        for line in file.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
    except OSError:
        pass
    return out


def ledger_rows(root, slug=None, since=None, everything=False):
    """(rows, slugs). `slug`: that run; `since`: rows written at or after it,
    over every run; `everything`: all of them."""
    runs = ladder_state.state_dir(root) / "runs"
    rows = []
    files = [runs / slug / "usage.jsonl"] if slug else sorted(runs.glob("*/usage.jsonl"))
    for f in files:
        for e in _entries(f):
            e.setdefault("slug", f.parent.name)
            if slug or everything or since is None or str(e.get("at") or "") >= since:
                rows.append(e)
    rows.sort(key=lambda e: str(e.get("at") or ""))
    return rows, list(dict.fromkeys(e.get("slug") for e in rows if e.get("slug")))


def conversation_thread(session_id):
    """This conversation's own tokens, from its transcript (found by session id
    under ~/.claude/projects). None when there is no id or no transcript."""
    if not session_id:
        return None
    try:
        sys.path.insert(0, str(HERE.parents[2] / "hooks" / "scripts"))
        import ledger  # noqa: E402
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
        files = list(base.glob(f"*/{session_id}.jsonl"))
        if not files:
            return None
        s = ledger.summarize_transcript(files[0])
        if not s.get("turns"):
            return None
        return {"agent": "this conversation", "rung": "conversation", "task": "-", "model": s.get("model"),
                "turns": s["turns"], "input": s["input"], "cache_read": s["cache_read"],
                "cache_write": s["cache_write"], "output": s["output"], "total": s["total"], "cost_usd": None}
    except Exception:  # noqa: BLE001 - the summary never fails over an unreadable transcript
        return None


def _get(url, key, timeout=15):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def hindsight_status(root, cwd, since=None, offline=False, get=_get):
    """What can be known about the Hindsight account from its API: the key works,
    the bank and its size, this session's reads and writes. Its credit balance is
    not one of them (there is no endpoint), which the summary says plainly."""
    out = {"connected": False, "bank": None, "memories": None, "last_write": None, "written_this_session": 0,
           "recalls_this_run": 0, "balance_usd": None,
           "balance_note": f"Hindsight Cloud's API has no balance endpoint; the prepaid credit balance is on {BILLING_PAGE}"}
    try:
        sys.path.insert(0, str(HERE.parents[1] / "hindsight-memory" / "scripts"))
        import resolve_bank  # noqa: E402
    except Exception as e:  # noqa: BLE001 - the Claude app keeps the scripts elsewhere
        return dict(out, error=f"hindsight scripts not found ({type(e).__name__})")
    written = 0
    for e in _entries(Path(root) / ".feature-ladder" / "memory-ledger.jsonl"):
        if since is None or str(e.get("at") or "") >= since:
            written += 1
    out["written_this_session"] = written
    gate = ladder_state.read_json(Path(root) / ".feature-ladder" / "memory-gate.json")
    if isinstance(gate, dict) and isinstance(gate.get("recalls"), dict):
        out["recalls_this_run"] = sum(int(v or 0) for v in gate["recalls"].values() if str(v).isdigit() or isinstance(v, int))
    ok, where = resolve_bank.key_status(cwd)
    if not ok:
        return dict(out, error=f"Hindsight is off: {where}")
    url = resolve_bank.setting("HINDSIGHT_MCP_URL", cwd)[0] or resolve_bank.DEFAULT_URL
    key = resolve_bank.setting("HINDSIGHT_API_KEY", cwd)[0] or ""
    if not url.startswith("https://api.hindsight.vectorize.io"):
        out["balance_note"] = "a self-hosted Hindsight server has no credit balance"
    if offline:
        return dict(out, error="offline: not checked")
    try:
        bank = (resolve_bank.resolve(cwd, offline=True) or {}).get("bank_id")
    except Exception:  # noqa: BLE001
        bank = None
    out["bank"] = bank
    base = url.rstrip("/")
    base = base[:-4] if base.endswith("/mcp") else base
    try:
        if bank:
            s = get(f"{base}/v1/default/banks/{urllib.parse.quote(bank, safe='')}/stats", key)
            out.update(connected=True, memories=s.get("total_nodes"), last_write=s.get("last_memory_write_at"))
        else:
            get(f"{base}/v1/default/banks", key)
            out["connected"] = True
    except urllib.error.HTTPError as e:
        if e.code == 404 and bank:
            out.update(connected=True, memories=0)  # the bank is created by the first write
        elif e.code == 402:
            out.update(connected=True, error="Hindsight says payment is required: the credit balance is used up")
        else:
            out["error"] = f"Hindsight answered HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        out["error"] = f"Hindsight not reachable ({type(e).__name__})"
    return out


def build(cwd, slug=None, everything=False, offline=False):
    root = ladder_state.state_root(cwd)
    sess = ladder_state.read_json(ladder_state.state_dir(root) / "session.json")
    sess = sess if isinstance(sess, dict) else {}
    run = ladder_state.read_json(ladder_state.state_dir(root) / "run.json")
    run = run if isinstance(run, dict) else {}
    since = None if (slug or everything) else sess.get("started")
    if not slug and not everything and not since:
        slug = run.get("slug")  # no session on record: the last run
    rows, slugs = ledger_rows(root, slug=slug, since=since, everything=everything)
    catalogue, _ = model_catalog.catalog(offline=offline)
    threads = []
    if not slug:
        me = conversation_thread(sess.get("session_id"))
        if me:
            rows = [me] + rows
    for e in rows:
        c = thread_cost(e, catalogue)
        threads.append({"thread": str(e.get("task") or "-"), "feature": e.get("slug"), "rung": e.get("rung") or "?",
                        "agent": e.get("agent"), "model": e.get("model") or "?", "billed_to": billed_to(e),
                        "turns": int(e.get("turns") or 0), "seconds": e.get("seconds"),
                        "input": int(e.get("input") or 0) + int(e.get("cache_read") or 0) + int(e.get("cache_write") or 0),
                        "output": int(e.get("output") or 0), "cost_usd": c["usd"], "cost_basis": c["basis"]})
    totals = {}
    for t in threads:
        s = totals.setdefault(t["billed_to"], {"threads": 0, "usd": 0.0, "estimated_usd": 0.0, "unknown": 0})
        s["threads"] += 1
        if t["cost_usd"] is None:
            s["unknown"] += 1
        else:
            s["usd"] += t["cost_usd"]
            if t["cost_basis"] == "estimate":
                s["estimated_usd"] += t["cost_usd"]
    for s in totals.values():
        s["usd"], s["estimated_usd"] = round(s["usd"], 4), round(s["estimated_usd"], 4)
    spawned = sess.get("spawned") or []
    out = {"scope": f"run {slug}" if slug else ("every run" if everything else "this session"),
           "since": since, "features": slugs,
           "subagents": {"used": len(spawned), "max": sess.get("max_agents"), "size": sess.get("size")},
           "orchestrator": (run.get("orchestrator") or {}).get("label") if isinstance(run.get("orchestrator"), dict) else None,
           "threads": threads, "totals": totals}
    try:
        import external_worker as ew  # noqa: E402
        cfg = ew.load_config(cwd, no_bs=False)
        out["budget_level"] = cfg.get("budget")
        if offline:
            out["openrouter"] = {"error": "offline: not checked"}
        else:
            key, source = ew.get_key(cfg, cwd)
            out["openrouter"] = model_catalog.openrouter_balance(key, cfg["openrouter"]["base_url"]) if key \
                else {"error": f"no OpenRouter key ({source})"}
    except Exception as e:  # noqa: BLE001
        out["openrouter"] = {"error": f"not checked ({type(e).__name__})"}
    # What the key was really charged over the session: its usage now, minus its usage
    # when the session's first OpenRouter thread started (external_worker.note_usage_baseline).
    base = ladder_state.read_json(ladder_state.state_dir(root) / "openrouter-usage.json")
    used = (out.get("openrouter") or {}).get("key_used_usd")
    if since and isinstance(base, dict) and base.get("session_started") == since and used is not None \
            and base.get("usage_usd") is not None:
        try:
            out["openrouter"]["session_charged_usd"] = round(max(0.0, float(used) - float(base["usage_usd"])), 4)
        except (TypeError, ValueError):
            pass
    out["hindsight"] = hindsight_status(root, cwd, since=since, offline=offline)
    return out


def _usd(v, digits=4):
    return "?" if v is None else f"${v:,.{digits}f}"


def _cost_cell(t):
    if t["cost_usd"] is None:
        return "plan" if t["billed_to"] == "Claude plan" else "unknown"
    if t["cost_basis"] == "plan":
        return f"plan (~{_usd(t['cost_usd'])} at API prices)"
    return _usd(t["cost_usd"]) + {"estimate": " est.", "share": " (share, key)",
                                  "charged": " (key)" if t["billed_to"] == "OpenRouter" else ""}.get(t["cost_basis"], "")


def render(s):
    sub = s["subagents"]
    head = [f"## Session summary ({s['scope']})", "",
            "Features: " + (", ".join(s["features"]) if s["features"] else "none on the ledger")
            + (f"   Subagents: {sub['used']} of {sub['max']}" if sub.get("max") else "")
            + (f"   Orchestrator: {s['orchestrator']}" if s.get("orchestrator") else "")
            + (f"   Budget level: {s['budget_level']}" if s.get("budget_level") else ""), ""]
    if s["threads"]:
        head += ["| Thread | Rung | Model | Billed to | Turns | Time | Input | Output | Cost |",
                 "|---|---|---|---|---|---|---|---|---|"]
        for t in s["threads"]:
            name = t["thread"] if t["thread"] != "-" else (t["agent"] or t["rung"])
            if t.get("feature") and len(s["features"]) > 1:
                name = f"{t['feature']} / {name}"
            head.append(f"| {name} | {t['rung']} | {t['model']} | {t['billed_to']} | {t['turns']} | "
                        f"{'-' if t['seconds'] is None else str(t['seconds']) + 's'} | {t['input']:,} | {t['output']:,} | "
                        f"{_cost_cell(t)} |")
        head.append("")
        for who, tot in sorted(s["totals"].items()):
            if who == "Claude plan":
                line = f"Claude plan: {tot['threads']} threads, nothing charged per token (~{_usd(tot['usd'])} at API prices)"
            else:
                line = f"{who}: {tot['threads']} threads, {_usd(tot['usd'])}"
                if tot["estimated_usd"]:
                    line += f" (of it {_usd(tot['estimated_usd'])} estimated from tokens at list price)"
            if tot["unknown"]:
                line += f"; {tot['unknown']} with no figure"
            head.append(line)
    else:
        head.append("No thread is on the ledger for this scope (no subagent or external worker has finished).")
    head.append("")
    o = s.get("openrouter") or {}
    if o.get("session_charged_usd") is not None:
        head.append(f"OpenRouter charged this key {_usd(o['session_charged_usd'])} since the session's first "
                    "OpenRouter thread (exact; the per-thread figures are tokens at list price).")
    if o.get("account_left_usd") is not None:
        line = (f"OpenRouter balance: {_usd(o['account_left_usd'], 2)} left on the account "
                f"({_usd(o['account_bought_usd'], 2)} bought, {_usd(o['account_used_usd'], 2)} used)")
        if o.get("key_limit_usd") is not None:
            line += f"; this key has {_usd(o.get('key_left_usd'), 2)} left of its {_usd(o['key_limit_usd'], 2)} limit"
        head.append(line)
    elif o.get("key_left_usd") is not None:
        head.append(f"OpenRouter balance: this key has {_usd(o['key_left_usd'], 2)} left of its "
                    f"{_usd(o.get('key_limit_usd'), 2)} limit (the account's credits could not be read with this key)")
    elif o.get("key_used_usd") is not None:
        head.append(f"OpenRouter balance: not readable with this key (it has used {_usd(o['key_used_usd'], 2)} and has no limit)")
    else:
        head.append(f"OpenRouter balance: not available ({o.get('error') or o.get('credits_error') or 'no answer'})")
    h = s.get("hindsight") or {}
    if h.get("connected"):
        line = f"Hindsight: connected, bank {h.get('bank') or '(none for this folder)'}"
        if h.get("memories") is not None:
            line += f", {h['memories']} memories stored"
        line += ", {} written {}".format(h.get("written_this_session", 0), "this session" if s.get("since") else "in all")
        if h.get("error"):
            line += f". {h['error']}"
        head.append(line + f". Balance: {h['balance_note']}.")
    else:
        head.append(f"Hindsight: {h.get('error') or 'not checked'}. Balance: {h.get('balance_note')}.")
    return "\n".join(head) + "\n"


def main(argv):
    ladder_state.utf8_stdio()
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cwd, slug, everything, as_json, offline = os.getcwd(), None, False, False, False
    it = iter(argv)
    for a in it:
        if a == "--cwd":
            cwd = next(it, cwd)
        elif a == "--slug":
            slug = next(it, None)
        elif a == "--all":
            everything = True
        elif a == "--json":
            as_json = True
        elif a == "--offline":
            offline = True
        else:
            print(f"session_summary: unknown option {a} (it takes --cwd, --slug, --all, --json, --offline)", file=sys.stderr)
            return 1
    s = build(cwd, slug=slug, everything=everything, offline=offline)
    sys.stdout.write(json.dumps(s, indent=2) + "\n" if as_json else render(s))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
