#!/usr/bin/env python3
"""
PreToolUse(Bash) guard for feature-ladder runs.

While a ladder run is active in this repository (.feature-ladder/run.json has
"active": true), commands that cannot be undone are denied. The agent is told
to prepare the step (script, migration file, command in the PR body) and park
it in a checkpoint PR instead of running it. Outside a run, it does nothing.

It is a safety net, not the only control: the coordinator and workers are
instructed never to run irreversible steps. Patterns are deliberately narrow so
normal development (tests, local builds, git commit) is never blocked.
"""
import json
import re
import sys
from pathlib import Path

PATTERNS = [
    (r"\bgit\s+push\b[^\n;&|]*(\s--force\b|\s-f\b|\s--force-with-lease\b|\s\+\S)", "force push"),
    (r"\bgit\s+push\b[^\n;&|]*\s(origin\s+)?(main|master|production|prod|release)\b(?!-)", "push to a protected branch"),
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+clean\s+-[a-z]*f", "git clean -f"),
    (r"\bgit\s+branch\s+-D\b", "force-delete a branch"),
    (r"\bgh\s+pr\s+merge\b", "merging a PR"),
    (r"\bgh\s+release\s+(create|delete)\b", "publishing a release"),
    (r"\bgh\s+repo\s+delete\b", "deleting a repository"),
    (r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+(/|~|\$HOME|\.\.|[A-Za-z]:\\)(\s|$)", "recursive delete outside the repo"),
    (r"\b(drop\s+(table|database|schema)|truncate\s+table)\b", "dropping or truncating data"),
    (r"\bdelete\s+from\s+\w+\s*(;|$|\")", "DELETE without WHERE"),
    (r"\bterraform\s+(apply|destroy)\b", "terraform apply/destroy"),
    (r"\bpulumi\s+(up|destroy)\b", "pulumi up/destroy"),
    (r"\bkubectl\s+(delete|apply|replace|scale|rollout\s+restart)\b", "changing a Kubernetes cluster"),
    (r"\bhelm\s+(install|upgrade|uninstall|delete)\b", "changing a Helm release"),
    (r"\b(prisma\s+migrate\s+deploy|rails\s+db:migrate\s+RAILS_ENV=production)\b", "migrating a deployed database"),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b", "publishing a package"),
    (r"\bdocker\s+push\b", "pushing an image"),
    (r"\b(vercel|netlify|fly|railway|wrangler)\s+(deploy|--prod|up)\b", "deploying"),
    (r"\baws\s+\S+\s+(delete|terminate|remove|rm|rb)\S*", "deleting cloud resources"),
    (r"\b(az|gcloud)\b[^\n]*\s(delete|destroy)\b", "deleting cloud resources"),
    (r"\bssh\s+\S+", "running commands on another machine"),
]


def run_active(cwd):
    p = Path(cwd).resolve()
    for d in [p, *p.parents]:
        f = d / ".feature-ladder" / "run.json"
        if f.is_file():
            try:
                return bool(json.loads(f.read_text(encoding="utf-8")).get("active"))
            except (OSError, ValueError):
                return False
        if (d / ".git").exists():
            return False
    return False


def main():
    try:
        payload = json.loads(sys.stdin.read().lstrip("﻿"))
    except ValueError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    cmd = str((payload.get("tool_input") or {}).get("command", ""))
    if not cmd or not run_active(payload.get("cwd") or "."):
        return 0
    for pat, what in PATTERNS:
        if re.search(pat, cmd, re.IGNORECASE):
            reason = (f"feature-ladder: blocked ({what}) while a ladder run is active. Irreversible "
                      "steps are never run by the ladder. Prepare it instead (script, migration file, or the exact "
                      "command), then park it with pr_checkpoint.py checkpoint --kind irreversible and keep working "
                      "on the next branch. The user runs it after approving the PR.")
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                     "permissionDecision": "deny",
                                                     "permissionDecisionReason": reason}}))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
