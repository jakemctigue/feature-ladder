#!/usr/bin/env python3
"""
feature-ladder: deterministic classifier for commands with effects outside
the repo. It is an audit log, not a gate.

Classification costs no model tokens. The guard hook calls it on every Bash
command during a ladder run and records what ran:

  mundane    non-production targets, deploys, index builds, exports,
             previews, dry runs: runs at once, logged
  risky      production data overwritten or deleted, or production
             resources deleted: runs at once, logged with review: pending
  never      the former never-list (merging PRs, pushing or force-pushing
             the base branch, deleting a project, database, repo or backups,
             weakening database protection, rm -rf of / or $HOME). No
             longer a gate: runs at once, logged with review: pending so
             the report and the PR name it first.
  secret     the ONE denial left: a command that would put a secret VALUE
             into the transcript or the audit log (gcloud secrets versions
             access, cat .env, printenv, echo $X_KEY, X_KEY=<value> on the
             command line). Workers run such commands through
             with_secrets.py instead. Quoted spellings count ("$X_KEY",
             cat ".env"); text that only mentions one does not
             (git commit -m "cat .env"), and .env.example, lower-case
             shell variables, *_URL / *_FILE names and paths are not secrets.

"review: pending" is a status in FEATURE-REPORT.md and a label on the PR,
never a stop: the action has already gone up and the user reviews it
afterwards. Nothing waits for a verdict, there is no approve step, and the
intake asks for no outside-repo policy. Whether a step is a good idea is
answered by the confidence surface (confidence.py) from the project's own
memory, and that is advice about size and previews, not a block.

Only the executable part of a command is classified: quoted strings, heredocs
and comments are stripped first, so `git commit -m "drop table"` or
`grep "DELETE FROM"` are not commands. Code that a shell will still run is
classified too: the payload of `bash -c '...'` / `sh -c` / `eval`, a heredoc
fed to a shell, `$(...)` and backticks, and the command after `--` in
with_secrets.py. Calls to our own scripts (the script is what the interpreter
runs, not a file name somewhere in the command) are skipped one segment at a
time, so `route.py ...; <command>` still classifies the command.

Nothing secret is written to run.json: record() redacts secret-looking
assignments, flags, bearer tokens, URL passwords and well-known token shapes
before storing a command, and the report and PR lines redact again when they
are rendered.

  classify "<command>"            print the decision as JSON (no side effects)
  report                          markdown table of outside-repo actions
  pending                         the review: pending rows as a markdown list

hooks/guard-rs is a Rust port of gate() and the guard hook, used when built
(hooks/scripts/build_guard.sh). This file stays the reference: change a rule
here, mirror it in guard-rs/src/classify.rs, and tests/test_guard_rs.py
compares the two. A binary built before the edit hands every call back to
Python until it is rebuilt.
"""
import json
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ladder_state  # noqa: E402
from pr_checkpoint import NoRun, load, now, root_dir  # noqa: E402
from pr_checkpoint import pending_lines as _pending_lines  # noqa: E402

I = re.IGNORECASE
BR = r"(?:\s|:|\+|[\s:+]refs/heads/)"  # what precedes a branch name in a refspec

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

# Commands that would put a secret value into the transcript, a file or the
# audit log. Workers use with_secrets.py instead, which keeps the value in one
# command's environment and masks it in the output. The rules read a command
# the way a shell does (quotes removed, per command word): secret_exposure().
SECRET_WORDS = {"KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS"}
SECRET_ENDINGS = ("APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD")
# A name ending in one of these holds a reference to a secret, not its value.
NOT_A_VALUE = {"FILE", "FILES", "PATH", "DIR", "URL", "URI", "NAME", "NAMES", "ID", "IDS", "COUNT", "LEN", "LENGTH",
               "TTL", "TYPE", "HEADER", "PREFIX", "SUFFIX", "ENV", "VAR", "EXPIRY", "EXPIRES", "ENDPOINT", "HOST",
               "PORT", "VERSION", "ALG", "ALGORITHM", "SIZE", "FORMAT", "TIMEOUT", "LOCATION", "REGION", "ALIAS",
               "FIELD", "COLUMN", "USER", "USERNAME", "EMAIL", "SCOPE", "SCOPES", "ROTATION", "POLICY", "REF", "ARN"}
SECRET_FLAG_LAST = {"TOKEN", "PASSWORD", "PASSWD", "APIKEY"}
SECRET_FLAG_PAIR = {"API", "SECRET", "ACCESS", "PRIVATE", "CLIENT", "AUTH", "SIGNING", "ENCRYPTION", "MASTER"}
MIN_SECRET_CHARS = 16   # a shorter value typed on a command line is not treated as a secret
# Commands that print the files they are given.
READERS = {"cat", "type", "less", "more", "head", "tail", "bat", "nl", "tac", "grep", "egrep", "fgrep", "rg", "awk",
           "sed", "sort", "strings", "xxd", "od", "base64", "cut", "get-content", "gc"}
GREPS = {"grep", "egrep", "fgrep", "rg"}
NOT_DOTENV = {"example", "sample", "template", "tmpl", "tpl", "dist", "defaults", "default", "schema", "md"}
# Words a shell allows in front of the command it runs.
WRAPPERS = {"sudo", "command", "exec", "time", "nohup", "builtin", "env", "do", "then", "else", "elif", "if",
            "while", "until"}
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)
VAR_REF = re.compile(r"(?<!\\)\$\{?([A-Za-z_][A-Za-z0-9_]*)")

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
    (r"\b(vercel|netlify|fly|railway|wrangler)\b[^;&|]*(\bdeploy\b|(?<!\S)--prod(?:uction)?\b|\bup\b)", "deploy", False),
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

# Our own scripts take commands and SQL as quoted arguments; a segment that RUNS one
# is never classified. A segment that only names one (cat .env ledger.py) is.
OWN_SCRIPTS = re.compile(r"^(irreversible|pr_checkpoint|intake|route|select_skills|resolve_bank|codex_review|verify|"
                         r"ledger|confidence|session_budget|memory_gate|brief|with_secrets)\.py$")
INTERPRETER = re.compile(r"^(python[\d.]*|py|pypy[\d.]*)$")


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
RANK = {"deny": 2, "allow": 1}
TIER_RANK = {"secret": 4, "never": 3, "risky": 2, "mundane": 1, "none": 0}


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
    """The more consequential of two classifications: a denial first, then
    the higher tier, so a merge wrapped in `bash -c` is still logged as one."""
    ra, rb = RANK[a["decision"]], RANK[b["decision"]]
    if ra != rb:
        return a if ra > rb else b
    return a if TIER_RANK.get(a.get("tier"), 0) >= TIER_RANK.get(b.get("tier"), 0) else b


def _strip_comment(seg):
    mask = _quoted(seg)
    for i, ch in enumerate(seg):
        if ch == "#" and mask[i] is None and (i == 0 or seg[i - 1].isspace()):
            return seg[:i]
    return seg


def _tokens(seg):
    """The words a shell would see: quotes removed, redirections as their own tokens."""
    seg = _strip_comment(seg)
    try:
        lex = shlex.shlex(seg, posix=True, punctuation_chars="()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:  # unbalanced quote: fall back to a plain split
        return [t.strip("'\"") for t in seg.split()]


def _base(word):
    b = re.split(r"[\\/]", word)[-1].lower()
    return b[:-4] if b.endswith(".exe") else b


def _command(tokens):
    """(command word, its arguments) after assignments, wrappers and redirections.
    An input redirection keeps its file as an argument (cat < .env reads it)."""
    words, i = [], 0
    while i < len(tokens):
        t = tokens[i]
        if t and not t.strip("<>"):           # a redirection operator
            if words and words[-1].isdigit():
                words.pop()                    # the fd in `2> file`
            if t == "<" and i + 1 < len(tokens):
                words.append(tokens[i + 1])    # `< file`: the command reads it
            i += 2                             # `> file`, `>> file`, `<< DELIM`: not arguments
            continue
        words.append(t)
        i += 1
    i, after_wrapper = 0, False
    while i < len(words):
        w = words[i]
        if ASSIGN.match(w) or w in ("(", ")", "{", "!"):
            i += 1
        elif _base(w) in WRAPPERS:
            i, after_wrapper = i + 1, True
        elif after_wrapper and w.startswith("-"):
            i += 1                             # sudo -E, env -i
        else:
            break
    if i >= len(words):
        # nothing left to run: a bare `env` (or `env > file`) dumps the environment
        return ("env", []) if any(_base(w) == "env" for w in words) else ("", [])
    return _base(words[i]), [w for w in words[i + 1:] if w not in ("(", ")")]


def own_script(tokens):
    """The feature-ladder script a segment RUNS (its file name), else None."""
    cmd, args = _command(tokens)
    if OWN_SCRIPTS.match(cmd):
        return cmd
    rest = [a for a in args if not a.startswith("-")]
    if cmd in ("bash", "sh") and rest and _base(rest[0]) == "py.sh":
        rest = rest[1:]                        # the hook launcher: bash py.sh <script> ...
    elif not INTERPRETER.match(cmd):
        return None
    return _base(rest[0]) if rest and OWN_SCRIPTS.match(_base(rest[0])) else None


def secret_name(name):
    """True for an environment-style name that holds a secret VALUE (X_API_KEY,
    DB_PASSWORD): not a reference to one (TOKEN_FILE, KEYCLOAK_URL) and not a
    lower-case shell variable ($key, $token_count)."""
    if name != name.upper():
        return False
    parts = [p for p in name.split("_") if p]
    if not parts or parts[-1] in NOT_A_VALUE or "PUBLIC" in parts:
        return False
    return any(p in SECRET_WORDS or p.endswith(SECRET_ENDINGS) for p in parts)


def secret_flag(flag):
    """True for a flag whose value is a secret (--token, --api-key, --client-secret);
    --secret NAME, --keyfile PATH and --set-secrets take references, not values."""
    parts = [p for p in flag.strip("-").upper().replace("_", "-").split("-") if p]
    if not parts:
        return False
    if parts[-1] in SECRET_FLAG_LAST or parts[-1].endswith(("APIKEY", "PASSWORD")):
        return True
    return parts[-1] in ("KEY", "SECRET") and len(parts) > 1 and parts[-2] in SECRET_FLAG_PAIR


def _literal(value, min_chars):
    """A value typed out in the command: long enough, and not a path, a URL or
    a reference the shell fills in ($VAR, $(...))."""
    v = value.strip("'\"")
    if len(v) < min_chars or v[0] in "$/~<@." or re.match(r"[A-Za-z]:[\\/]", v) or re.match(r"\w+://", v):
        return False
    return not (("/" in v or "\\" in v) and re.search(r"\.\w{1,5}$", v))


def _dotenv(path):
    base = re.split(r"[\\/]", path)[-1].lower()
    if base == ".env":
        return True
    if base.startswith(".env.") or base.endswith(".env"):
        return not (set(base.split(".")) & NOT_DOTENV)
    return False


def secret_exposure(seg):
    """The label of the secret exposure in one command segment, or None."""
    tokens = _tokens(seg)
    cmd, args = _command(tokens)
    flags = [a for a in args if a.startswith("-")]
    files = [a for a in args if not a.startswith("-")]
    short = "".join(f[1:] for f in flags if not f.startswith("--"))
    if cmd in READERS and any(_dotenv(a) for a in files):
        quiet = cmd in GREPS and (set(short) & set("qclL") or any(
            f in ("--quiet", "--silent", "--count", "--files-with-matches", "--files-without-match") for f in flags))
        in_place = cmd == "sed" and any(f.startswith(("-i", "--in-place")) for f in flags)
        if not quiet and not in_place:
            return "prints a .env file"
    if cmd in ("env", "set") and not args:
        return "dumps the environment"
    if cmd == "printenv":
        if not files:
            return "dumps the environment"
        if any(secret_name(a) for a in files):
            return "prints a secret variable"
    if cmd in ("export", "declare", "typeset") and not files and all(f in ("-p", "-x") for f in flags):
        return "dumps the environment"
    if cmd in ("echo", "printf", "print") or (cmd in READERS and "<<<" in tokens):  # cat <<< "$X_KEY" prints it too
        text = _strip_comment(seg)
        mask = _quoted(text)
        live = "".join(ch if mask[i] != "'" else " " for i, ch in enumerate(text))  # '...' does not expand
        if any(secret_name(m.group(1)) for m in VAR_REF.finditer(live)):
            return "prints a secret variable"
    joined = " ".join([cmd] + args)
    if cmd == "gcloud" and re.search(r"\bsecrets\s+versions\s+access\b", joined):
        return "reads a secret value"
    if cmd == "firebase" and re.search(r"\bfunctions:(secrets:access|config:get)\b", joined):
        return "reads a secret value"
    for i, t in enumerate(tokens):
        m = ASSIGN.match(t)
        if m and secret_name(m.group(1)) and _literal(m.group(2), MIN_SECRET_CHARS):
            return "puts a secret value on the command line"
        if t.startswith("--"):
            flag, eq, value = t.partition("=")
            if not eq and i + 1 < len(tokens) and not tokens[i + 1].startswith("-"):
                value = tokens[i + 1]
            if value and secret_flag(flag) and _literal(value, MIN_SECRET_CHARS):
                return "puts a secret value on the command line"
    return None


def _deny(label):
    return {"tier": "secret", "label": label, "decision": "deny",
            "reason": f"this {label}, which would put it in the transcript. Run the command that needs it "
                      "through with_secrets.py --secret NAME -- <command> instead"}


def classify(cmd, run, _depth=0):
    inner = [m.group(2) for m in SHELL_HEREDOC.finditer(cmd)]
    raw = HEREDOC.sub(r"\1 ", cmd)
    kept = []
    for seg in _segments(raw):
        script = own_script(_tokens(seg))
        if script == "with_secrets.py":
            m = re.search(r"\s--\s(.*)$", seg, re.S)
            if m:
                kept.append(m.group(1))
        elif script is None:
            kept.append(seg)
    for seg in kept:
        label = secret_exposure(seg)
        if label:
            return _deny(label)
    raw = " ; ".join(kept)
    out = _classify_text(executable_text(raw), run)
    if _depth < 4:
        for c in inner + _inner_commands(raw):
            out = _worse(out, classify(c, run, _depth + 1))
    return out


def _classify_text(text, run):
    env = run.get("environments") or {}
    base = run.get("base") or ""
    never = list(NEVER)
    if base and base not in ("main", "master", "production", "release"):
        never.append((r"\bgit\s+push\b[^;&|]*" + BR + re.escape(base) + r"(\s|$)", f"push to the base branch {base}"))
    # A refspec onto main (feature-ladder/x:main) is a push to main whatever the source branch is.
    never.append((r"\bgit\s+push\b[^;&|]*" + BR + r"(main|master)(\s|$)", "push to main/master"))
    for pat, label in never:
        if re.search(pat, text, I):
            # Not a gate: it runs, and the report lists it first with review: pending.
            return {"tier": "never", "label": label, "decision": "allow", "review": "pending",
                    "reason": f"'{label}' runs and is logged for review"}
    hit = next(((label, destroys) for pat, label, destroys in OUTSIDE if re.search(pat, text, I)), None)
    if not hit:
        return {"tier": "none", "decision": "allow"}
    label, destroys = hit
    where = _environment(text, env)
    safe = any(re.search(p, text, I) for p in SAFE_HINTS)
    risky = where == "prod" and destroys and not safe
    return {"label": label, "environment": where, "tier": "risky" if risky else "mundane",
            "decision": "allow", "review": "pending" if risky else "n/a"}


def norm(cmd):
    return " ".join(cmd.split())


def review_status(c):
    return c.get("review") or ("pending" if c.get("tier") in ("risky", "never") else "n/a")


REDACTED = "<redacted>"
_VALUE = r"""("(?:\\.|[^"\\])*"|'[^']*'|[^\s;&|]+)"""
_ASSIGNMENT = re.compile(r"""(?:^|(?<=[\s;&|("']))([A-Za-z_][A-Za-z0-9_]*)=""" + _VALUE)
# The value may not itself be a flag: in `--verbose --token X` the pair is (--token, X).
_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z][\w-]*)(=|\s+)(?!-)" + _VALUE)
_TOKEN_SHAPES = re.compile(
    r"\b(sk-[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|rk_(?:live|test)_[A-Za-z0-9]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|"
    r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|hsk_[A-Za-z0-9_-]{16,}|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})")
_OPAQUE = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z0-9_+=/-]{24,}$|^[0-9a-fA-F]{32,}$")
MIN_REDACT_CHARS = 8


def redact(cmd):
    """The command with every secret-looking value replaced by <redacted>. More
    liberal than the deny rules (any letter case, shorter values): hiding a
    harmless value in the audit log costs nothing, storing a secret does."""
    def assignment(m):
        name, value = m.group(1), m.group(2)
        bare = value.strip("'\"")
        secret = secret_name(name.upper()) and _literal(bare, MIN_REDACT_CHARS)
        return f"{name}={REDACTED}" if secret or _OPAQUE.match(bare) else m.group(0)

    def flag(m):
        name, sep, value = m.groups()
        bare = value.strip("'\"")
        secret = secret_flag(name) and _literal(bare, MIN_REDACT_CHARS)
        return f"{name}{sep}{REDACTED}" if secret or _OPAQUE.match(bare) else m.group(0)

    t = _TOKEN_SHAPES.sub(REDACTED, str(cmd))
    t = re.sub(r"(\w+://[^/\s:@]+):([^@\s/]+)@", lambda m: f"{m.group(1)}:{REDACTED}@", t)     # https://user:pass@host
    t = re.sub(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", lambda m: f"{m.group(1)} {REDACTED}", t)
    t = re.sub(r"(?<!\S)(-u|--user)(=|\s+)([^\s:]+):\S+", lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}:{REDACTED}", t)
    t = re.sub(r"(\bgh\s+secret\s+set\b.*?\s(?:--body|-b)(?:=|\s+))" + _VALUE, lambda m: m.group(1) + REDACTED, t)
    t = _ASSIGNMENT.sub(assignment, t)
    return _FLAG.sub(flag, t)


def record(run, cmd, c, status):
    """Log one outside-repo command. The stored text is redacted first: it is
    copied into FEATURE-REPORT.md and the PR body."""
    run.setdefault("outside_actions", []).append(
        {"at": now(), "cmd": redact(norm(cmd))[:300], "label": c.get("label"), "environment": c.get("environment"),
         "tier": c.get("tier"), "status": status, "review": review_status(c)})


def pending_lines(run):
    """The review: pending rows as markdown bullets (for the report and the PR body).
    pr_checkpoint owns the format (it writes the same list into the PR body) and
    redacts each command with redact() above."""
    return _pending_lines(run)


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ")  # an unescaped pipe would add columns


def report_lines(run):
    """The outside-repo actions as a markdown table ([] when there are none)."""
    rows = run.get("outside_actions", [])
    if not rows:
        return []
    out = ["| When | Action | Where | Status | Review | Command |", "|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {str(r.get('at', ''))[11:16]} | {_cell(r.get('label'))} | {_cell(r.get('environment'))} | "
                   f"{_cell(r.get('status', ''))} | {_cell(r.get('review', 'n/a'))} | "
                   f"`{_cell(redact(r.get('cmd', ''))[:120])}` |")
    return out


def gate(cmd, run):
    """Used by the guard hook. Returns (allowed, reason, run_changed). Only a
    command that would expose a secret value is denied; everything else runs
    and, when it has effects outside the repo, is logged."""
    c = classify(cmd, run)
    if c["decision"] == "allow":
        if c["tier"] == "none":
            return True, "", False
        record(run, cmd, c, "ran" + (" (review: pending)" if review_status(c) == "pending" else ""))
        return True, "", True
    record(run, cmd, c, "blocked: would expose a secret")
    return False, "feature-ladder: " + c["reason"] + ".", True


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    ladder_state.utf8_stdio()
    root = root_dir()
    run, problem = {}, None
    try:
        run = load(root) if root else {}
    except NoRun:
        run = {}                        # no run here: nothing was logged
    except RuntimeError as e:
        problem = str(e)                # run.json exists but cannot be read
    if not isinstance(run, dict):
        run, problem = {}, "run.json does not hold a run (expected a JSON object)"
    if argv[0] == "classify":
        print(json.dumps(classify(" ".join(argv[1:]), run), indent=2))
        return 0
    if problem and argv[0] in ("report", "pending"):
        print(f"irreversible {argv[0]}: {problem}", file=sys.stderr)
        return 1
    if argv[0] == "report":
        lines = report_lines(run)
        print("\n".join(lines) if lines else "No commands with effects outside the repo were run.")
        return 0
    if argv[0] == "pending":
        lines = pending_lines(run)
        print("\n".join(lines) if lines else "Nothing is pending review.")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
