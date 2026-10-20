#!/usr/bin/env python3
"""
Cross-model review for feature-ladder: a second opinion from OpenAI Codex on
code written by Claude, using the official Codex CLI and the user's own Codex
sign-in (ChatGPT plan or API key). Nothing here touches credentials; the Codex
CLI handles authentication itself.

Modes
  review       read-only second-opinion review of the diff (codex exec, read-only sandbox)
  adversarial  Codex writes tests that try to break the change, ONLY under
               .feature-ladder/adversarial/<slug>/, runs them, reports results
               (codex exec, workspace-write sandbox)

Usage
  codex_review.py check
  codex_review.py review      [--base REF | --uncommitted] [--tier T] [--flags a,b] [--slug S] [--effort E]
  codex_review.py adversarial [--base REF | --uncommitted] [--tier T] [--flags a,b] [--slug S] [--effort E]

Deterministic reasoning effort (unless --effort or FEATURE_LADDER_CODEX_EFFORT):
  medium for tiers T0-T3; high for T4, T5, ESC, D, or any of the flags
  security, persistence, concurrency, cross_system, public_api (both modes)
Model: Codex's own configured default, or FEATURE_LADDER_CODEX_MODEL.

Output: JSON written to .feature-ladder/reviews/<slug>-codex-<mode>.json and a
markdown summary on stdout. Exit 0 on a completed run (findings or not), 3 when
Codex is unavailable (the ladder continues without the second opinion), 1 on error.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RISK_FLAGS = {"security", "persistence", "concurrency", "cross_system", "public_api", "irreversible"}
HIGH_TIERS = ("T4", "T5", "ESC", "D")  # matched by prefix: route.py emits "T4 ...", "ESCALATE ..."
TIMEOUT = int(os.environ.get("FEATURE_LADDER_CODEX_TIMEOUT", "570"))  # Claude Code caps a Bash call at 600 s
CACHE_MARKERS = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "node_modules/.cache/", ".hypothesis/")

FINDING = {
    "type": "object", "additionalProperties": False,
    "required": ["severity", "file", "line", "title", "detail", "suggested_fix", "confidence", "repro"],
    "properties": {
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "file": {"type": "string"}, "line": {"type": "integer"},
        "title": {"type": "string"}, "detail": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "repro": {"type": "string"},
    },
}
REVIEW_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["verdict", "summary", "findings"],
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "changes_requested"]},
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": FINDING},
    },
}
ADV_TEST = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "file", "attacks", "status", "evidence", "real_bug", "severity"],
    "properties": {
        "name": {"type": "string"}, "file": {"type": "string"},
        "attacks": {"type": "string"},
        "status": {"type": "string", "enum": ["pass", "fail", "error"]},
        "evidence": {"type": "string"},
        "real_bug": {"type": "boolean"},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "none"]},
    },
}
ADV_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "test_command", "tests"],
    "properties": {
        "summary": {"type": "string"},
        "test_command": {"type": "string"},
        "tests": {"type": "array", "items": ADV_TEST},
    },
}

REVIEW_PROMPT = """You are giving an independent second opinion on code written by another AI model.
The changes to review are {scope}. Inspect them with git (for example `{diffcmd}`)
and read the surrounding code you need. Review only those changes. Report real problems, not style preferences:
correctness bugs, unhandled edge cases, error handling gaps, security issues,
data loss or migration risks, concurrency problems, broken contracts with callers,
and tests that pass without actually proving the behaviour.
For each finding give the exact file and line, why it is wrong, a concrete fix, and
`repro`: one shell command run from the repository root that demonstrates the problem
(a test invocation, an existing script, or a one-line python/node snippet), or an
empty string when no practical command exists.
Use severity critical/high only for issues that would cause wrong results, data
loss, security exposure, or crashes in normal use. Do not modify any files.
If the change is sound, return verdict "approve" with an empty findings list."""

ADV_PROMPT = """You are an adversarial tester. Another AI model wrote the changes {scope}.
Your job is to break them. Steps:
1. Read the diff and the code it touches. Identify the repository's test framework and test command.
2. Write tests that attack the new behaviour: boundary values, empty/null/huge inputs,
   malformed or hostile input, unicode, ordering and concurrency, error paths, resource
   limits, and any contract the change claims to keep.
3. Write test files ONLY inside {outdir} (create it). Do not modify, create or delete
   any other file in the repository. Import the code under test; do not copy it.
4. Run only your tests with the repository's own test runner and record the results.
For every test report whether a failure shows a real bug in the change (real_bug) or a
problem with the test itself. Do not fix the code under test."""


# ------------------------------------------------------------- triage ------
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _key(f):
    """Two findings are the same bug when they name the same file and the same
    problem (title, normalised) or the same file and line."""
    title = " ".join(str(f.get("title", "")).lower().split())
    return str(f.get("file", "")), title


def triage(findings):
    """Deterministic trim before anything reaches the coordinator: drop `low`
    (listed in the record, never acted on), collapse duplicates, sort by
    severity then file and line. Returns {"actionable", "low", "duplicates"}."""
    seen_keys, seen_lines, actionable, low, dup = set(), set(), [], [], 0
    ordered = sorted(findings, key=lambda x: (SEVERITY_ORDER.get(x.get("severity"), 9),
                                              str(x.get("file", "")), int(x.get("line") or 0)))
    for f in ordered:
        k = _key(f)
        at = (str(f.get("file", "")), int(f.get("line") or 0))
        if k in seen_keys or (at[1] and at in seen_lines):
            dup += 1
            continue
        seen_keys.add(k)
        if at[1]:
            seen_lines.add(at)
        (low if f.get("severity") == "low" else actionable).append(f)
    return {"actionable": actionable, "low": low, "duplicates": dup}


def verify_card(slug, actionable, record_rel):
    """One Task Card that lets a single mechanic verify every finding in one
    dispatch, instead of one subagent per finding."""
    lines = [f"TASK codex-verify-{slug}: confirm or refute {len(actionable)} Codex finding(s)",
             "Rung: mechanic (haiku/low)   Router score: n/a   Flags: none",
             "Scope: no file edits; run commands only",
             "Design: N/A - mechanical",
             "Done when: every finding below has a verdict line",
             "Do not: fix anything; do not edit, create or delete files; do not re-run with different flags",
             f"Memory: none (Codex record: {record_rel})",
             "Secrets: none",
             "Return: Evidence Packet whose Claims section has, per finding, exactly one line:",
             "  <n>. <confirmed | not reproduced | unclear> - <command run> -> <exit code> - <last line of output>",
             "  (`unclear` means the command ran but its output does not settle the claim; say why in one clause)",
             "", "Findings to check (run each `repro` from the repository root; if it is empty, run the",
             "most direct existing test or command for that file and say which):"]
    for i, f in enumerate(actionable, 1):
        lines.append(f"{i}. [{f.get('severity')}/{f.get('confidence')}] {f.get('file')}:{f.get('line')} {f.get('title')}")
        lines.append(f"   claim: {f.get('detail')}")
        lines.append(f"   repro: {f.get('repro') or '(none given)'}")
    return "\n".join(lines) + "\n"


def run(cmd, cwd, timeout=60, stdin=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                          input=stdin, encoding="utf-8", errors="replace")


def codex_bin():
    return shutil.which("codex") or shutil.which("codex.exe") or shutil.which("codex.cmd")


def check():
    exe = codex_bin()
    if not exe:
        return False, "Codex CLI not installed (npm install -g @openai/codex, then codex login)"
    try:
        r = run([exe, "login", "status"], os.getcwd(), timeout=30)
    except Exception as e:
        return False, f"codex login status failed: {e}"
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "logged in" not in out.lower():
        return False, f"Codex not signed in ({out or 'no output'}); run: codex login"
    return True, out


def effort_for(mode, tier, flags):
    env = os.environ.get("FEATURE_LADDER_CODEX_EFFORT")
    if env:
        return env
    if (tier or "").upper().startswith(HIGH_TIERS) or (set(flags) & RISK_FLAGS):
        return "high"
    return "medium"


def repo_root(cwd):
    r = run(["git", "rev-parse", "--show-toplevel"], cwd)
    if r.returncode != 0:
        raise SystemExit("not inside a git repository")
    return Path(r.stdout.strip())


def default_base(root):
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        r = run(["git", "merge-base", "HEAD", ref], root)
        if r.returncode == 0 and r.stdout.strip():
            head = run(["git", "rev-parse", "HEAD"], root).stdout.strip()
            mb = r.stdout.strip()
            if mb != head:
                return ref
    return None


def porcelain(root):
    """Dirty/untracked paths with a content fingerprint, so edits to files that
    were already dirty before Codex ran are detected too."""
    import hashlib
    r = run(["git", "status", "--porcelain", "--untracked-files=all"], root)
    out = set()
    for l in r.stdout.splitlines():
        if not l.strip():
            continue
        p = l[3:].strip().strip('"').replace("\\", "/")
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        fp = Path(root) / p
        try:
            digest = hashlib.sha1(fp.read_bytes()).hexdigest()[:12] if fp.is_file() else "dir-or-missing"
        except OSError:
            digest = "unreadable"
        out.add(f"{p}@{digest}")
    return out


def codex_cmd(exe, mode, root, effort, schema_file, out_file):
    model = os.environ.get("FEATURE_LADDER_CODEX_MODEL")
    common = ["-c", f"model_reasoning_effort={json.dumps(effort)}",
              "--output-schema", str(schema_file), "-o", str(out_file), "--ephemeral"]
    if model:
        common += ["-m", model]
    sandbox = "read-only" if mode == "review" else "workspace-write"
    return [exe, "exec", "-s", sandbox, "-C", str(root), *common, "-"]


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    mode = argv[0]
    if mode == "check":
        ok, msg = check()
        print(("OK: " if ok else "UNAVAILABLE: ") + msg)
        return 0 if ok else 3
    if mode not in ("review", "adversarial"):
        print(__doc__)
        return 1

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    ok, msg = check()
    if not ok:
        print(f"Codex second opinion skipped: {msg}")
        return 3
    exe = codex_bin()
    root = repo_root(os.getcwd())
    slug = opt("--slug", time.strftime("review-%Y%m%d-%H%M%S"))
    tier = opt("--tier", "")
    flags = [f for f in (opt("--flags", "") or "").split(",") if f]
    effort = opt("--effort") or effort_for(mode, tier, flags)

    if "--uncommitted" in argv:
        scope_args, scope_txt = ["--uncommitted"], "in the uncommitted working tree (staged, unstaged and untracked files)"
        diffcmd = "git diff HEAD; git status --porcelain"
    else:
        base = opt("--base") or default_base(root)
        if base:
            scope_args, scope_txt = ["--base", base], f"on this branch compared with {base} (committed and uncommitted)"
            diffcmd = f"git diff {base}...HEAD; git diff HEAD; git status --porcelain"
        else:
            scope_args, scope_txt = ["--uncommitted"], "in the uncommitted working tree"
            diffcmd = "git diff HEAD; git status --porcelain"

    outdir_rel = f".feature-ladder/adversarial/{slug}"
    reviews = root / ".feature-ladder" / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    gi = root / ".feature-ladder" / ".gitignore"
    want = ["reviews/", "adversarial/"]
    have = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if any(w not in have for w in want):
        gi.write_text("\n".join(have + [w for w in want if w not in have]) + "\n", encoding="utf-8")
    tmp = Path(tempfile.mkdtemp(prefix="fl-codex-"))
    schema_file = tmp / "schema.json"
    out_file = tmp / "last.json"
    schema_file.write_text(json.dumps(REVIEW_SCHEMA if mode == "review" else ADV_SCHEMA), encoding="utf-8")

    before = porcelain(root)
    cmd = codex_cmd(exe, mode, root, effort, schema_file, out_file)
    if mode == "review":
        stdin = REVIEW_PROMPT.format(scope=scope_txt, diffcmd=diffcmd)
    else:
        stdin = ADV_PROMPT.format(scope=scope_txt, outdir=outdir_rel)
    started = time.time()
    try:
        r = run(cmd, root, timeout=TIMEOUT, stdin=stdin)
    except subprocess.TimeoutExpired:
        print(f"Codex {mode} timed out after {TIMEOUT}s; second opinion incomplete.")
        return 1
    secs = round(time.time() - started)
    raw = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
    try:
        result = json.loads(raw)
    except ValueError:
        result = {"unparsed_output": raw[-4000:], "stderr": r.stderr[-2000:]}

    after = porcelain(root)
    stray = sorted(p.rsplit("@", 1)[0] for p in (after - before)
                   if not p.startswith(outdir_rel) and not p.startswith(".feature-ladder/")
                   and not any(m in p or p.rstrip("/") + "/" == m for m in CACHE_MARKERS)
                   and not p.endswith((".pyc", ".pyo")))
    record = {"mode": mode, "effort": effort, "scope": scope_args, "slug": slug,
              "codex_exit": r.returncode, "seconds": secs, "result": result,
              "files_changed_outside_allowed_dir": stray}
    dest = reviews / f"{slug}-codex-{mode}.json"
    dest.write_text(json.dumps(record, indent=2), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"## Codex {mode} ({effort} effort, {secs}s) - saved to {dest.relative_to(root)}")
    if stray:
        print(f"**GUARD VIOLATION:** Codex changed files outside {outdir_rel}: {', '.join(stray)}")
    if r.returncode != 0 and not raw:
        print(f"Codex exited {r.returncode}: {r.stderr.strip()[-800:]}")
        return 1
    if mode == "review" and stray:
        print("Review mode is read-only, so these changes did not come from the review; verify before acting.")
    if mode == "review" and "findings" in result:
        t = triage(result["findings"])
        act, low = t["actionable"], t["low"]
        print(f"Verdict: **{result.get('verdict')}** - {len(act)} finding(s) to verify"
              f" ({len(low)} low listed in the record only, {t['duplicates']} duplicate(s) dropped). {result.get('summary', '')}")
        for x in act:
            print(f"- [{x['severity']}/{x['confidence']}] {x['file']}:{x['line']} {x['title']} - {x['detail']} Fix: {x['suggested_fix']}")
        if act:
            card = reviews / f"{slug}-codex-verify-card.md"
            card.write_text(verify_card(slug, act, str(dest.relative_to(root))), encoding="utf-8")
            print(f"Verification card for ONE mechanic dispatch: {card.relative_to(root)}")
    elif mode == "adversarial" and "tests" in result:
        t = result["tests"]
        bugs = [x for x in t if x["real_bug"]]
        print(f"{len(t)} adversarial test(s) in {outdir_rel}; {len(bugs)} real bug(s). Run: `{result.get('test_command')}`. {result.get('summary', '')}")
        for x in t:
            mark = "BUG" if x["real_bug"] else x["status"]
            print(f"- [{mark}/{x['severity']}] {x['name']} ({x['file']}): {x['attacks']} - {x['evidence'][:300]}")
    else:
        print("Codex output did not match the schema; raw output saved in the JSON record.")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
