#!/usr/bin/env python3
"""
Acceptance checks for feature-ladder with no model tokens.

Runs a Task Card's "Done when" commands exactly as written, records each
command's exit code and output tail, compares what actually changed in the
repo with the card's Scope and the worker's "Changed:" claim, and writes the
result to .feature-ladder/evidence/<task-id>.json. It replaces the mechanic
subagent that used to re-run checks for acceptance; a mechanic is only needed
when a check's output has to be interpreted.

Usage
  verify.py run --id T3 --check "pytest tests/x -q" --check "ruff check ." \
                [--scope src/x.py --scope tests/] [--changed src/x.py --changed tests/test_x.py] \
                [--base REF] [--cwd DIR] [--timeout SECONDS]
  verify.py run --card CARD.json [--cwd DIR]
        CARD.json: {"id": "T3", "checks": [...], "scope": [...], "changed": [...], "base": "..."}
  verify.py show --id T3 [--cwd DIR]        print the saved evidence

Exit codes
  0  every check passed and the changed files match the Scope and the claim
  1  a check failed, timed out, or was refused (see below)
  2  checks passed but a file outside Scope changed, or the "Changed:" claim is wrong
  3  bad arguments

Scope: each entry is a file, a directory (trailing slash optional) or a glob.
Paths under .feature-ladder/ are ignored. Without --scope the scope check is
skipped; without --changed the claim check is skipped; outside a git repo both
are skipped and the evidence says so.

Refused checks: every command is classified by irreversible.py first. The one
refusal is a command whose output would print a secret value; it is not run
and counts as failed (run it through with_secrets.py, as on any Task Card).
Everything else runs as written; a check with effects outside the repo (a
deploy, a merge, a data step) is recorded in run.json like any other
outside-repo command, with review: pending where it touched production.
"""
import fnmatch
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

TAIL = 10
DEFAULT_TIMEOUT = int(os.environ.get("FEATURE_LADDER_CHECK_TIMEOUT", "570"))
IGNORED_PREFIXES = (".feature-ladder/",)
CACHE_MARKERS = ("__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "node_modules/.cache/",
                 ".hypothesis/", ".tox/")  # build noise a check leaves behind is not a change


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sh(args, cwd, timeout=60):
    try:
        r = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def repo_root(cwd):
    code, out = sh(["git", "rev-parse", "--show-toplevel"], cwd)
    return Path(out.strip()) if code == 0 and out.strip() else None


def main_checkout(root):
    """The main checkout for a linked worktree (where run.json and the evidence live); else root."""
    code, out = sh(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    common = Path(out.strip()) if code == 0 and out.strip() else None
    return common.parent if common and common.name == ".git" else Path(root)


def load_run(root):
    p = Path(root) / ".feature-ladder" / "run.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"active": True, "base": "main", "outside_policy": "all", "environments": {}}


def refusal(cmd, run):
    """None if the check may run; otherwise why not. Only a secret exposure is
    refused; a check with effects outside the repo runs and is logged."""
    try:
        from irreversible import classify
    except ImportError:
        return None
    c = classify(cmd, run)
    if c.get("decision") == "deny":
        return f"refused: {c.get('tier')} ({c.get('label') or c.get('reason') or 'not a check'})"
    if c.get("tier", "none") != "none":
        _log_outside(cwd_of(run), cmd, c)
    return None


def cwd_of(run):
    return run.get("_root")


def _log_outside(root, cmd, c):
    """Record a check with effects outside the repo in run.json (audit, not a gate)."""
    if not root:
        return
    p = Path(root) / ".feature-ladder" / "run.json"
    if not p.is_file():
        return
    try:
        from irreversible import record
        live = json.loads(p.read_text(encoding="utf-8"))
        record(live, cmd, c, "ran as a check")
        p.write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, ImportError):
        pass


def run_check(cmd, cwd, timeout, run):
    why = refusal(cmd, run)
    if why:
        return {"cmd": cmd, "exit": None, "status": "refused", "reason": why, "tail": [], "seconds": 0.0}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        tail = [l for l in out.splitlines() if l.strip()][-TAIL:]
        return {"cmd": cmd, "exit": r.returncode, "status": "passed" if r.returncode == 0 else "failed",
                "tail": tail, "seconds": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return {"cmd": cmd, "exit": None, "status": "timeout", "reason": f"no exit after {timeout}s",
                "tail": [l for l in out.splitlines() if l.strip()][-TAIL:], "seconds": round(time.time() - t0, 1)}
    except OSError as e:
        return {"cmd": cmd, "exit": None, "status": "failed", "reason": str(e), "tail": [], "seconds": 0.0}


def changed_files(root, base):
    """Files that differ from `base` (if given) plus anything uncommitted or untracked."""
    files = set()
    if base:
        code, out = sh(["git", "diff", "--name-only", f"{base}...HEAD"], root)
        if code != 0:
            code, out = sh(["git", "diff", "--name-only", base, "HEAD"], root)
        if code == 0:
            files.update(l.strip() for l in out.splitlines() if l.strip())
    code, out = sh(["git", "status", "--porcelain", "--untracked-files=all"], root)
    if code == 0:
        for line in out.splitlines():
            if len(line) > 3:
                path = line[3:].strip()
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                files.add(path.strip('"'))
    out = []
    for f in files:
        f = f.replace("\\", "/")
        if any(f.startswith(p) for p in IGNORED_PREFIXES) or any(m in f for m in CACHE_MARKERS):
            continue
        out.append(f)
    return sorted(out)


def in_scope(path, scope):
    for s in scope:
        s = str(s).replace("\\", "/").rstrip("/")
        if not s:
            continue
        if path == s or path.startswith(s + "/") or fnmatch.fnmatchcase(path, s) \
                or fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], s):
            return True
    return False


def evidence_path(root, task_id):
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in str(task_id)) or "task"
    return Path(root) / ".feature-ladder" / "evidence" / f"{safe}.json"


def save(root, task_id, data):
    p = evidence_path(root, task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    gi = p.parent.parent / ".gitignore"
    try:
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
        if "evidence/" not in lines:
            gi.write_text("\n".join(lines + ["evidence/"]) + "\n", encoding="utf-8")
    except OSError:
        pass
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return p


def verify(card, cwd, timeout):
    cwd = Path(cwd).resolve()
    root = repo_root(cwd)
    home = main_checkout(root) if root else cwd  # evidence and run.json live in the main checkout
    run = load_run(home)
    run["_root"] = str(home)  # where an outside-repo check is logged (audit, not a gate)
    checks = [run_check(c, cwd, timeout, run) for c in card.get("checks") or []]
    result = {"task": card.get("id") or "task", "at": now(), "cwd": str(cwd), "checks": checks,
              "git": bool(root), "scope": card.get("scope") or [], "claimed": card.get("changed") or []}
    if root:
        changed = changed_files(root, card.get("base"))
        result["changed"] = changed
        scope = card.get("scope") or []
        result["outside_scope"] = [f for f in changed if not in_scope(f, scope)] if scope else []
        claimed = sorted(str(c).replace("\\", "/") for c in card.get("changed") or [])
        if claimed:
            result["claimed_not_changed"] = [c for c in claimed if c not in changed and not any(f.startswith(c.rstrip("/") + "/") for f in changed)]
            result["changed_not_claimed"] = [f for f in changed if f not in claimed and not any(f.startswith(c.rstrip("/") + "/") for c in claimed)]
        else:
            result["claimed_not_changed"] = result["changed_not_claimed"] = []
    else:
        result.update(changed=None, outside_scope=[], claimed_not_changed=[], changed_not_claimed=[],
                      note="not a git repository: scope and claim checks skipped")
    passed = sum(1 for c in checks if c["status"] == "passed")
    failed = len(checks) - passed
    result["checks_passed"] = passed
    result["checks_total"] = len(checks)
    if failed:
        result["verdict"], code = "failed", 1
    elif result["outside_scope"] or result["claimed_not_changed"] or result["changed_not_claimed"]:
        result["verdict"], code = "scope-or-claim-mismatch", 2
    else:
        result["verdict"], code = "passed", 0
    result["evidence"] = str(save(home, result["task"], result))
    return result, code


def summary(r):
    lines = [f"TASK {r['task']} - {r['verdict']} - checks {r['checks_passed']}/{r['checks_total']} passed"
             + ("" if r.get("git") else " - not a git repo")]
    for i, c in enumerate(r["checks"]):
        ex = c["exit"] if c["exit"] is not None else c["status"]
        lines.append(f"  [{i}] {c['cmd']} -> {ex} ({c['seconds']}s)" + (f" {c.get('reason')}" if c.get("reason") else ""))
        if c["status"] != "passed":
            lines += [f"      | {t}" for t in c["tail"][-5:]]
    if r.get("outside_scope"):
        lines.append("  outside scope: " + ", ".join(r["outside_scope"]))
    if r.get("claimed_not_changed"):
        lines.append("  claimed but unchanged: " + ", ".join(r["claimed_not_changed"]))
    if r.get("changed_not_claimed"):
        lines.append("  changed but not claimed: " + ", ".join(r["changed_not_claimed"]))
    if r.get("note"):
        lines.append("  " + r["note"])
    lines.append(f"  evidence: {r['evidence']}")
    return "\n".join(lines)


def parse(argv):
    a = {"--check": [], "--scope": [], "--changed": []}
    it = iter(argv)
    for x in it:
        if x in ("--check", "--scope", "--changed"):
            v = next(it, None)
            if v is None:
                raise ValueError(f"{x} needs a value")
            a[x].append(v)
        elif x in ("--id", "--card", "--base", "--cwd", "--timeout"):
            v = next(it, None)
            if v is None:
                raise ValueError(f"{x} needs a value")
            a[x] = v
        else:
            raise ValueError(f"unknown argument {x}")
    return a


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        a = parse(rest)
    except ValueError as e:
        print(f"verify.py: {e}", file=sys.stderr)
        return 3
    cwd = a.get("--cwd") or os.getcwd()
    if cmd == "show":
        root = repo_root(cwd)
        p = evidence_path(main_checkout(root) if root else Path(cwd), a.get("--id") or "task")
        if not p.is_file():
            print(f"verify.py: no evidence at {p}", file=sys.stderr)
            return 3
        sys.stdout.write(p.read_text(encoding="utf-8"))
        return 0
    if cmd != "run":
        print(f"verify.py: unknown command {cmd}", file=sys.stderr)
        return 3
    if a.get("--card"):
        try:
            card = json.loads(Path(a["--card"]).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as e:
            print(f"verify.py: cannot read card: {e}", file=sys.stderr)
            return 3
    else:
        card = {}
    if a.get("--id"):
        card["id"] = a["--id"]
    for k, key in (("--check", "checks"), ("--scope", "scope"), ("--changed", "changed")):
        if a[k]:
            card[key] = a[k]
    if a.get("--base"):
        card["base"] = a["--base"]
    if not card.get("checks"):
        print("verify.py: no checks given (--check CMD or a card with \"checks\")", file=sys.stderr)
        return 3
    try:
        timeout = int(a.get("--timeout") or DEFAULT_TIMEOUT)
    except ValueError:
        print("verify.py: --timeout must be an integer", file=sys.stderr)
        return 3
    result, code = verify(card, cwd, timeout)
    print(summary(result))
    return code


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
