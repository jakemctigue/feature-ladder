#!/usr/bin/env python3
"""
feature-ladder benchmark: run fixed Task Cards through the real worker path
(external_worker.py -> a child `claude -p` on OpenRouter) and grade each with
its own check, so a model choice per rung rests on measured pass rate, cost
and time. Spends OpenRouter credits (cents); no Claude plan tokens.

    ladder_bench.py run [--level low|standard|high] [--compare] [--models RUNG=ID[,ID...] ...]
                        [--tasks ID,...] [--repeat N] [--parallel N] [--timeout S]
                        [--out FILE.json] [--md FILE.md]
        Without --compare: every task on its rung's model at the active budget
        level (the installed configuration). With --compare: every task on each
        of its rung's candidate models (CANDIDATES, or --models), to choose the
        level table. Each run gets a fresh temporary git repository; nothing in
        this repository is touched. Prints a Markdown table and a per-model
        summary; exit 0 when every card of the non-compare run passed, else 1.

    ladder_bench.py tasks        list the tasks

Tasks (all graded by a command, never by a model):
    mech-offbyone    T0  a pagination off-by-one, one failing test
    mech-rename      T0  rename a config key across three files and their test
    impl-ttlcache    T2  write an LRU cache with expiry to pass the given tests
    impl-report      T2  fix a quoted-field parser bug and add a grouped summary
    evid-docs        T1  read three documents and report four facts (read only)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[1]
SCRIPTS = PLUGIN / "skills" / "orchestration-ladder" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import external_worker as ew  # noqa: E402
import ladder_state  # noqa: E402
import model_catalog as mc  # noqa: E402

CANDIDATES = {
    "mechanic": [mc.FLASH, mc.GLM, mc.INKLING_MINI],
    "implementer": [mc.FLASH, mc.GLM, mc.INKLING_SMALL, mc.KIMI],
    "evidence-collector": [mc.INKLING, mc.INKLING_SMALL, mc.INKLING_MINI],
}
PY = "python" if os.name == "nt" else "python3"

# ---------------------------------------------------------------- fixtures --
OFFBYONE = {
    "pager.py": '''def page(items, number, size):
    """Page `number` (1-based) of `items`, `size` items a page."""
    start = number * size
    return items[start:start + size]


def page_count(items, size):
    return len(items) // size
''',
    "test_pager.py": '''import unittest
from pager import page, page_count


class Pager(unittest.TestCase):
    def test_first_page(self):
        self.assertEqual(page(list(range(10)), 1, 3), [0, 1, 2])

    def test_last_partial_page(self):
        self.assertEqual(page(list(range(10)), 4, 3), [9])

    def test_count_rounds_up(self):
        self.assertEqual(page_count(list(range(10)), 3), 4)
        self.assertEqual(page_count(list(range(9)), 3), 3)
        self.assertEqual(page_count([], 3), 0)


if __name__ == "__main__":
    unittest.main()
''',
}
RENAME = {
    "settings.py": 'DEFAULTS = {"max_conn": 10, "timeout_s": 30}\n\n\ndef load(overrides=None):\n'
                   '    cfg = dict(DEFAULTS)\n    cfg.update(overrides or {})\n    return cfg\n',
    "pool.py": 'from settings import load\n\n\nclass Pool:\n    def __init__(self, overrides=None):\n'
               '        self.limit = load(overrides)["max_conn"]\n        self.open = 0\n\n'
               '    def acquire(self):\n        if self.open >= self.limit:\n'
               '            raise RuntimeError("max_conn reached")\n        self.open += 1\n',
    "report.py": 'from settings import load\n\n\ndef describe(overrides=None):\n    cfg = load(overrides)\n'
                 '    return f"up to {cfg[\'max_conn\']} connections, {cfg[\'timeout_s\']}s timeout"\n',
    "test_pool.py": 'import unittest\nfrom pool import Pool\nfrom report import describe\nfrom settings import DEFAULTS\n\n\n'
                    'class T(unittest.TestCase):\n    def test_key_is_renamed_everywhere(self):\n'
                    '        self.assertIn("max_connections", DEFAULTS)\n        self.assertNotIn("max_conn", DEFAULTS)\n'
                    '        self.assertEqual(Pool({"max_connections": 2}).limit, 2)\n'
                    '        self.assertEqual(describe({"max_connections": 7}), "up to 7 connections, 30s timeout")\n\n'
                    '    def test_limit_is_enforced(self):\n        p = Pool({"max_connections": 1})\n        p.acquire()\n'
                    '        with self.assertRaisesRegex(RuntimeError, "max_connections reached"):\n            p.acquire()\n\n\n'
                    'if __name__ == "__main__":\n    unittest.main()\n',
}
TTLCACHE = {
    "cache.py": '"""TTLCache: see test_cache.py for the behaviour."""\n',
    "test_cache.py": '''import unittest
from cache import TTLCache


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class T(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.c = TTLCache(max_size=2, ttl=10, clock=self.clock)

    def test_get_set_and_default(self):
        self.c.set("a", 1)
        self.assertEqual(self.c.get("a"), 1)
        self.assertIsNone(self.c.get("missing"))
        self.assertEqual(self.c.get("missing", 7), 7)

    def test_entries_expire_at_ttl(self):
        self.c.set("a", 1)
        self.clock.t += 9.9
        self.assertEqual(self.c.get("a"), 1)
        self.clock.t += 0.2
        self.assertIsNone(self.c.get("a"))
        self.assertEqual(len(self.c), 0)

    def test_per_entry_ttl_overrides_the_default(self):
        self.c.set("a", 1, ttl=1)
        self.c.set("b", 2)
        self.clock.t += 2
        self.assertIsNone(self.c.get("a"))
        self.assertEqual(self.c.get("b"), 2)

    def test_least_recently_used_is_evicted(self):
        self.c.set("a", 1)
        self.c.set("b", 2)
        self.c.get("a")          # a is now the most recently used
        self.c.set("c", 3)       # evicts b
        self.assertEqual(self.c.get("a"), 1)
        self.assertIsNone(self.c.get("b"))
        self.assertEqual(self.c.get("c"), 3)

    def test_expired_entries_are_dropped_before_live_ones(self):
        self.c.set("a", 1, ttl=1)
        self.c.set("b", 2)
        self.clock.t += 2
        self.c.set("c", 3)       # a is expired: it goes, b stays
        self.assertEqual(self.c.get("b"), 2)
        self.assertEqual(self.c.get("c"), 3)

    def test_overwrite_refreshes_value_and_expiry(self):
        self.c.set("a", 1)
        self.clock.t += 8
        self.c.set("a", 2)
        self.clock.t += 8
        self.assertEqual(self.c.get("a"), 2)

    def test_delete_contains_and_len(self):
        self.c.set("a", 1)
        self.assertIn("a", self.c)
        self.assertTrue(self.c.delete("a"))
        self.assertFalse(self.c.delete("a"))
        self.assertNotIn("a", self.c)
        self.assertEqual(len(self.c), 0)

    def test_bad_arguments_are_refused(self):
        with self.assertRaises(ValueError):
            TTLCache(max_size=0, ttl=10)
        with self.assertRaises(ValueError):
            TTLCache(max_size=2, ttl=0)


if __name__ == "__main__":
    unittest.main()
''',
}
REPORT = {
    "parse.py": '''def split_line(line):
    """One CSV line into fields. Fields may be quoted with double quotes; a quoted
    field may contain commas, and "" inside it is one quote character."""
    return [f.strip() for f in line.rstrip("\\n").split(",")]


def read(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header = split_line(lines[0])
    return [dict(zip(header, split_line(ln))) for ln in lines[1:]]
''',
    "summary.py": '"""summarize(rows): see test_report.py."""\n',
    "test_report.py": '''import unittest
from decimal import Decimal
from parse import read, split_line
from summary import summarize

DATA = \'\'\'region,customer,amount
north,"Acme, Inc.",10.10
north,Bolt,5.255
south,"Say ""Hi"" Ltd",7
south,Dune,0.005
north,"Acme, Inc.",1.00
\'\'\'


class T(unittest.TestCase):
    def test_quoted_fields_keep_their_commas_and_quotes(self):
        self.assertEqual(split_line('a,"b, c",d'), ["a", "b, c", "d"])
        self.assertEqual(split_line('"say ""hi""",x'), ['say "hi"', "x"])
        self.assertEqual(split_line("plain, spaced ,x"), ["plain", "spaced", "x"])
        self.assertEqual(split_line('a,"",c'), ["a", "", "c"])

    def test_read_uses_the_header(self):
        rows = read(DATA)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], {"region": "north", "customer": "Acme, Inc.", "amount": "10.10"})

    def test_summary_groups_sums_and_rounds_half_up(self):
        out = summarize(read(DATA), by="region", value="amount")
        self.assertEqual(out, [
            {"region": "north", "count": 3, "total": Decimal("16.36")},
            {"region": "south", "count": 2, "total": Decimal("7.01")},
        ])

    def test_summary_orders_by_total_descending_then_key(self):
        rows = [{"k": "b", "v": "1"}, {"k": "a", "v": "1"}, {"k": "c", "v": "5"}]
        self.assertEqual([r["k"] for r in summarize(rows, by="k", value="v")], ["c", "a", "b"])

    def test_a_bad_amount_names_its_row(self):
        with self.assertRaisesRegex(ValueError, "row 2"):
            summarize([{"k": "a", "v": "1"}, {"k": "a", "v": "oops"}], by="k", value="v")


if __name__ == "__main__":
    unittest.main()
''',
}
DOCS = {
    "docs/limits.md": "# Limits\n\nThe public API allows 120 requests per minute per key. Batch endpoints are separate: "
                      "20 requests per minute. Since release 2.4.1 a 429 response carries a `Retry-After` header in seconds.\n",
    "docs/changelog.md": "# Changelog\n\n## 2.4.1\n- Added Retry-After on 429.\n\n## 2.4.0\n- The upload size limit rose "
                         "from 25 MB to 40 MB.\n- `LEGACY_AUTH` was removed; set `ORBIT_API_TOKEN` instead.\n\n## 2.3.0\n"
                         "- First release with batch endpoints.\n",
    "docs/deploy.md": "# Deploy\n\nSet `ORBIT_API_TOKEN` and `ORBIT_REGION` (default `eu-west-2`). Health check: "
                      "`GET /healthz` must return 200 within 3 seconds. Rollback is `orbit rollback --to <release>`.\n",
    "README.md": "# Orbit client\nSee docs/.\n",
}
DOC_FACTS = [("rate limit", r"\b120\b"), ("upload limit", r"\b40\s*MB\b"),
             ("token variable", r"ORBIT_API_TOKEN"), ("default region", r"eu-west-2")]

TASKS = {
    "mech-offbyone": {"rung": "mechanic", "files": OFFBYONE, "check": f"{PY} -m unittest -q test_pager",
                      "card": "TASK {id}: Fix the pagination bugs in pager.py so test_pager.py passes\n"
                              "Rung: mechanic (haiku/low)   Router score: 1   Flags: none\n"
                              "Scope: pager.py\nDesign: N/A - the request is the design\n"
                              "Done when: {check}\nDo not: change test_pager.py\nMemory: none\nSecrets: none\n"
                              "Return: Evidence Packet"},
    "mech-rename": {"rung": "mechanic", "files": RENAME, "check": f"{PY} -m unittest -q test_pool",
                    "card": "TASK {id}: Rename the config key max_conn to max_connections everywhere (settings.py, "
                            "pool.py, report.py, and the error message) so test_pool.py passes\n"
                            "Rung: mechanic (haiku/low)   Router score: 3   Flags: none\n"
                            "Scope: settings.py, pool.py, report.py\nDesign: N/A - the request is the design\n"
                            "Done when: {check}\nDo not: change test_pool.py\nMemory: none\nSecrets: none\n"
                            "Return: Evidence Packet"},
    "impl-ttlcache": {"rung": "implementer", "files": TTLCACHE, "check": f"{PY} -m unittest -q test_cache",
                      "card": "TASK {id}: Implement TTLCache in cache.py (LRU eviction at max_size, per-entry expiry, "
                              "injectable clock) so every test in test_cache.py passes\n"
                              "Rung: implementer (sonnet/medium)   Router score: 5   Flags: none\n"
                              "Scope: cache.py\nDesign: N/A - test_cache.py is the specification\n"
                              "Done when: {check}\nDo not: change test_cache.py; add no dependency\nMemory: none\n"
                              "Secrets: none\nReturn: Evidence Packet"},
    "impl-report": {"rung": "implementer", "files": REPORT, "check": f"{PY} -m unittest -q test_report",
                    "card": "TASK {id}: Make split_line in parse.py handle quoted fields, and write summarize(rows, by, "
                            "value) in summary.py (group, count, Decimal total rounded half up to 2 places, ordered by "
                            "total descending then key, a bad amount raises ValueError naming its 1-based row) so "
                            "test_report.py passes\n"
                            "Rung: implementer (sonnet/medium)   Router score: 6   Flags: none\n"
                            "Scope: parse.py, summary.py\nDesign: N/A - test_report.py is the specification\n"
                            "Done when: {check}\nDo not: change test_report.py; add no dependency\nMemory: none\n"
                            "Secrets: none\nReturn: Evidence Packet"},
    "evid-docs": {"rung": "evidence-collector", "files": DOCS, "check": None, "facts": DOC_FACTS,
                  "card": "TASK {id}: From this repository's docs/ folder only, report four facts with the file each "
                          "comes from: the public API rate limit per key, the current upload size limit, the "
                          "environment variable that holds the API token, and the default region\n"
                          "Rung: evidence-collector (haiku/medium)   Router score: 0   Flags: none\n"
                          "Scope: docs/ (read only)\nDone when: each of the four facts is in Claims with its source file\n"
                          "Do not: use the web; edit anything\nMemory: none\nSecrets: none\nReturn: Evidence Packet"},
}


# -------------------------------------------------------------------- runs --
def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(task, where):
    root = Path(where)
    for rel, text in task["files"].items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init", "-q")
    _git(root, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "add", "-A")
    _git(root, "-c", "user.name=bench", "-c", "user.email=bench@example.invalid", "commit", "-q", "-m", "fixture")
    return root


def grade(task, root, packet):
    """(passed, detail). Coding tasks: the check command, and the tests untouched.
    The evidence task: every fact is in the packet and nothing was edited."""
    changed = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True).stdout
    touched = [ln[3:].strip() for ln in changed.splitlines() if not ln[3:].startswith(".feature-ladder")]
    if task.get("facts"):
        missing = [name for name, pat in task["facts"] if not re.search(pat, packet or "", re.I)]
        if touched:
            return False, f"edited {', '.join(touched)}"
        return (not missing), ("all facts" if not missing else "missing: " + ", ".join(missing))
    tests = [t for t in touched if Path(t).name.startswith("test_")]
    if tests:
        return False, f"changed the tests ({', '.join(tests)})"
    r = subprocess.run([sys.executable] + task["check"].split()[1:], cwd=root, capture_output=True, text=True,
                       timeout=180)  # the card's own check, with this interpreter and no shell
    tail = ((r.stderr or r.stdout).strip().splitlines() or [""])[-1][:120]
    return r.returncode == 0, tail


def run_one(task_id, model, n, timeout, max_turns, rows):
    task = TASKS[task_id]
    tmp = tempfile.mkdtemp(prefix=f"fl-bench-{task_id}-")
    try:
        root = make_repo(task, tmp)
        card = root.parent / f"{Path(tmp).name}-card.md"
        tid = "B1"
        card.write_text(task["card"].format(id=tid, check=task["check"] or "") + "\n", encoding="utf-8", newline="\n")
        env = dict(os.environ, FEATURE_LADDER_RECALL="off")  # a fixture repo has no memory: keep Hindsight out of it
        cmd = [sys.executable, str(SCRIPTS / "external_worker.py"), "run", "--card", str(card), "--id", tid,
               "--rung", task["rung"], "--model", model, "--cwd", str(root), "--timeout", str(timeout),
               "--max-turns", str(max_turns)]
        t0 = time.time()
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 120, env=env,
                               encoding="utf-8", errors="replace")
            code, out = r.returncode, r.stdout or ""
        except subprocess.TimeoutExpired:
            code, out = 1, ""
        secs = round(time.time() - t0)
        rec = ladder_state.read_json(ladder_state.state_dir(root) / "evidence" / f"{tid}-external.json") or {}
        usage = rec.get("usage") or {}
        passed, detail = (False, f"runner exit {code}") if code == 3 else grade(task, root, rec.get("packet") or out)
        est = mc.estimate_usd(model, usage, rows)
        charged = rec.get("openrouter_cost_usd")
        shared = bool(rec.get("openrouter_cost_shared"))
        return {"task": task_id, "rung": task["rung"], "model": model, "run": n, "passed": bool(passed),
                "detail": detail, "exit": code, "seconds": secs, "turns": rec.get("num_turns"),
                "input_tokens": sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens",
                                                                     "cache_creation_input_tokens")),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "charged_usd": None if shared else charged, "estimate_usd": est,
                # Per run the cost is the tokens at the model's list price: OpenRouter's usage
                # counter trails a request by up to a minute and every run here shares one key,
                # so a per-run difference of the counter is not that run's cost. What the whole
                # benchmark was really charged is in the header.
                "cost_usd": est, "cost_basis": "estimate (list price)" if est is not None else "unknown",
                "served_by": rec.get("model")}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            (Path(tmp).parent / f"{Path(tmp).name}-card.md").unlink()
        except OSError:
            pass


def summarize(results):
    by = {}
    for r in results:
        s = by.setdefault((r["rung"], r["model"]), {"runs": 0, "passed": 0, "seconds": 0, "cost": 0.0, "priced": 0})
        s["runs"] += 1
        s["passed"] += 1 if r["passed"] else 0
        s["seconds"] += r["seconds"]
        if r["cost_usd"] is not None:
            s["cost"] += r["cost_usd"]
            s["priced"] += 1
    out = []
    for (rung, model), s in sorted(by.items()):
        out.append({"rung": rung, "model": model, "runs": s["runs"], "passed": s["passed"],
                    "pass_rate": round(s["passed"] / s["runs"], 2), "avg_seconds": round(s["seconds"] / s["runs"]),
                    "avg_cost_usd": round(s["cost"] / s["priced"], 4) if s["priced"] else None,
                    "cost_per_pass_usd": round(s["cost"] / s["passed"], 4) if s["passed"] and s["priced"] else None})
    return out


def markdown(results, summary, header):
    lines = [f"# feature-ladder benchmark ({header})", "",
             "| Task | Rung | Model | Run | Result | Turns | Time | Input | Output | Cost |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        cost = "?" if r["cost_usd"] is None else f"${r['cost_usd']:.4f}" + ("" if r["cost_basis"] == "charged" else " est.")
        lines.append(f"| {r['task']} | {r['rung']} | {r['model']} | {r['run']} | "
                     f"{'pass' if r['passed'] else 'FAIL: ' + str(r['detail'])} | {r['turns'] or '?'} | {r['seconds']}s | "
                     f"{r['input_tokens']:,} | {r['output_tokens']:,} | {cost} |")
    lines += ["", "| Rung | Model | Passed | Avg time | Avg cost | Cost per pass |", "|---|---|---|---|---|---|"]
    for s in summary:
        lines.append(f"| {s['rung']} | {s['model']} | {s['passed']}/{s['runs']} | {s['avg_seconds']}s | "
                     f"{'?' if s['avg_cost_usd'] is None else '$%.4f' % s['avg_cost_usd']} | "
                     f"{'-' if s['cost_per_pass_usd'] is None else '$%.4f' % s['cost_per_pass_usd']} |")
    return "\n".join(lines) + "\n"


def parse(argv):
    a, i = {"--models": []}, 0
    flags = {"--compare"}
    while i < len(argv):
        k = argv[i]
        if k in flags:
            a[k] = True
            i += 1
            continue
        if i + 1 >= len(argv):
            raise ValueError(f"{k} needs a value")
        if k == "--models":
            a[k].append(argv[i + 1])
        elif k in ("--level", "--tasks", "--repeat", "--parallel", "--timeout", "--out", "--md", "--max-turns"):
            a[k] = argv[i + 1]
        else:
            raise ValueError(f"unknown option {k}")
        i += 2
    return a


def plan(a, cfg):
    """[(task id, model)] for this invocation."""
    tasks = [t for t in (a.get("--tasks") or ",".join(TASKS)).split(",") if t]
    unknown = [t for t in tasks if t not in TASKS]
    if unknown:
        raise ValueError(f"unknown task {', '.join(unknown)} (see `ladder_bench.py tasks`)")
    named = {}
    for spec in a["--models"]:
        rung, _, ids = spec.partition("=")
        named[rung] = [m for m in ids.split(",") if m]
    level = mc.models_for(a["--level"]) if a.get("--level") else cfg["openrouter"]["models"]
    jobs = []
    for t in tasks:
        rung = TASKS[t]["rung"]
        models = named.get(rung) or (CANDIDATES[rung] if a.get("--compare") else [level[rung]])
        jobs += [(t, m) for m in models]
    return jobs


def main(argv):
    ladder_state.utf8_stdio()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "tasks":
        for k, t in TASKS.items():
            print(f"{k:16} {t['rung']:20} {t['card'].splitlines()[0].split(': ', 1)[1][:90]}")
        return 0
    if argv[0] != "run":
        print(__doc__)
        return 1
    try:
        a = parse(argv[1:])
        cfg = ew.load_config(os.getcwd(), no_bs=False)
        jobs = plan(a, cfg)
        repeat, parallel = int(a.get("--repeat") or 1), int(a.get("--parallel") or 4)
        timeout, max_turns = int(a.get("--timeout") or 420), int(a.get("--max-turns") or 30)
    except ValueError as e:
        print(f"ladder_bench: {e}", file=sys.stderr)
        return 1
    key, source = ew.get_key(cfg)
    if not ew.claude_bin() or not key:
        print(f"ladder_bench: unavailable ({'no claude CLI' if not ew.claude_bin() else source})", file=sys.stderr)
        return 3
    rows, _ = mc.catalog()
    before = ew.usage_usd(cfg, key)
    t0 = time.time()
    work = [(t, m, n) for n in range(1, repeat + 1) for t, m in jobs]
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        results = list(pool.map(lambda w: run_one(w[0], w[1], w[2], timeout, max_turns, rows), work))
    time.sleep(20)  # the key's usage counter trails the last request
    after = ew.usage_usd(cfg, key)
    charged = round(after - before, 4) if before is not None and after is not None and after >= before else None
    results.sort(key=lambda r: (r["rung"], r["task"], r["model"], r["run"]))
    summary = summarize(results)
    header = ("compare" if a.get("--compare") or a["--models"] else f"budget level {a.get('--level') or cfg.get('budget')}") \
        + f", {len(results)} runs, {round(time.time() - t0)}s, OpenRouter charged " \
        + ("an unknown amount" if charged is None else f"${charged:.4f}") + " in all"
    md = markdown(results, summary, header)
    print(md)
    report = {"at": ew.now(), "header": header, "charged_usd": charged, "results": results, "summary": summary}
    for flag, text in (("--out", json.dumps(report, indent=2)), ("--md", md)):
        if a.get(flag):
            Path(a[flag]).parent.mkdir(parents=True, exist_ok=True)
            Path(a[flag]).write_text(text + ("\n" if flag == "--out" else ""), encoding="utf-8", newline="\n")
    return 0 if all(r["passed"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
