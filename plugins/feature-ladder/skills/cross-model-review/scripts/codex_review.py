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
  codex_review.py review      [--base REF | --uncommitted] [--tier T] [--flags a,b | --flags auto] [--slug S] [--effort E]
  codex_review.py adversarial [--base REF | --uncommitted] [--tier T] [--flags a,b | --flags auto] [--slug S] [--effort E]

Deterministic reasoning effort (unless --effort or FEATURE_LADDER_CODEX_EFFORT):
  medium for tiers T0-T3; high for T4, T5, ESC, D, or any of the flags
  security, persistence, concurrency, cross_system, public_api, irreversible
  (both modes). The script never inspects the diff: without --tier or --flags
  the effort is medium. `--flags auto` takes the risk flags from the intakes
  confirmed in this coding session (.feature-ladder/intake/*.json newer than
  session.json's start), which is what /feature-ladder:wrap passes.
Model: Codex's own configured default, or FEATURE_LADDER_CODEX_MODEL.

Output: JSON written to .feature-ladder/reviews/<slug>-codex-<mode>.json and a
markdown summary on stdout. Exit 0 on a completed run (findings or not), 3 when
Codex is unavailable (the ladder continues without the second opinion), 1 on
error or timeout. A timeout stops Codex and everything it started, and the
guard check, the record and the temp-file cleanup still happen.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "orchestration-ladder" / "scripts"))
import ladder_state  # noqa: E402

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


# Stop a process and everything it started. Killing only the direct child is
# not enough: an npm `codex.cmd` shim is cmd.exe, and the real Codex below it
# would keep running (and keep the output pipes open).
kill_tree = ladder_state.kill_tree


def run(cmd, cwd, timeout=60, stdin=None):
    """subprocess.run with captured UTF-8 output, except that a timeout stops
    the whole process tree before TimeoutExpired is raised."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", **group)
    try:
        out, err = proc.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            out, err = "", ""
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
    low = out.lower()
    if r.returncode != 0 or "logged in" not in low or re.search(r"\bnot\s+logged\s+in\b", low):
        return False, f"Codex not signed in ({out or 'no output'}); run: codex login"
    return True, out


def effort_for(mode, tier, flags):
    env = os.environ.get("FEATURE_LADDER_CODEX_EFFORT")
    if env:
        return env
    if (tier or "").upper().startswith(HIGH_TIERS) or (set(flags) & RISK_FLAGS):
        return "high"
    return "medium"


def session_flags(state_root):
    """The risk flags of every intake confirmed in this coding session, sorted.
    Lets the one review at /feature-ladder:wrap pick its effort without a model
    reading the diff. No session start or no intake: no flags (medium effort)."""
    state = ladder_state.state_dir(state_root)
    started = str((ladder_state.read_json(state / "session.json") or {}).get("started") or "")
    if not started:
        return []
    found = set()
    for f in sorted((state / "intake").glob("*.json")):
        if f.name.endswith(("-scan.json", "-proposal.json", "-answers.json")):
            continue
        intake = ladder_state.read_json(f) or {}
        fields = intake.get("fields") if isinstance(intake, dict) else None
        if not isinstance(fields, dict) or str(intake.get("confirmed_at") or "") < started:
            continue
        found.update(k for k in RISK_FLAGS if fields.get(k) is True)
    return sorted(found)


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
    """{path: content fingerprint} for every dirty or untracked path, so edits
    to files that were already dirty before Codex ran are detected too.
    `-z` with core.quotepath=off gives real file names (spaces, non-ASCII)."""
    r = run(["git", "-c", "core.quotepath=off", "status", "--porcelain", "-z", "--untracked-files=all"], root)
    out = {}
    entries = iter(r.stdout.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        status, p = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            next(entries, None)  # the entry after a rename/copy is its old name
        fp = Path(root) / p
        try:
            digest = hashlib.sha1(fp.read_bytes()).hexdigest()[:12] if fp.is_file() else "dir-or-missing"
        except OSError:
            digest = "unreadable"
        out[p] = digest
    return out


def changed_paths(before, after):
    """Paths whose state differs between two porcelain() snapshots, in BOTH
    directions: a path that appeared or changed, and one that vanished from the
    snapshot (a reverted edit, a deleted untracked file)."""
    return sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))


def allowed_path(p, outdir_rel):
    return (p.startswith(outdir_rel) or p.startswith(".feature-ladder/")
            or any(m in p or p.rstrip("/") + "/" == m for m in CACHE_MARKERS)
            or p.endswith((".pyc", ".pyo")))


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
        if name not in argv:
            return default
        i = argv.index(name) + 1
        if i >= len(argv) or argv[i].startswith("--"):
            raise ValueError(f"{name} needs a value")
        return argv[i]

    try:
        slug = opt("--slug", time.strftime("review-%Y%m%d-%H%M%S"))
        tier = opt("--tier", "")
        flags_arg = opt("--flags", "") or ""
        effort_arg = opt("--effort")
        base_arg = opt("--base")
    except ValueError as e:
        print(f"codex_review.py {mode}: {e}", file=sys.stderr)
        return 1

    ok, msg = check()
    if not ok:
        print(f"Codex second opinion skipped: {msg}")
        return 3
    exe = codex_bin()
    root = repo_root(os.getcwd())
    state_root = ladder_state.state_root(root)  # the main checkout: one .feature-ladder/ for every worktree
    flags = session_flags(state_root) if flags_arg.strip().lower() == "auto" else [f for f in flags_arg.split(",") if f]
    effort = effort_arg or effort_for(mode, tier, flags)

    if "--uncommitted" in argv:
        scope_args, scope_txt = ["--uncommitted"], "in the uncommitted working tree (staged, unstaged and untracked files)"
        diffcmd = "git diff HEAD; git status --porcelain"
    else:
        base = base_arg or default_base(root)
        if base:
            scope_args, scope_txt = ["--base", base], f"on this branch compared with {base} (committed and uncommitted)"
            diffcmd = f"git diff {base}...HEAD; git diff HEAD; git status --porcelain"
        else:
            scope_args, scope_txt = ["--uncommitted"], "in the uncommitted working tree"
            diffcmd = "git diff HEAD; git status --porcelain"

    outdir_rel = f".feature-ladder/adversarial/{slug}"
    reviews = ladder_state.state_dir(state_root) / "reviews"
    reviews.mkdir(parents=True, exist_ok=True)
    for r_ in {str(state_root): state_root, str(root): root}.values():  # adversarial tests land in this worktree
        ladder_state.ensure_gitignore(r_)

    def shown(path):
        try:
            return path.relative_to(state_root)
        except ValueError:
            return path

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
    timed_out = False
    try:
        r = run(cmd, root, timeout=TIMEOUT, stdin=stdin)
    except subprocess.TimeoutExpired as e:
        timed_out = True
        r = subprocess.CompletedProcess(cmd, None, e.output or "", e.stderr or "")
    secs = round(time.time() - started)
    raw = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
    try:
        result = json.loads(raw)
    except ValueError:
        result = {"unparsed_output": raw[-4000:], "stderr": (r.stderr or "")[-2000:]}

    # The guard runs whatever happened to Codex: a run that timed out may have written files first.
    stray = [p for p in changed_paths(before, porcelain(root)) if not allowed_path(p, outdir_rel)]
    record = {"mode": mode, "effort": effort, "flags": flags, "scope": scope_args, "slug": slug,
              "codex_exit": r.returncode, "timed_out": timed_out, "seconds": secs, "result": result,
              "files_changed_outside_allowed_dir": stray}
    dest = reviews / f"{slug}-codex-{mode}.json"
    ladder_state.write_json(dest, record)
    shutil.rmtree(tmp, ignore_errors=True)

    if timed_out:
        print(f"Codex {mode} timed out after {TIMEOUT}s; second opinion incomplete. Record: {shown(dest)}")
        if stray:
            print(f"**GUARD VIOLATION:** files outside {outdir_rel} were changed, deleted or reverted "
                  f"before the timeout: {', '.join(stray)}")
        return 1
    print(f"## Codex {mode} ({effort} effort, {secs}s) - saved to {shown(dest)}")
    if stray:
        print(f"**GUARD VIOLATION:** files outside {outdir_rel} were changed, deleted or reverted: {', '.join(stray)}")
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
            card.write_text(verify_card(slug, act, str(shown(dest))), encoding="utf-8")
            print(f"Verification card for ONE mechanic dispatch: {shown(card)}")
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
    ladder_state.utf8_stdio()
    sys.exit(main(sys.argv[1:]))
