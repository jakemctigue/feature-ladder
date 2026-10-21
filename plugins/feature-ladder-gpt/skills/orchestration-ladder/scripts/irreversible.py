#!/usr/bin/env python3
"""
feature-ladder: deterministic gate for commands with effects outside the repo.

Classification costs no model tokens. The guard hook calls it on every Bash
command during a ladder run:

  mundane    runs immediately and is logged (emulator / non-production
             targets, deploys, index builds, exports, previews, dry runs, and
             anything else that doesn't destroy existing production data)
  risky      production data overwritten or deleted, or production resources
             deleted. Needs one short Fable verdict first:
               irreversible.py approve --cmd "<exact command>" --verdict-file F
             F must contain "VERDICT: APPROVE" (from second-opinion, fable/low).
             Then the exact command runs and is logged.
  never      a short list that is never run autonomously (deleting a
             project, database or repo, weakening database protection,
             force-pushing or deleting the base branch, pushing to the base
             branch, merging PRs, deleting backups). Logged as left for the user.

The run's policy (set once at intake) can narrow this: "nonprod" turns every
production action into "left for the user"; "none" does that for everything
outside the repo.

Only the executable part of a command is classified: quoted strings, heredocs
and comments are stripped first, so `git commit -m "drop table"` or
`grep "DELETE FROM"` are not commands. Code that a shell will still run is
classified too: the payload of `bash -c '...'` / `sh -c` / `eval`, a heredoc
fed to a shell, `$(...)` and backticks, and the command after `--` in
with_secrets.py. Calls to our own scripts are skipped one segment at a time,
so `route.py ...; <command>` still classifies the command.

  classify "<command>"            print the decision as JSON (no side effects)
  approve --cmd C --verdict-file F
  report                          markdown table of outside-repo actions
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pr_checkpoint import load, now, root_dir, save  # noqa: E402

I = re.IGNORECASE
BR = r"(?:\s|:|\+)"  # what precedes a branch name in a refspec

NEVER = [
    (r"\bgcloud\s+projects\s+delete\b", "delete a cloud project"),
    (r"\bgcloud\s+firestore\s+databases\s+delete\b", "delete a Firestore database"),
    (r"\bgcloud\s+firestore\s+databases\s+update\b.*(no-enable-pitr|DELETE_PROTECTION_DISABLED)", "weaken database protection"),
    (r"\bgh\s+repo\s+delete\b", "delete a repository"),
    (r"\bgh\s+pr\s+merge\b", "merge a PR"),
    (r"\bgit\s+push\b[^;&|]*(--force\b|\s-f\b|--force-with-lease|\s\+)[^;&|]*" + BR + r"(main|master|production|release)(\s|$)", "force-push a shared branch"),
    (r"\bgit\s+push\b[^;&|]*(\s:(main|master)(\s|$)|--delete\s+(main|master)(\s|$))", "delete main/master"),
    (r"\b(gsutil\s+(-m\s+)?rm|gcloud\s+storage\s+rm)\b[^;&|]*backup", "delete backups"),
    (r"\bterraform\s+destroy\b", "terraform destroy"),
    (r"\brm\b(?=[^;&|]*\s(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(\s|$))[^;&|]*\s(/\*?|~/?\*?|\$\{?HOME\}?/?\*?)(\s|$)",
     "recursively delete / or the home folder"),
    (r"\bdrop\s+database\b", "drop a database"),
    (r"\bgcloud\s+secrets\s+(delete|versions\s+(destroy|disable))\b", "delete or disable a secret"),
    (r"\bgcloud\s+secrets\s+(add-iam-policy-binding|set-iam-policy)\b", "change who can read a secret"),
    (r"\bfirebase\s+functions:secrets:(destroy|prune)\b", "destroy a Firebase secret"),
]

# Commands whose output would put a secret value into the transcript or a file.
# Workers use with_secrets.py instead, which keeps the value in one command's
# environment and masks it in the output.
SECRET_EXPOSURE = [
    (r"\bgcloud\s+secrets\s+versions\s+access\b", "reads a secret value"),
    (r"\bfirebase\s+functions:(secrets:access|config:get)\b", "reads a secret value"),
    (r"\b(cat|type|less|more|head|tail|bat)\s+[^;&|]*\.env(\.\w+)?(\s|$)", "prints a .env file"),
    (r"(^|[;&|]\s*)(printenv|env|set)\s*($|[;&|])", "dumps the environment"),
    (r"\b(echo|printf|print)\b[^;&|]*\$\{?[A-Za-z_]*(KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL)[A-Za-z_]*\b", "prints a secret variable"),
    (r"\b(KEY|SECRET|TOKEN|PASSWORD)\S*=\S{16,}", "puts a secret value on the command line"),
]

# (pattern, label, destroys_data). Everything here has effects outside the repo.
# Script-name patterns exclude test files (tests/, test_*, *_test, *.spec).
_NOT_TEST = r"(?!\S*(?:^|/|\\)(?:tests?|spec|__tests__)(?:/|\\))(?!\S*(?:^|/|\\)test_)(?!\S*[._-]test\.)(?!\S*\.spec\.)"
OUTSIDE = [
    (r"\bfirebase\s+firestore:delete\b", "delete Firestore documents", True),
    (r"\bfirebase\s+database:(set|remove|update|push)\b", "write Realtime Database", True),
    (r"\bfirebase\s+auth:import\b", "import auth users", True),
    (r"\bfirebase\s+functions:delete\b", "delete Cloud Functions", True),
    (r"\bfirebase\s+deploy\b", "firebase deploy", False),
    (r"\bfirebase\s+hosting:", "hosting change", False),
    (r"\bgcloud\s+firestore\s+import\b", "import into Firestore (overwrites)", True),
    (r"\bgcloud\s+firestore\s+export\b", "export Firestore", False),
    (r"\bgcloud\s+secrets\s+(create|versions\s+add|update)\b", "write a secret", True),
    (r"\bfirebase\s+functions:secrets:set\b", "write a Firebase secret", True),
    (r"\bgcloud\b[^;&|]*\s(delete|remove)\b", "delete cloud resource", True),
    (r"\b(gsutil\s+(-m\s+)?rm|gcloud\s+storage\s+rm)\b", "delete stored objects", True),
    (r"\b(node|tsx|ts-node|deno|bun|python3?|py|npx\s+\S+)\s+" + _NOT_TEST + r"(\S+[/\\])*\S*(migrat|backfill|purge|cleanup|clean-up|wipe|reset|dedupe|delete|import|seed)\S*\.(m?[jt]s|cjs|py)\b", "data script", True),
    (r"\b(npm|pnpm|yarn)\s+(run\s+)?(\S*:)?(migrat|backfill|purge|cleanup|wipe|seed|db:reset|reset-db|reset:db)\S*", "data script", True),
    (r"\b(drop\s+table|truncate\s+table|delete\s+from|alter\s+table\s+\S+\s+drop)\b", "destructive SQL", True),
    (r"\bupdate\s+\w+\s+set\b", "SQL update", True),
    (r"\b(prisma\s+migrate\s+deploy|alembic\s+upgrade|knex\s+migrate|rails\s+db:migrate|manage\.py\s+migrate|flyway\s+migrate)\b", "schema migration", True),
    (r"\b(terraform\s+apply|pulumi\s+up)\b", "infrastructure change", False),
    (r"\bkubectl\s+delete\b|\bhelm\s+(uninstall|delete)\b", "delete cluster resources", True),
    (r"\bkubectl\s+(apply|replace|scale|rollout)\b|\bhelm\s+(install|upgrade)\b", "cluster change", False),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|\bdocker\s+push\b", "publish", False),
    (r"\b(vercel|netlify|fly|railway|wrangler)\b[^;&|]*\b(deploy|--prod|up)\b", "deploy", False),
    (r"\bgh\s+release\s+create\b", "release", False),
    (r"\bgit\s+push\b[^;&|]*(--force\b|\s-f\b|--force-with-lease)", "force push", False),
    (r"\baws\s+\S+\s+(delete|terminate|remove|rm|rb)\S*|\baz\b[^;&|]*\sdelete\b", "delete cloud resource", True),
]

# Tools whose target, if unnamed, is a real service (assume production).
CLOUD_TOOLS = r"\b(firebase|gcloud|gsutil|aws|az|terraform|pulumi|kubectl|helm|vercel|netlify|fly|railway|wrangler)\b"
# Tools that read the Firebase project alias / default.
FIREBASE_TOOLS = r"\b(firebase|gcloud|gsutil)\b"

SAFE_HINTS = [r"--dry-run\b", r"--dryrun\b", r"\bdry_run\b", r"--preview\b", r"hosting:channel:deploy",
              r"--only\s+firestore:indexes(?![^;&|]*--force)"]
EMULATOR_HINTS = [r"emulators:(exec|start)", r"FIRESTORE_EMULATOR_HOST=", r"\blocalhost\b|127\.0\.0\.1"]
NONPROD_WORDS = r"\b(dev|devel|development|test|testing|staging|stage|qa|sandbox|local|demo)\b"
PROD_WORDS = r"\b(prod|production|live)\b"

# Our own scripts take commands and SQL as quoted arguments; never classify them.
OWN_SCRIPTS = re.compile(r"\b(irreversible|pr_checkpoint|intake|route|select_skills|resolve_bank|codex_review|verify|ledger)\.py\b")


HEREDOC = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?[^\n]*)\n.*?\n\s*\2\b", re.S)
SHELL_HEREDOC = re.compile(r"\b(?:ba|z|da|k)?sh\b[^;&|\n]*<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\b", re.S)


def executable_text(cmd):
    """Strip quoted strings, heredoc bodies and comments: what is left is what runs."""
    t = HEREDOC.sub(r"\1 ", cmd)  # heredoc bodies; the rest of the opening line still runs
    t = re.sub(r"'[^']*'", "''", t)
    t = re.sub(r'"(?:\\.|[^"\\])*"', '""', t)
    t = re.sub(r"(^|\s)#[^\n]*", " ", t)
    return t


def _project(cmd, env):
    m = re.search(r"(?:--project|-P)[=\s]+(\S+)", cmd)
    if m:
        alias = m.group(1).strip("'\"")
    elif re.search(FIREBASE_TOOLS, cmd, I):
        alias = env.get("default") or ""
    else:
        return ""
    return env.get("aliases", {}).get(alias, alias)


def _environment(cmd, env):
    if any(re.search(p, cmd, I) for p in EMULATOR_HINTS):
        return "emulator"
    proj = _project(cmd, env)
    if proj.startswith("demo-"):
        return "emulator"
    if proj and proj in env.get("nonproduction", []):
        return "nonprod"
    if proj and proj in env.get("production", []):
        return "prod"
    if re.search(PROD_WORDS, cmd, I):
        return "prod"
    if re.search(NONPROD_WORDS, cmd, I):
        return "nonprod"
    if proj or re.search(CLOUD_TOOLS, cmd, I):
        return "prod"  # unknown target on a real service: assume production
    if env.get("production") and re.search(r"\b(node|tsx|ts-node|deno|bun|npx)\b", cmd, I) and not re.search(r"\b(npm|pnpm|yarn)\s+run\b", cmd, I):
        return "prod"  # a JS data script in a Firebase repo talks to the default (production) project
    return "nonprod"   # local tools with no target named (dev database, local scripts)


SHELL_C = re.compile(r"\b(?:ba|z|da|k)?sh\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*c[a-zA-Z]*\s+(?:'([^']*)'|\"((?:\\.|[^\"\\])*)\")")
EVAL = re.compile(r"\beval\s+(?:'([^']*)'|\"((?:\\.|[^\"\\])*)\")")
SUBST = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")
RANK = {"deny": 3, "needs_verdict": 2, "allow": 1}


def _segments(cmd):
    """Split on unquoted ; & | and newlines; quoted text stays inside its segment."""
    segs, cur, q, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and q != "'" and i + 1 < len(cmd):
            cur.append(cmd[i:i + 2])
            i += 2
            continue
        if q:
            q = None if ch == q else q
            cur.append(ch)
        elif ch in "'\"":
            q = ch
            cur.append(ch)
        elif ch in ";&|\n":
            segs.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    segs.append("".join(cur))
    return [g for g in segs if g.strip()]


def _quoted(cmd):
    """For each position in cmd: the quote it is inside (' or "), or None."""
    mask, q, i = [None] * len(cmd), None, 0
    while i < len(cmd):
        ch = cmd[i]
        if ch == "\\" and q != "'" and i + 1 < len(cmd):
            mask[i] = mask[i + 1] = q
            i += 2
            continue
        if q:
            mask[i] = q
            q = None if ch == q else q
        elif ch in "'\"":
            mask[i] = q = ch
        i += 1
    return mask


def _unescape(dq):
    return re.sub(r'\\(["\\$`])', r"\1", dq)


def _inner_commands(raw):
    """Code a shell runs that quote-stripping would hide."""
    mask = _quoted(raw)
    wrapped = [m for rx in (SHELL_C, EVAL) for m in rx.finditer(raw) if not mask[m.start()]]
    inner = [m.group(1) if m.group(1) is not None else _unescape(m.group(2)) for m in wrapped]
    # $(...) and backticks run unless single-quoted or escaped
    inner += [m.group(1) if m.group(1) is not None else m.group(2) for m in SUBST.finditer(raw)
              if mask[m.start()] != "'" and not raw[:m.start()].endswith("\\")]
    return [c for c in inner if c.strip()]


def _worse(a, b):
    ra, rb = RANK[a["decision"]], RANK[b["decision"]]
    if ra != rb:
        return a if ra > rb else b
    return b if a["tier"] == "none" and b["tier"] != "none" else a


def classify(cmd, run, _depth=0):
    inner = [m.group(2) for m in SHELL_HEREDOC.finditer(cmd)]
    raw = HEREDOC.sub(r"\1 ", cmd)
    kept = []
    for seg in _segments(raw):
        if "with_secrets.py" in executable_text(seg):
            m = re.search(r"\s--\s(.*)$", seg, re.S)
            if m:
                kept.append(m.group(1))
        elif not OWN_SCRIPTS.search(executable_text(seg)):
            kept.append(seg)
    raw = " ; ".join(kept)
    out = _classify_text(executable_text(raw), run)
    if _depth < 4:
        for c in inner + _inner_commands(raw):
            out = _worse(out, classify(c, run, _depth + 1))
    return out


def _classify_text(text, run):
    env = run.get("environments") or {}
    policy = run.get("outside_policy") or "all"
    for pat, label in SECRET_EXPOSURE:
        if re.search(pat, text, I | re.M):
            return {"tier": "secret", "label": label, "decision": "deny",
                    "reason": f"this {label}, which would put it in the transcript. Run the command that needs it "
                              "through with_secrets.py --secret NAME -- <command> instead"}
    base = run.get("base") or ""
    never = list(NEVER)
    if base and base not in ("main", "master", "production", "release"):
        never.append((r"\bgit\s+push\b[^;&|]*" + BR + re.escape(base) + r"(\s|$)", f"push to the base branch {base}"))
    never.append((r"\bgit\s+push\b(?![^;&|]*" + BR + r"feature-ladder/)[^;&|]*" + BR + r"(main|master)(\s|$)", "push to main/master"))
    for pat, label in never:
        if re.search(pat, text, I):
            return {"tier": "never", "label": label, "decision": "deny",
                    "reason": f"'{label}' is never run autonomously; left for the user"}
    hit = next(((label, destroys) for pat, label, destroys in OUTSIDE if re.search(pat, text, I)), None)
    if not hit:
        return {"tier": "none", "decision": "allow"}
    label, destroys = hit
    where = _environment(text, env)
    safe = any(re.search(p, text, I) for p in SAFE_HINTS)
    risky = where == "prod" and destroys and not safe
    out = {"label": label, "environment": where, "tier": "risky" if risky else "mundane"}
    if policy == "none" or (policy == "nonprod" and where == "prod" and not safe):
        out.update(decision="deny", reason=f"intake policy '{policy}' leaves this for the user")
    elif risky:
        out.update(decision="needs_verdict",
                   reason="overwrites or deletes production data: get one Fable verdict, then approve")
    else:
        out.update(decision="allow")
    return out


def norm(cmd):
    return " ".join(cmd.split())


def record(run, cmd, c, status):
    run.setdefault("outside_actions", []).append(
        {"at": now(), "cmd": norm(cmd)[:300], "label": c.get("label"), "environment": c.get("environment"),
         "tier": c.get("tier"), "status": status})


def gate(cmd, run):
    """Used by the guard hook. Returns (allowed, reason, run_changed)."""
    c = classify(cmd, run)
    if c["decision"] == "allow":
        if c["tier"] == "none":
            return True, "", False
        record(run, cmd, c, "ran (mundane)")
        return True, "", True
    if c["decision"] == "needs_verdict":
        for a in run.get("approved", []):
            if a["cmd"] == norm(cmd) and not a.get("used"):
                a["used"] = now()
                record(run, cmd, c, "ran (Fable approved)")
                return True, "", True
        return False, (f"feature-ladder: '{c['label']}' on production overwrites or deletes data. Ask "
                       "second-opinion (fable/low) one question: is this exact command correct and safe to run "
                       "now? It must answer with a line 'VERDICT: APPROVE' or 'VERDICT: REJECT'. Save the "
                       "answer to a file, run irreversible.py approve --cmd \"<exact command>\" --verdict-file "
                       "<file>, then run the exact same command. Do not write undo scripts."), False
    if c["tier"] == "secret":
        record(run, cmd, c, "blocked: would expose a secret")
        return False, "feature-ladder: " + c["reason"] + ".", True
    record(run, cmd, c, "left for user: " + c["reason"])
    return False, "feature-ladder: " + c["reason"] + ". Note it in FEATURE-REPORT.md and carry on.", True


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    root = root_dir()
    try:
        run = load(root) if root else {}
    except RuntimeError:
        run = {}
    if argv[0] == "classify":
        print(json.dumps(classify(" ".join(argv[1:]), run), indent=2))
        return 0
    if argv[0] == "approve":
        a = dict(zip(argv[1::2], argv[2::2]))
        cmd, vf = a.get("--cmd"), a.get("--verdict-file")
        if not (cmd and vf and Path(vf).is_file() and root):
            print("approve needs --cmd, --verdict-file and an active run", file=sys.stderr)
            return 1
        text = Path(vf).read_text(encoding="utf-8-sig", errors="replace")
        if not re.search(r"^\s*VERDICT:\s*APPROVE\b", text, re.M | I):
            record(run, cmd, classify(cmd, run), "rejected by Fable")
            save(root, run)
            print(json.dumps({"approved": False, "reason": "verdict is not APPROVE; skip it and note it in the report"}))
            return 0
        run.setdefault("approved", []).append({"cmd": norm(cmd), "at": now()})
        save(root, run)
        print(json.dumps({"approved": True, "cmd": norm(cmd)}))
        return 0
    if argv[0] == "report":
        rows = run.get("outside_actions", [])
        if not rows:
            print("No commands with effects outside the repo were run.")
            return 0
        print("| When | Action | Where | Status | Command |\n|---|---|---|---|---|")
        for r in rows:
            print(f"| {r['at'][11:16]} | {r.get('label')} | {r.get('environment')} | {r['status']} | `{r['cmd'][:120]}` |")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
