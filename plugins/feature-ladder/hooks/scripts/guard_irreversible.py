#!/usr/bin/env python3
"""
PreToolUse(Bash) audit hook for feature-ladder runs. Costs no model tokens.

Only active while .feature-ladder/run.json in this repo says "active": true.
Each command is classified by skills/orchestration-ladder/scripts/irreversible.py
and every command with effects outside the repo (deploys, merges, data steps)
is recorded in run.json, with "review: pending" when it touched production or
was on the former never-list, so FEATURE-REPORT.md and the PR can list it.
It is an audit log, not a gate: the only command it denies is one whose
output would put a secret VALUE into the transcript (use with_secrets.py).
Ordinary development commands pass untouched.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "skills" / "orchestration-ladder" / "scripts"))


def find_run(cwd):
    """(root, run.json) of the run this folder belongs to, or (None, None).

    run.json lives in the main checkout; linked worktrees must find it there
    too. This runs before every Bash call, so the common case costs no
    subprocess: only a linked worktree or a submodule (where .git is a file)
    asks git where the main checkout is."""
    p = Path(cwd).resolve()
    for d in [p, *p.parents]:
        f = d / ".feature-ladder" / "run.json"
        if f.is_file():
            return d, f
        dot_git = d / ".git"
        if dot_git.is_dir():
            return None, None  # the main checkout itself, and it has no run
        if dot_git.exists():
            try:
                r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                   cwd=str(d), capture_output=True, text=True, timeout=10)
                common = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
            except (OSError, subprocess.SubprocessError):
                common = None
            if common and common.name == ".git":
                main = common.parent.resolve()
                f = main / ".feature-ladder" / "run.json"
                if f.is_file():
                    return main, f
            return None, None
    return None, None


def main():
    try:
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
    except ValueError:
        return 0
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return 0
    cmd = str((payload.get("tool_input") or {}).get("command", ""))
    root, f = find_run(payload.get("cwd") or os.getcwd())
    if not cmd or not f:
        return 0
    import ladder_state
    run = ladder_state.read_json(f)
    if not isinstance(run, dict) or not run.get("active"):
        return 0  # no run, a finished run, or an unreadable file: nothing to audit
    from irreversible import gate
    # Classify and append under the lock, on a fresh read: two commands logged at
    # the same moment (parallel workers) must both end up in the file.
    with ladder_state.locked(root):
        run = ladder_state.read_json(f)
        if not isinstance(run, dict) or not run.get("active"):
            return 0
        allowed, reason, changed = gate(cmd, run)
        if changed:
            try:
                ladder_state.write_json(f, run)
            except OSError:
                pass
    if not allowed:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - an audit log must never break a Bash call
        sys.exit(0)
