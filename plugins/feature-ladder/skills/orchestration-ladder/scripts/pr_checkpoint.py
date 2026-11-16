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
             marked as the user's (include, default) or stashed (stash);
             untracked files that look like secrets (.env*, *.pem,
             *credentials*.json) are never committed. Writes
             .feature-ladder/run.json (environments, budget and switches),
             which switches on the audit hook and the spawn rules
             (session_budget.py). Refused while another run is active
             (finish or abort it first). If HEAD is still on the branch of
             the previous, finished run, this run starts from that run's
             base instead, so every feature of a session lands on the same
             base. run.json also records base_sha (where this run began) and
             session_base_sha (where the session's first run began), which
             /feature-ladder:wrap reviews from.

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
             finished, check the base branch out again (pulled when the PRs
             merged on GitHub) and print the stack with each merge result.
             --no-merge leaves the PRs open; --draft implies it. A merge
             GitHub refuses (branch protection, conflicts) is reported with
             its reason, never forced. Running finish again after --no-merge
             merges the same entry; it does not add a second one.

  status     Print run.json.

  abort      Mark the run inactive (turns the hook off) without opening PRs
             and go back to the base branch. Branches and commits are kept;
             with uncommitted changes HEAD stays where it is. Use after a
             run that died midway.

Run state lives in the MAIN checkout's .feature-ladder/ (ladder_state.py), so
a run started in a linked worktree is the same run for every script and hook.

Without a git remote or the gh CLI, everything still happens on local
branches: each PR body is saved to .feature-ladder/checkpoints/ and finish
merges the stack into the base branch locally (--no-ff). A run started on a
detached HEAD has no base branch: its work stays on the run's branch and is
reported as not merged. Exit 0 ok, 1 error, 2 not a git repository.
"""
import json
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ladder_state  # noqa: E402

RUN_FILE = Path(".feature-ladder") / "run.json"
REVIEW_LABEL = "review: pending"
REVIEW_LABEL_COLOR = "FBCA04"
# Seconds before a command is killed with its children. A push or a gh call
# waiting on a credential prompt would otherwise hang the run for good.
LOCAL_TIMEOUT = 300
NETWORK_TIMEOUT = 240
NETWORK_GIT = {"push", "pull", "fetch", "ls-remote", "clone"}
# Untracked files `git add -A` must not sweep into a commit that is then pushed.
SECRET_FILE = re.compile(r"(^|/)(\.env(\.[^/]*)?|[^/]*\.env|[^/]*\.pem|[^/]*credentials[^/]*\.json)$", re.I)
SECRET_FILE_OK = re.compile(r"\.(example|sample|template|tmpl|tpl|dist|defaults?)(\.|$)", re.I)


class NoRun(RuntimeError):
    """There is no run.json (as opposed to one that cannot be read)."""


_kill_tree = ladder_state.kill_tree  # one implementation, shared with verify.py and codex_review.py


def _spawn(args, cwd, env, limit):
    """Run to completion or raise TimeoutExpired after killing the whole process
    tree (a credential helper holding the pipes would otherwise keep us waiting)."""
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", env=env, **group)
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except (subprocess.SubprocessError, OSError, ValueError):
            pass
        raise
    return proc.returncode, out or "", err or ""


def _limit(args):
    override = os.environ.get("FEATURE_LADDER_GIT_TIMEOUT", "")
    if override.isdigit() and int(override) > 0:
        return int(override)
    network = args[:1] == ["gh"] or (args[:1] == ["git"] and any(a in NETWORK_GIT for a in args[1:3]))
    return NETWORK_TIMEOUT if network else LOCAL_TIMEOUT


def sh(args, cwd, check=True, timeout=None):
    """Run a command. Returns (exit code, text): stdout on success; on failure
    stdout or, when that is empty, stderr, so the caller has the reason (gh and
    git write refusals to stderr). Never prompts for credentials and never
    outlives its time limit: a timeout is exit 124 with a reason."""
    limit = timeout or _limit(args)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GH_PROMPT_DISABLED="1", GCM_INTERACTIVE="never")
    try:
        code, out, err = _spawn(args, cwd, env, limit)
    except subprocess.TimeoutExpired:
        msg = f"{' '.join(args[:3])}: timed out after {limit}s"
        if check:
            raise RuntimeError(msg) from None
        return 124, msg
    out, err = out.strip(), err.strip()
    if check and code != 0:
        raise RuntimeError(f"{' '.join(args)}: {err or out}")
    return code, (out if code == 0 else (out or err))


def root_dir():
    """The git work tree the commands run in (a linked worktree's own root)."""
    code, out = sh(["git", "rev-parse", "--show-toplevel"], os.getcwd(), check=False)
    return Path(out) if code == 0 and out else None


def state_root(root):
    """Where .feature-ladder/ lives: the main checkout, also for a linked worktree."""
    return ladder_state.state_root(root)


def run_file(root):
    return state_root(root) / RUN_FILE


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load(root):
    p = run_file(root)
    if not p.is_file():
        raise NoRun("no feature-ladder run in this repo (run `start` first)")
    try:
        run = ladder_state.read_json(p, strict=True)
    except ladder_state.CorruptState as e:
        raise RuntimeError(f"{e}. Fix .feature-ladder/run.json or delete it, then start again") from None
    if not isinstance(run, dict):
        raise RuntimeError("run.json does not hold a run (expected a JSON object). Delete "
                           ".feature-ladder/run.json, then start again")
    return run


def save(root, run):
    ladder_state.ensure_gitignore(state_root(root))
    ladder_state.write_json(run_file(root), run)


def previous_run(root):
    """The run.json already on disk, or None when there is none or it cannot be read."""
    try:
        return load(root)
    except RuntimeError:
        return None


def adopt_logged(root, run):
    """Keep outside-repo actions the audit hook logged while a command was running:
    the hook appends to run.json on its own, and saving our older copy would drop them."""
    disk = ladder_state.read_json(run_file(root), {})
    if not isinstance(disk, dict) or disk.get("slug") != run.get("slug"):
        return
    seen = {(r.get("at"), r.get("cmd")) for r in run.get("outside_actions", [])}
    for row in disk.get("outside_actions") or []:
        if isinstance(row, dict) and (row.get("at"), row.get("cmd")) not in seen:
            run.setdefault("outside_actions", []).append(row)


_IDENT = {}


def ident(root):
    """git -c flags so commits work even if user.name/email are not configured."""
    key = str(root)
    if key not in _IDENT:
        flags = []
        if sh(["git", "config", "user.name"], root, check=False)[0] != 0:
            flags += ["-c", "user.name=feature-ladder"]
        if sh(["git", "config", "user.email"], root, check=False)[0] != 0:
            flags += ["-c", "user.email=feature-ladder@localhost"]
        _IDENT[key] = flags
    return list(_IDENT[key])


def looks_secret(path):
    p = str(path).replace("\\", "/")
    return bool(SECRET_FILE.search(p)) and not SECRET_FILE_OK.search(p.rsplit("/", 1)[-1])


def unstage_new_secrets(root):
    """Take newly added files that look like secrets back out of the index.
    Files git already tracks are the user's decision and stay."""
    code, out = sh(["git", "-c", "core.quotePath=false", "diff", "--cached", "--name-only", "--diff-filter=A", "-z"],
                   root, check=False)
    if code != 0:
        return []
    found = [f for f in out.split("\0") if f and looks_secret(f)]
    for i in range(0, len(found), 50):
        sh(["git", "reset", "-q", "--"] + found[i:i + 50], root, check=False)
    return found


def commit_all(root, message, allow_empty=False, user_only=False, skipped=None):
    """Commit the work tree. Returns True when a commit was made. Untracked files
    that look like secrets are left out and appended to `skipped`."""
    sh(["git", "add", "-A", "--", "."] + ([":(exclude).feature-ladder"] if user_only else []), root)
    left_out = unstage_new_secrets(root)
    if skipped is not None:
        skipped.extend(f for f in left_out if f not in skipped)
    dirty = sh(["git", "diff", "--cached", "--quiet"], root, check=False)[0] != 0
    if not dirty and not allow_empty:
        return False
    args = ["git"] + ident(root) + ["commit", "-q", "-m", message]
    if not dirty:
        args.append("--allow-empty")
    sh(args, root)
    return True


def warn(run, text):
    if text not in run.setdefault("warnings", []):
        run["warnings"].append(text)


def warn_skipped(run, skipped):
    if skipped:
        warn(run, "not committed (looks like a secret; add it to .gitignore, or commit it yourself if it is not): "
                  + ", ".join(skipped))


def branch_exists(root, name):
    return sh(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], root, check=False)[0] == 0


def free_branch(root, name):
    cand, n = name, 2
    while branch_exists(root, cand):
        cand, n = f"{name}-{n}", n + 1
    return cand


def current_branch(root):
    """The branch HEAD is on; "HEAD" when detached; "" when it cannot be told."""
    code, out = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, check=False)
    return out if code == 0 else ""


def checkout(root, ref, detach=False):
    """Switch to ref. The generated .feature-ladder/.gitignore is untracked until
    the first feature commit; left in place it would block a switch to a branch
    that tracks it, so it is set aside and its lines restored afterwards."""
    gi = Path(root) / ".feature-ladder" / ".gitignore"
    kept = []
    if gi.is_file() and sh(["git", "ls-files", "--error-unmatch", ".feature-ladder/.gitignore"], root, check=False)[0] != 0:
        try:
            kept = gi.read_text(encoding="utf-8").splitlines()
            gi.unlink()
        except OSError:
            kept = []
    try:
        sh(["git", "checkout", "-q"] + (["--detach"] if detach else []) + [ref], root)
    finally:
        if kept:
            try:
                have = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
                missing = [line for line in kept if line not in have]
                if missing:
                    gi.parent.mkdir(parents=True, exist_ok=True)
                    gi.write_text("\n".join(have + missing) + "\n", encoding="utf-8")
            except OSError:
                pass


def user_changes(root):
    """Uncommitted paths outside .feature-ladder/ (porcelain lines)."""
    code, st = sh(["git", "status", "--porcelain"], root, check=False)
    return [line for line in st.splitlines() if line.strip() and ".feature-ladder/" not in line] if code == 0 else []


def return_to_base(root, run):
    """After a run, HEAD goes back to the branch the run started from, so the next
    feature starts from the base and not from this feature's branch. Returns the
    branch HEAD is on afterwards."""
    base = run.get("base")
    here = current_branch(root)
    if not base or here == base:
        return here
    try:
        if run.get("base_detached"):
            checkout(root, base, detach=True)
        elif branch_exists(root, base):
            checkout(root, base)
        else:
            return here
    except RuntimeError as e:
        warn(run, f"still on {here}: could not go back to {base} ({str(e)[-200:]})")
        return here
    return current_branch(root) or base


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
    """Put the review: pending label on a PR (creating the label once per run). Never fatal."""
    if not run.get("pr_enabled") or not url:
        return
    if not run.get("review_label_ready"):
        sh(["gh", "label", "create", REVIEW_LABEL, "--color", REVIEW_LABEL_COLOR, "--force",
            "--description", "Merged or deployed by feature-ladder; review afterwards"], root, check=False)
        run["review_label_ready"] = True
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
    d = state_root(root) / ".feature-ladder" / "checkpoints"
    d.mkdir(parents=True, exist_ok=True)
    local = d / f"{head.replace('/', '__')}.md"
    local.write_text(f"# {title}\n\nbase: {base}\nhead: {head}\n\n" +
                     Path(body_file).read_text(encoding="utf-8"), encoding="utf-8")
    return None


def merge_entry(root, run, entry):
    """Merge one PR of the stack (GitHub) or one branch (local). Records the result on the entry."""
    # Every entry lands on the ORIGINAL base: a later entry's recorded base is the previous
    # feature branch, and merging into that would leave the original base without it.
    head, base = entry["branch"], run.get("base") or entry["base"]
    if run.get("pr_enabled") and entry.get("pr"):
        url = entry["pr"]
        if entry["base"] != base:  # retarget the stacked PR to the original base before merging
            sh(["gh", "pr", "edit", url, "--base", base], root, check=False)
        sh(["gh", "pr", "ready", url], root, check=False)  # drafts cannot merge
        code, out = sh(["gh", "pr", "merge", url, "--merge"], root, check=False)
        if code == 0:
            entry["merged"] = "merged"
            return True
        code2, out2 = sh(["gh", "pr", "merge", url, "--merge", "--auto"], root, check=False)
        if code2 == 0:
            entry["merged"] = "queued (auto-merge when checks pass)"
            run.setdefault("warnings", []).append(f"{url}: {entry['merged']} - pending, later PRs retargeted to {base}")
            return True
        entry["merged"] = f"not merged: {(out or out2).strip()[-240:]}"
        run.setdefault("warnings", []).append(f"{url}: {entry['merged']}")
        return False
    if run.get("base_detached"):
        # The run began on a detached HEAD: a merge there would sit on no branch and be lost.
        entry["merged"] = (f"not merged: the run started on a detached HEAD ({str(base)[:7]}), so there is no base "
                           f"branch to merge into; the work is on {head}")
        run.setdefault("warnings", []).append(f"{head}: {entry['merged']}")
        return False
    # Local-only: fast-forward or --no-ff merge into the base branch, then come back.
    current = current_branch(root)
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
        if current and current != "HEAD":
            sh(["git", "checkout", "-q", current], root, check=False)


def merge_stack(root, run):
    """Merge every PR/branch of the stack in order; stop at the first one GitHub refuses.
    Entries that already landed are skipped; one refused earlier is tried again, so a
    later finish cannot merge the entries behind it out of order."""
    results = []
    for entry in run.get("checkpoints", []):
        if str(entry.get("merged") or "").startswith(("merged", "queued")):
            continue
        ok = merge_entry(root, run, entry)
        results.append({"branch": entry["branch"], "result": entry["merged"]})
        if not ok:
            break
    return results


def _redact(text):
    try:
        from irreversible import redact  # lazy: irreversible imports this module
        return redact(text)
    except Exception:  # noqa: BLE001 - the list must render even if the import fails
        return text


def pending_lines(run):
    """The review: pending rows as markdown bullets (for the report and the PR body).
    The one formatter: irreversible.pending_lines is this function."""
    rows = [r for r in run.get("outside_actions", []) if isinstance(r, dict) and r.get("review") == "pending"]
    return [f"- [ ] {r.get('label')} ({r.get('environment') or 'repo'}): `{_redact(str(r.get('cmd', '')))[:160]}`"
            for r in rows]


def pending_note(run):
    lines = pending_lines(run)
    if not lines:
        return ""
    head = ["", f"**{REVIEW_LABEL}** - these ran during the feature; review them afterwards:"]
    return "\n".join(head + lines) + "\n"


def stack_note(run):
    lines = ["", "---", "**feature-ladder PR stack** (merged in this order):"]
    for i, c in enumerate(run.get("checkpoints", []), 1):
        lines.append(f"{i}. `{c['branch']}` ({c['kind']}): {c['title']}" + (f" {c['pr']}" if c.get("pr") else ""))
    return "\n".join(lines) + "\n" + pending_note(run)


def with_stack(body_file, run, root):
    text = Path(body_file).read_text(encoding="utf-8") + stack_note(run)
    out = state_root(root) / ".feature-ladder" / "checkpoints" / "_body.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return str(out)


def run_branches(run):
    return {run.get("branch")} | {c.get("branch") for c in run.get("checkpoints", []) if isinstance(c, dict)}


def landed(run):
    """True when every entry of a run's stack was merged or queued to merge
    (a run that committed nothing has nothing left behind either)."""
    stack = [c for c in run.get("checkpoints", []) if isinstance(c, dict)]
    return all(str(c.get("merged", "")).startswith(("merged", "queued")) for c in stack)


def read_intake(path):
    """The intake a run starts from, marked used so the same request does not
    silently reuse it for a later run."""
    if not path or path is True or not Path(path).is_file():
        return {}
    intake = ladder_state.read_json(Path(path), {})
    if not isinstance(intake, dict):
        return {}
    if not intake.get("used"):
        intake["used"] = now()
        try:
            ladder_state.write_json(Path(path), intake)
        except OSError:
            pass
    return intake


def release_intake(path):
    """Unmark a run's intake on abort so `intake.py find` can return it again."""
    if not path or not Path(path).is_file():
        return
    intake = ladder_state.read_json(Path(path), {})
    if not isinstance(intake, dict) or not intake.get("used"):
        return
    intake.pop("used", None)
    try:
        ladder_state.write_json(Path(path), intake)
    except OSError:
        pass


# ------------------------------------------------------------------ commands --
def cmd_start(root, a):
    slug = a.get("--slug") or "feature"
    if slug is True:
        slug = "feature"
    prev = previous_run(root)
    if prev and prev.get("active"):
        raise RuntimeError(f"a feature-ladder run is already active here (slug '{prev.get('slug')}', branch "
                           f"{prev.get('branch')}). Run `finish` to complete it or `abort` to drop it, then start "
                           "again; starting over it would lose its logged outside-repo actions")
    notes = []
    if prev is None and run_file(root).is_file():
        notes.append("the run.json found here was unreadable and has been replaced by this run; if a run was "
                     "active, its branch and commits are still in git")
    code, base = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root, check=False)
    if code != 0 or not base:  # no commits yet: an EMPTY root commit so branches have a base
        sh(["git"] + ident(root) + ["commit", "-q", "--allow-empty", "-m", "feature-ladder: initial commit"], root)
        _, base = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    detached = base == "HEAD"
    if detached:  # detached: anchor the stack on the commit itself
        base = sh(["git", "rev-parse", "HEAD"], root)[1]
        notes.append(f"started on a detached HEAD ({base[:7]}): there is no base branch, so finish will leave the "
                     "work on the run's branch instead of merging it")
    elif prev and base in run_branches(prev) and prev.get("base") and prev["base"] != base \
            and not prev.get("base_detached") and branch_exists(root, prev["base"]):
        # HEAD is still on the previous run's branch. Starting from it would stack this feature
        # on that branch and "merge" it there, never reaching the base the session works on.
        try:
            checkout(root, prev["base"])
        except RuntimeError as e:
            raise RuntimeError(f"HEAD is on {base}, the branch of the finished run '{prev.get('slug')}', and "
                               f"switching to its base {prev['base']} failed ({str(e)[-200:]}). Check out "
                               f"{prev['base']} (commit or stash your changes first), then start again") from None
        notes.append(f"HEAD was on {base}, the branch of the finished run '{prev.get('slug')}'; this run starts from "
                     f"that run's base {prev['base']}" + ("" if landed(prev) else
                                                         f" (that run was not merged: its work is on {base}, not here)"))
        base = prev["base"]
    elif base.startswith("feature-ladder/"):
        notes.append(f"started from {base}, itself a feature-ladder branch: this run merges into {base}, not into "
                     "the branch that one came from")
    base_sha = sh(["git", "rev-parse", "HEAD"], root)[1]
    session = ladder_state.read_json(state_root(root) / ".feature-ladder" / "session.json", {})
    session_started = session.get("started") if isinstance(session, dict) else None
    session_base = base_sha
    if prev and session_started and prev.get("session_started") == session_started and prev.get("session_base_sha"):
        # Same Claude Code session: the session's review range still starts at its first run.
        if sh(["git", "merge-base", "--is-ancestor", prev["session_base_sha"], base_sha], root, check=False)[0] == 0:
            session_base = prev["session_base_sha"]
    workspace = a.get("--workspace") or "include"
    dirty = user_changes(root)
    branch = free_branch(root, f"feature-ladder/{slug}")
    stashed = None
    if dirty and workspace == "stash":
        sh(["git", "stash", "push", "-u", "-q", "-m", f"feature-ladder {slug}: pre-run changes",
            "--", ".", ":(exclude).feature-ladder"], root)
        stashed = "stash@{0} (feature-ladder " + slug + ": pre-run changes)"
    sh(["git", "checkout", "-q", "-b", branch], root)
    pre, skipped = None, []
    if dirty and workspace != "stash":
        if commit_all(root, "Pre-existing uncommitted changes (made by the user, not by feature-ladder)",
                      user_only=True, skipped=skipped):
            pre = sh(["git", "rev-parse", "--short", "HEAD"], root)[1]
    ok, why = pr_ready(root)
    intake_path = a.get("--intake")
    intake = read_intake(intake_path)
    environments = intake.get("environments") or {}
    if not environments:  # no intake: still learn the deployment targets from the repo
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "complexity-router" / "scripts"))
            import intake as _intake
            if hasattr(_intake, "environments"):  # reads .firebaserc only: no whole-repo scan
                environments = _intake.environments(str(root))
            else:
                environments = _intake.scan("", str(root)).get("environments", {})
        except Exception:  # noqa: BLE001 - best effort
            environments = {}
    run = {"outside_policy": "all",  # informational since v1.11: the hook logs, it does not gate
           "budget": intake.get("budget") or {"size": "S", "workers": 1, "design": False},
           "switches": intake.get("switches") or [],
           "orchestrator": intake.get("orchestrator") or {},
           "environments": environments,
           "slug": slug, "base": base, "branch": branch, "stack_base": base,
           "base_sha": base_sha, "base_detached": detached,
           "session_base_sha": session_base, "session_started": session_started,
           "active": True, "started": now(), "pr_enabled": ok,
           "pr_disabled_reason": why or None, "pre_existing_commit": pre,
           "stash": stashed, "checkpoints": []}
    if intake_path and intake_path is not True:
        run["intake"] = str(Path(intake_path))
    for note in notes:
        warn(run, note)
    warn_skipped(run, skipped)
    save(root, run)
    return run


def cmd_checkpoint(root, a):
    run = load(root)
    kind = "decision"
    title = a.get("--title") or kind
    body = a.get("--body-file")
    if not body or body is True or not Path(body).is_file():
        raise RuntimeError("--body-file is required")
    n = len(run["checkpoints"]) + 1
    head, base = run["branch"], run["stack_base"]
    skipped = []
    commit_all(root, f"feature-ladder checkpoint {n} ({kind}): {title}", allow_empty=True, skipped=skipped)
    warn_skipped(run, skipped)
    label = "Decision needed"
    entry = {"n": n, "kind": kind, "title": title, "branch": head, "base": base,
             "commit": sh(["git", "rev-parse", "--short", "HEAD"], root)[1], "at": now()}
    run["checkpoints"].append(entry)
    entry["pr"] = open_pr(root, run, base, head, f"[feature-ladder] {label}: {title}",
                          with_stack(body, run, root), draft=True)
    nxt = free_branch(root, f"feature-ladder/{run['slug']}-pt{n + 1}")
    sh(["git", "checkout", "-q", "-b", nxt], root)
    run["stack_base"], run["branch"] = head, nxt
    adopt_logged(root, run)
    save(root, run)
    return {"checkpoint": entry, "now_on": nxt, "pr_enabled": run["pr_enabled"],
            "warnings": run.get("warnings", [])}


def cmd_finish(root, a):
    run = load(root)
    title = a.get("--title") or run["slug"]
    body = a.get("--body-file")
    if not body or body is True or not Path(body).is_file():
        raise RuntimeError("--body-file is required")
    head, base = run["branch"], run["stack_base"]
    here = current_branch(root)
    if here and here != head and branch_exists(root, head):
        # A finish after --no-merge runs from the base branch; committing there would put
        # the work straight onto the base, so go back to the run's branch first.
        try:
            checkout(root, head)
        except RuntimeError as e:
            raise RuntimeError(f"HEAD is on {here}, not on the run's branch {head}, and switching failed "
                               f"({str(e)[-200:]}). Check out {head}, then finish again") from None
    skipped = []
    committed = commit_all(root, f"feature-ladder: {title}", skipped=skipped)
    warn_skipped(run, skipped)
    code, ahead = sh(["git", "rev-list", "--count", f"{base}..{head}"], root, check=False)
    ahead = ahead if code == 0 else "0"
    draft = "--draft" in a
    last = run["checkpoints"][-1] if run["checkpoints"] else None
    if last and last.get("kind") == "final" and last.get("branch") == head:
        # finish again (after --no-merge or a refused merge): the same entry, not a second one
        if committed and run.get("pr_enabled") and last.get("pr"):
            code, out = sh(["git", "push", "-q", "origin", head], root, check=False)
            if code != 0:
                warn(run, f"{head}: new commits not pushed ({out[-200:]})")
    elif ahead not in ("", "0"):
        final = {"n": len(run["checkpoints"]) + 1, "kind": "final", "title": title,
                 "branch": head, "base": base, "at": now()}
        run["checkpoints"].append(final)
        final["pr"] = open_pr(root, run, base, head, f"[feature-ladder] {title}",
                              with_stack(body, run, root), draft=draft)
    merged = []
    if "--no-merge" not in a and not draft:
        merged = merge_stack(root, run)
    run["active"], run["finished"] = False, now()
    on = return_to_base(root, run)
    if run.get("pr_enabled") and on == run.get("base") and any(
            c.get("pr") and c.get("merged") == "merged" for c in run["checkpoints"]):
        code, out = sh(["git", "pull", "-q", "--ff-only", "origin", run["base"]], root, check=False)
        if code != 0:
            warn(run, f"local {run['base']} is behind origin: pull it before the next feature ({out[-200:]})")
    adopt_logged(root, run)
    save(root, run)
    landed_now = [m for m in merged if str(m["result"]).startswith(("merged", "queued"))]
    return {"stack": run["checkpoints"], "base": run["base"], "on": on, "pr_enabled": run["pr_enabled"],
            "pr_disabled_reason": run.get("pr_disabled_reason"),
            "merged": merged, "review": REVIEW_LABEL if landed_now else None,
            "stash": run.get("stash"), "warnings": run.get("warnings", [])}


def cmd_abort(root, a):
    run = load(root)
    run["active"], run["aborted"] = False, now()
    release_intake(run.get("intake"))
    out = {"aborted": True, "branch": run.get("branch"), "stash": run.get("stash")}
    if user_changes(root):
        out["on"] = current_branch(root)
        out["note"] = (f"uncommitted changes: HEAD stays on {out['on']}. Commit or stash them, then "
                       f"`git checkout {run.get('base')}` before the next feature")
    else:
        out["on"] = return_to_base(root, run)
    adopt_logged(root, run)
    save(root, run)
    if run.get("warnings"):
        out["warnings"] = run["warnings"]
    return out


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


COMMANDS = {"start": cmd_start, "checkpoint": cmd_checkpoint, "finish": cmd_finish,
            "status": lambda r, _a: load(r), "abort": cmd_abort}
OPTIONS = {"start": ("--slug", "--workspace", "--intake"), "checkpoint": ("--title", "--body-file"),
           "finish": ("--title", "--body-file", "--draft", "--no-merge"), "status": (), "abort": ()}


def check_options(cmd, a):
    """A misspelt option used to be ignored (start --slgu x ran as slug 'feature')."""
    unknown = [k for k in a if k not in OPTIONS[cmd]]
    if unknown:
        allowed = ", ".join(OPTIONS[cmd]) or "none"
        raise RuntimeError(f"unknown option {', '.join(unknown)} for {cmd} (allowed: {allowed})")
    if cmd == "start" and a.get("--workspace") not in (None, "include", "stash"):
        raise RuntimeError(f"--workspace must be include or stash, not {a['--workspace']}")


def main(argv):
    ladder_state.utf8_stdio()
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    fn = COMMANDS.get(argv[0])
    if fn is None:
        print(__doc__)
        return 1
    root = root_dir()
    if root is None:
        print(json.dumps({"error": "not a git repository: work stays in place, irreversible steps are "
                                   "written to FEATURE-REPORT.md and never run"}))
        return 2
    a = parse(argv[1:])
    try:
        check_options(argv[0], a)
        print(json.dumps(fn(root, a), indent=2))
        return 0
    except KeyError as e:
        print(json.dumps({"error": f"run.json has no {e} (written by an older version or an interrupted run). "
                                   "Run `abort`, or delete .feature-ladder/run.json, then start again"}))
        return 1
    except (RuntimeError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
