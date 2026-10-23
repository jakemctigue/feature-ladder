#!/usr/bin/env python3
"""
feature-ladder branches and PRs: run a feature to completion without
stopping. Critical design decisions are parked in draft PRs while work
continues; the finished stack is merged and labelled "review: pending" so
the work goes up and the user reviews it afterwards. Commands with effects
outside the repo are logged by irreversible.py and the audit hook, not
gated by branches.

  start      --slug S [--workspace include|stash] [--intake FILE]
             Create branch feature-ladder/<S> from the current branch.
             Uncommitted changes are committed first as a separate commit
             marked as the user's (include, default) or stashed (stash).
             Writes .feature-ladder/run.json (environments, budget and
             switches), which switches on the audit hook and the spawn
             rules (session_budget.py).

  checkpoint --title T --body-file F
             For a critical decision: commit everything, push, open a DRAFT PR
             (base = the previous branch in the stack) whose body is F, then
             create the next branch (feature-ladder/<S>-pt<N>) and switch to
             it, so work continues immediately.

  finish     --title T --body-file F [--draft] [--no-merge]
             Commit the rest, push, open the final PR (base = previous branch,
             or the original base if there were no checkpoints), label every
             PR in the stack "review: pending", merge the stack in order
             (checkpoints first; drafts are marked ready), mark the run
             finished and print the stack with each merge result. --no-merge
             leaves the PRs open; --draft implies it. A merge GitHub refuses
             (branch protection, conflicts) is reported, never forced.

  status     Print run.json.

  abort      Mark the run inactive (turns the hook off) without opening PRs.
             Branches and commits are kept. Use after a run that died midway.

Without a git remote or the gh CLI, everything still happens on local
branches: each PR body is saved to .feature-ladder/checkpoints/ and finish
merges the stack into the base branch locally (--no-ff). Exit 0 ok, 1 error,
2 not a git repository.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

RUN_FILE = Path(".feature-ladder") / "run.json"
REVIEW_LABEL = "review: pending"
REVIEW_LABEL_COLOR = "FBCA04"


def sh(args, cwd, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r.returncode, (r.stdout or "").strip()


def root_dir():
    code, out = sh(["git", "rev-parse", "--show-toplevel"], os.getcwd(), check=False)
    return Path(out) if code == 0 and out else None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(root):
    p = root / RUN_FILE
    if not p.is_file():
        raise RuntimeError("no feature-ladder run in this repo (run `start` first)")
    return json.loads(p.read_text(encoding="utf-8"))


def save(root, run):
    p = root / RUN_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    gi = p.parent / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    for e in ("intake/", "reviews/", "adversarial/", "checkpoints/", "cards/", "evidence/", "runs/", "run.json", "session.json", "session-memory*.md",
              "memory-gate.json", "memory-ledger.jsonl"):
        if e not in lines:
            lines.append(e)
    gi.write_text("\n".join(lines) + "\n", encoding="utf-8")
    p.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")


def ident(root):
    """git -c flags so commits work even if user.name/email are not configured."""
    flags = []
    if sh(["git", "config", "user.name"], root, check=False)[0] != 0:
        flags += ["-c", "user.name=feature-ladder"]
    if sh(["git", "config", "user.email"], root, check=False)[0] != 0:
        flags += ["-c", "user.email=feature-ladder@localhost"]
    return flags


def commit_all(root, message, allow_empty=False, user_only=False):
    sh(["git", "add", "-A", "--", "."] + ([":(exclude).feature-ladder"] if user_only else []), root)
    dirty = sh(["git", "diff", "--cached", "--quiet"], root, check=False)[0] != 0
    if not dirty and not allow_empty:
        return False
    args = ["git"] + ident(root) + ["commit", "-q", "-m", message]
    if not dirty:
        args.append("--allow-empty")
    sh(args, root)
    return True


def branch_exists(root, name):
    return sh(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root, check=False)[0] == 0


def free_branch(root, name):
    cand, n = name, 2
    while branch_exists(root, cand):
        cand, n = f"{name}-{n}", n + 1
    return cand


def pr_ready(root):
    if sh(["git", "remote", "get-url", "origin"], root, check=False)[0] != 0:
        return False, "no git remote 'origin'"
    try:
        if sh(["gh", "auth", "status"], root, check=False)[0] != 0:
            return False, "gh CLI not signed in (gh auth login)"
    except FileNotFoundError:
        return False, "gh CLI not installed"
    return True, ""


def label_pr(root, run, url):
    """Put the review: pending label on a PR (creating the label once). Never fatal."""
    if not run.get("pr_enabled") or not url:
        return
    sh(["gh", "label", "create", REVIEW_LABEL, "--color", REVIEW_LABEL_COLOR, "--force",
        "--description", "Merged or deployed by feature-ladder; review afterwards"], root, check=False)
    code, out = sh(["gh", "pr", "edit", url, "--add-label", REVIEW_LABEL], root, check=False)
    if code != 0:
        run.setdefault("warnings", []).append(f"label not added to {url}: {out[-200:]}")


def open_pr(root, run, base, head, title, body_file, draft):
    """Push head and open a PR. Returns URL, or None and saves the body locally."""
    if run.get("pr_enabled"):
        try:
            sh(["git", "push", "-q", "-u", "origin", head], root)
            if base not in (run.get("base"),) and sh(["git", "ls-remote", "--exit-code", "--heads", "origin", base], root, check=False)[0] != 0:
                sh(["git", "push", "-q", "-u", "origin", base], root)
            args = ["gh", "pr", "create", "--base", base, "--head", head,
                    "--title", title, "--body-file", body_file]
            if draft:
                args.append("--draft")
            _, out = sh(args, root)
            m = re.search(r"https://\S+/pull/\d+", out)
            url = m.group(0) if m else out.splitlines()[-1]
            label_pr(root, run, url)
            return url
        except RuntimeError as e:
            run.setdefault("warnings", []).append(f"PR not opened for {head}: {e}")
    d = root / ".feature-ladder" / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    local = d / f"{head.replace('/', '__')}.md"
    local.write_text(f"# {title}\n\nbase: {base}\nhead: {head}\n\n" +
                     Path(body_file).read_text(encoding="utf-8"), encoding="utf-8")
    return None


def merge_entry(root, run, entry):
    """Merge one PR of the stack (GitHub) or one branch (local). Records the result on the entry."""
    head, base = entry["branch"], entry["base"]
    if run.get("pr_enabled") and entry.get("pr"):
        url = entry["pr"]
        sh(["gh", "pr", "ready", url], root, check=False)  # drafts cannot merge
        code, out = sh(["gh", "pr", "merge", url, "--merge"], root, check=False)
        if code == 0:
            entry["merged"] = "merged"
            return True
        code2, out2 = sh(["gh", "pr", "merge", url, "--merge", "--auto"], root, check=False)
        if code2 == 0:
            entry["merged"] = "queued (auto-merge when checks pass)"
            return True
        entry["merged"] = f"not merged: {(out or out2).strip()[-240:]}"
        run.setdefault("warnings", []).append(f"{url}: {entry['merged']}")
        return False
    # Local-only: fast-forward or --no-ff merge into the base branch, then come back.
    _, current = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, check=False)
    try:
        sh(["git", "checkout", "-q", base], root)
        sh(["git"] + ident(root) + ["merge", "-q", "--no-ff", "-m", f"feature-ladder: merge {head} ({REVIEW_LABEL})", head], root)
        entry["merged"] = "merged locally"
        return True
    except RuntimeError as e:
        sh(["git", "merge", "--abort"], root, check=False)
        entry["merged"] = f"not merged: {str(e)[-240:]}"
        run.setdefault("warnings", []).append(f"{head}: {entry['merged']}")
        return False
    finally:
        if current:
            sh(["git", "checkout", "-q", current], root, check=False)


def merge_stack(root, run):
    """Merge every PR/branch of the stack in order; stop at the first one GitHub refuses."""
    results = []
    for entry in run.get("checkpoints", []):
        if entry.get("merged"):
            continue
        ok = merge_entry(root, run, entry)
        results.append({"branch": entry["branch"], "result": entry["merged"]})
        if not ok:
            break
    return results


def pending_note(run):
    rows = [r for r in run.get("outside_actions", []) if r.get("review") == "pending"]
    if not rows:
        return ""
    lines = ["", f"**{REVIEW_LABEL}** - these ran during the feature; review them afterwards:"]
    for r in rows:
        lines.append(f"- [ ] {r.get('label')} ({r.get('environment') or 'repo'}): `{r['cmd'][:160]}`")
    return "\n".join(lines) + "\n"


def stack_note(run):
    lines = ["", "---", "**feature-ladder PR stack** (merged in this order):"]
    for i, c in enumerate(run.get("checkpoints", []), 1):
        lines.append(f"{i}. `{c['branch']}` ({c['kind']}): {c['title']}" + (f" {c['pr']}" if c.get("pr") else ""))
    return "\n".join(lines) + "\n" + pending_note(run)


def with_stack(body_file, run, root):
    text = Path(body_file).read_text(encoding="utf-8") + stack_note(run)
    out = root / ".feature-ladder" / "checkpoints" / "_body.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return str(out)


# ------------------------------------------------------------------ commands --
def cmd_start(root, a):
    slug = a.get("--slug") or "feature"
    code, base = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, check=False)
    if code != 0 or not base:  # no commits yet: an EMPTY root commit so branches have a base
        sh(["git"] + ident(root) + ["commit", "-q", "--allow-empty", "-m", "feature-ladder: initial commit"], root)
        _, base = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    if base == "HEAD":  # detached: anchor the stack on the commit itself
        base = sh(["git", "rev-parse", "HEAD"], root)[1]
    workspace = a.get("--workspace") or "include"
    _, st = sh(["git", "status", "--porcelain"], root)
    dirty = [l for l in st.splitlines() if l.strip() and ".feature-ladder/" not in l]
    branch = free_branch(root, f"feature-ladder/{slug}")
    stashed = None
    if dirty and workspace == "stash":
        sh(["git", "stash", "push", "-u", "-q", "-m", f"feature-ladder {slug}: pre-run changes",
            "--", ".", ":(exclude).feature-ladder"], root)
        stashed = "stash@{0} (feature-ladder " + slug + ": pre-run changes)"
    sh(["git", "checkout", "-q", "-b", branch], root)
    pre = None
    if dirty and workspace != "stash":
        commit_all(root, "Pre-existing uncommitted changes (made by the user, not by feature-ladder)", user_only=True)
        pre = sh(["git", "rev-parse", "--short", "HEAD"], root)[1]
    ok, why = pr_ready(root)
    intake = {}
    if a.get("--intake") and Path(a["--intake"]).is_file():
        try:
            intake = json.loads(Path(a["--intake"]).read_text(encoding="utf-8-sig"))
        except ValueError:
            intake = {}
    environments = intake.get("environments") or {}
    if not environments:  # no intake: still learn the deployment targets from the repo
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "complexity-router" / "scripts"))
            from intake import scan as _scan
            environments = _scan("", str(root)).get("environments", {})
        except Exception:  # noqa: BLE001 - best effort
            environments = {}
    run = {"outside_policy": "all",  # informational since v1.11: the hook logs, it does not gate
           "budget": intake.get("budget") or {"size": "S", "workers": 1, "design": False},
           "switches": intake.get("switches") or [],
           "environments": environments,
           "slug": slug, "base": base, "branch": branch, "stack_base": base,
           "active": True, "started": now(), "pr_enabled": ok,
           "pr_disabled_reason": why or None, "pre_existing_commit": pre,
           "stash": stashed, "checkpoints": []}
    save(root, run)
    return run


def cmd_checkpoint(root, a):
    run = load(root)
    kind = "decision"
    title = a.get("--title") or kind
    body = a.get("--body-file")
    if not body or not Path(body).is_file():
        raise RuntimeError("--body-file is required")
    n = len(run["checkpoints"]) + 1
    head, base = run["branch"], run["stack_base"]
    commit_all(root, f"feature-ladder checkpoint {n} ({kind}): {title}", allow_empty=True)
    label = "Decision needed"
    entry = {"n": n, "kind": kind, "title": title, "branch": head, "base": base,
             "commit": sh(["git", "rev-parse", "--short", "HEAD"], root)[1], "at": now()}
    run["checkpoints"].append(entry)
    entry["pr"] = open_pr(root, run, base, head, f"[feature-ladder] {label}: {title}",
                          with_stack(body, run, root), draft=True)
    nxt = free_branch(root, f"feature-ladder/{run['slug']}-pt{n + 1}")
    sh(["git", "checkout", "-q", "-b", nxt], root)
    run["stack_base"], run["branch"] = head, nxt
    save(root, run)
    return {"checkpoint": entry, "now_on": nxt, "pr_enabled": run["pr_enabled"],
            "warnings": run.get("warnings", [])}


def cmd_finish(root, a):
    run = load(root)
    title = a.get("--title") or run["slug"]
    body = a.get("--body-file")
    if not body or not Path(body).is_file():
        raise RuntimeError("--body-file is required")
    commit_all(root, f"feature-ladder: {title}")
    head, base = run["branch"], run["stack_base"]
    ahead = sh(["git", "rev-list", "--count", f"{base}..{head}"], root, check=False)[1]
    final = {"n": len(run["checkpoints"]) + 1, "kind": "final", "title": title,
             "branch": head, "base": base, "at": now()}
    draft = "--draft" in a
    if ahead not in ("", "0"):
        run["checkpoints"].append(final)
        final["pr"] = open_pr(root, run, base, head, f"[feature-ladder] {title}",
                              with_stack(body, run, root), draft=draft)
    merged = []
    if "--no-merge" not in a and not draft:
        merged = merge_stack(root, run)
    run["active"], run["finished"] = False, now()
    save(root, run)
    return {"stack": run["checkpoints"], "base": run["base"], "pr_enabled": run["pr_enabled"],
            "pr_disabled_reason": run.get("pr_disabled_reason"),
            "merged": merged, "review": REVIEW_LABEL if merged else None,
            "stash": run.get("stash"), "warnings": run.get("warnings", [])}


def cmd_abort(root, a):
    run = load(root)
    run["active"], run["aborted"] = False, now()
    save(root, run)
    return {"aborted": True, "branch": run.get("branch"), "stash": run.get("stash")}


def parse(argv):
    a, i = {}, 0
    while i < len(argv):
        if argv[i].startswith("--"):
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                a[argv[i]] = argv[i + 1]
                i += 2
                continue
            a[argv[i]] = True
        i += 1
    return a


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    root = root_dir()
    if root is None:
        print(json.dumps({"error": "not a git repository: work stays in place, irreversible steps are "
                                   "written to FEATURE-REPORT.md and never run"}))
        return 2
    a = parse(argv[1:])
    try:
        fn = {"start": cmd_start, "checkpoint": cmd_checkpoint, "finish": cmd_finish,
              "status": lambda r, _a: load(r), "abort": cmd_abort}[argv[0]]
        print(json.dumps(fn(root, a), indent=2))
        return 0
    except KeyError:
        print(__doc__)
        return 1
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
