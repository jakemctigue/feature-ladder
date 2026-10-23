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
    """run.json lives in the main checkout; linked worktrees must find it there too."""
    p = Path(cwd).resolve()
    for d in [p, *p.parents]:
        f = d / ".feature-ladder" / "run.json"
        if f.is_file():
            return d, f
        if (d / ".git").exists():
            try:
                r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                   cwd=str(d), capture_output=True, text=True, timeout=10)
                common = Path(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else None
            except (OSError, subprocess.SubprocessError):
                common = None
            if common and common.name == ".git":
                f = common.parent / ".feature-ladder" / "run.json"
                if f.is_file():
                    return common.parent, f
            return None, None
    return None, None


def main():
    try:
        payload = json.loads(sys.stdin.read().lstrip("\ufeff"))
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = str((payload.get("tool_input") or {}).get("command", ""))
    root, f = find_run(payload.get("cwd") or os.getcwd())
    if not cmd or not f:
        return 0
    try:
        run = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not run.get("active"):
        return 0
    from irreversible import gate
    allowed, reason, changed = gate(cmd, run)
    if changed:
        try:
            f.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass
    if not allowed:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
