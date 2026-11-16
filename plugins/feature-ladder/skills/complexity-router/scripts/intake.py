#!/usr/bin/env python3
"""
feature-ladder intake: settle the router variables for a feature BEFORE any
work is delegated, and ask the user only where it matters.

Three deterministic steps (the scoper agent fills the judgment in between):

  scan    "<request>" [--cwd DIR] [--save]
          Read-only repo scan (--save also writes it to
          .feature-ladder/intake/<slug>-scan.json and reports the path).
          Prints a proposal skeleton: every router field
          with a value, a confidence (high/medium/low) and the evidence, plus
          candidate files and the repo's test command. The scoper agent then
          reads the candidate code and corrects the values.

  decide  PROPOSAL.json [--mode default|auto] [--max N]
          Picks the questions to ask. A field is asked when its confidence is
          not high AND flipping it would change the rung (model/effort/agent)
          the router picks or add or remove the design step. Open design decisions the scoper listed (choices a
          worker would otherwise have to guess) are always asked, so the run
          never has to stop for them later. Prints the predicted route,
          the questions (ready for AskUserQuestion: 2-4 options, each with the
          value it maps to) and what was assumed without asking.

  apply   PROPOSAL.json ANSWERS.json [--out FILE]
          Merges the user's answers ({"field": value, ..., "decisions":
          {"<id>": "<chosen option>"}}) into the final, user-confirmed intake,
          routes it, and writes .feature-ladder/intake/<slug>.json (git-ignored).

          The output also carries `budget` (binding: workers, design step,
          coordinator turns; never a review, a scribe or a Fable call),
          `codex` ({"review": false, "at_wrap": [...]}: reviews never run
          during a feature; the hints say why /feature-ladder:wrap --review
          may be worth it at the end of the session), `switches` and
          `secrets` (names the feature needs, never values). A
          `session_agents` decision sets the session's subagent budget.
          An `orchestrator` decision (asked up front, once per session:
          "Opus 5.5 xhigh" or "Fable 5.2") sets the model the coordinator
          runs on; the answers' "orchestrator": "fable" | "opus" (the
          --orchestrator switch) does the same for this run without asking.

  card    '<task json>'
          Route one task AND select its skills in one call. The JSON takes
          the router fields plus "id" (task id), "paths" (files in scope),
          "text" (goal and description), optional "add"/"exclude" skill names.
          Prints the route JSON, a blank line, then the short "## Skills for
          this task" block to paste into the Task Card; the skill text itself
          goes to .feature-ladder/cards/<id>-skills.md for the worker to read.

  find    "<request>" [--cwd DIR] [--mark]
          Prints the path of a confirmed, not-yet-used intake for exactly this
          request text (from /feature-ladder:scope or a run that never
          started); prints nothing if there is none. It only looks: the run
          that builds from the intake uses it up (`pr_checkpoint.py start
          --intake` stamps it), so scoping twice changes nothing. --mark uses
          it up right away, for a flow with no pr_checkpoint step. An intake
          is not returned when its run finished, when it was confirmed on a
          commit that is no longer in HEAD's history, or when it is older
          than MAX_INTAKE_AGE_DAYS.

  mark-used PATH
          Marks a saved intake used, so `find` never returns it again.

Exit 0 on success, 1 on bad input.
"""
import fnmatch
import functools
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route as router  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "orchestration-ladder" / "scripts"))
import ladder_state  # noqa: E402
import session_budget  # noqa: E402


def state_root(cwd=None):
    """Where .feature-ladder/ lives: the main checkout, also from a linked worktree."""
    return ladder_state.state_root(cwd or os.getcwd())


def session_status(repo_root):
    try:
        return session_budget.status(state_root(repo_root))
    except Exception:  # noqa: BLE001 - never fail the intake over this
        return {}


BOOL_FIELDS = ["public_api", "persistence", "security", "concurrency",
               "cross_system", "new_dependency", "no_tests", "irreversible"]
SCALE_FIELDS = ["ambiguity", "novelty", "blast_radius"]
COUNT_FIELDS = ["files", "loc", "modules"]
ALL_FIELDS = ["kind"] + COUNT_FIELDS + BOOL_FIELDS + SCALE_FIELDS + ["design_agreed"]

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "dist",
             "build", ".next", ".nuxt", "target", ".godot", ".import", ".cache",
             ".feature-ladder", ".idea", ".vscode", "vendor", "coverage", ".tox"}

TEST_GLOBS = ["test_*.py", "*_test.py", "*_test.go", "*.test.js", "*.test.ts",
              "*.test.jsx", "*.test.tsx", "*.spec.js", "*.spec.ts", "*.spec.jsx",
              "*.spec.tsx", "*Test.java", "*Tests.cs", "*_spec.rb", "test_*.gd",
              "*_test.rs", "*.test.mjs", "*.spec.mjs"]
TEST_DIRS = {"tests", "test", "__tests__", "spec", "specs"}
# Generated dependency locks: never candidates, never evidence for an area.
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock",
             "poetry.lock", "uv.lock", "pipfile.lock", "pdm.lock", "cargo.lock", "composer.lock", "gemfile.lock",
             "go.sum", "flake.lock", "packages.lock.json", "mix.lock", "pubspec.lock", "podfile.lock"}

# Path patterns that mark an area of the repo. A glob matches the file name. A
# plain marker matches the lower-cased path at a word boundary (camelCase is
# split first, so AuthService.ts reads auth_service.ts): "auth" is auth.py,
# auth/ and auth_service but not author.ts; "lock." is lock.py and file_lock.py
# but not clock.py. Plain inflections (workers, threading) count; a marker in
# MARKER_STEMS matches as a prefix (authenticat -> authentication).
AREA_MARKERS = {
    "persistence": ["migration", "alembic", "prisma", "*.sql", "schema.", "models.py",
                    "/models/", "/db/", "database", "repository", "repositories",
                    "*.sqlite", "*.db", "knexfile", "sequelize", "typeorm", "drizzle",
                    "savegame", "save_game", "/storage/"],
    "security": ["auth", "authn", "authz", "authenticat", "authoriz", "login", "session", "jwt", "oauth",
                 "password", "crypto", "cryptograph", "permission", "rbac", "acl", "csrf", "secret", "token",
                 "anticheat", "anti_cheat"],
    "concurrency": ["worker", "queue", "celery", "thread", "/jobs/", "scheduler",
                    "websocket", "async_", "mutex", "lock."],
    "cross_system": ["webhook", "/clients/", "client.", "/integrations/", "integration",
                     "grpc", "*.proto", "api_client", "sdk", "stripe", "discord",
                     "openai", "anthropic"],
    "public_api": ["/routes/", "routes.", "/api/", "/controllers/", "controller.",
                   "/handlers/", "openapi", "swagger", "/cmd/", "cli.", "/endpoints/",
                   "graphql", "schema.graphql"],
}
MARKER_STEMS = {"authenticat", "authoriz", "cryptograph", "celery", "sequelize"}

STOPWORDS = set("""a an the and or but if then else for to of in on at by with from into
onto over under add adds added adding make makes create new feature support allow allows
let lets use uses using so that this these those it its be is are was were will would
should could can may might must need needs want wants when where which while also just
only more most some any all each every per via not no yes our my your their them they
we you i me us have has had get gets set sets update updates change changes fix fixes
implement implementing build show shows page pages app apps code file files test tests
data user users""".split())

QUESTION_TEXT = {
    "kind": ("Kind", "What kind of work is this?"),
    "files": ("Files", "About how many files will this change?"),
    "loc": ("Size", "Roughly how much code will be written or changed?"),
    "modules": ("Parts", "How many separate parts of the app does it touch?"),
    "public_api": ("Public API", "Does it change something other code or users call (an endpoint, CLI flag, exported function, file or save format)?"),
    "persistence": ("Stored data", "Does it change stored data (database tables, migrations, save files, on-disk formats)?"),
    "security": ("Security", "Does it touch login, permissions, secrets or tokens, anti-cheat, or user input that must be validated?"),
    "concurrency": ("Concurrency", "Does it involve threads, async jobs, queues, locks or websockets?"),
    "cross_system": ("Other systems", "Does it talk to another service, repo or third-party API?"),
    "new_dependency": ("New package", "Will it need a new library or package?"),
    "no_tests": ("Tests", "Is this area untested today (no existing tests to run)?"),
    "irreversible": ("Live changes", "Does the run itself have to do something outside the repo (migrate or delete real data, deploy, publish)? Code that writes to a database is not this; running it against real data is."),
    "ambiguity": ("Approach", "How precisely is the approach specified?"),
    "novelty": ("Novelty", "Has this repo done something like this before?"),
    "blast_radius": ("Blast radius", "If this is wrong, what breaks?"),
    "design_agreed": ("Design", "Should an Opus domain expert write a design for you to approve first, or is your request the design?"),
}

COUNT_BUCKETS = {
    "files": [(1, "1 file"), (3, "2-3 files"), (6, "4-6 files"), (8, "7 or more")],
    "loc": [(30, "Tiny (30 lines or less)"), (120, "Small (31-120 lines)"),
            (300, "Medium (121-300 lines)"), (400, "Large (300+ lines)")],
    "modules": [(1, "One part"), (2, "Two parts"), (3, "Three or more")],
}
SCALE_OPTIONS = {
    "ambiguity": [(0, "Exactly as I described"), (1, "Mostly specified; small gaps are fine"),
                  (2, "Pick an approach for me (design step first)")],
    "novelty": [(0, "Yes - copy an existing pattern"), (1, "Something similar exists"),
                (2, "New to this repo"), (3, "Specialised domain, new to me too")],
    "blast_radius": [(0, "Nothing outside this feature"), (1, "This feature's users"),
                     (2, "A major part of the app"), (3, "The whole app / every user")],
}
KIND_OPTIONS = [("code", "Code"), ("docs", "Docs only"), ("research", "Research / gather sources"),
                ("analysis", "Analysis / design only")]


# ------------------------------------------------------------------ helpers --
def _run(cmd, cwd):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _git(args, cwd):
    """git with paths printed as they are (no C-quoting of non-ASCII names)."""
    return _run(["git", "-c", "core.quotePath=false", *args], str(cwd))


def repo_root(cwd):
    code, out = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    return (Path(out.strip()) if code == 0 and out.strip() else Path(cwd)).resolve(), code == 0


def _skipped(path):
    return any(p in SKIP_DIRS for p in path.split("/"))


def _lockfile(path):
    return path.rsplit("/", 1)[-1].lower() in LOCKFILES


def list_files(root, is_git, cap=20000):
    if is_git:
        code, out = _git(["ls-files", "--cached", "--others", "--exclude-standard", "-z"], root)
        if code == 0:
            files = [f for f in out.split("\0") if f and not _skipped(f)]
            return files[:cap]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
            if len(files) >= cap:
                return files
    return files


@functools.lru_cache(maxsize=None)
def _marker_re(pat):
    lead = r"(?<![a-z0-9])" if pat[0].isalnum() else ""
    tail = "" if pat in MARKER_STEMS or not pat[-1].isalnum() else r"(?:s|es|ed|ing)?(?![a-z])"
    return re.compile(lead + re.escape(pat) + tail)


def _match(path, pats):
    if _lockfile(path):
        return None
    name = path.lower().rsplit("/", 1)[-1]
    words = "/" + re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", path).lower()
    for pat in pats:
        if any(ch in pat for ch in "*?["):
            if fnmatch.fnmatchcase(name, pat):
                return pat
        elif _marker_re(pat).search(words):
            return pat
    return None


def is_test(path):
    parts = path.lower().split("/")
    return any(d in TEST_DIRS for d in parts[:-1]) or any(fnmatch.fnmatchcase(parts[-1], g.lower()) for g in TEST_GLOBS)


def test_command(root):
    pj = root / "package.json"
    if pj.is_file():
        try:
            t = json.loads(pj.read_text(encoding="utf-8-sig")).get("scripts", {}).get("test")
            if t and "no test specified" not in t:
                return "npm test"
        except (OSError, ValueError):
            pass
    if (root / "pytest.ini").is_file() or (root / "conftest.py").is_file() or \
            ((root / "pyproject.toml").is_file() and "pytest" in (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")):
        return "pytest"
    if (root / "go.mod").is_file():
        return "go test ./..."
    if (root / "Cargo.toml").is_file():
        return "cargo test"
    if (root / "addons" / "gut").is_dir():
        return "godot --headless -s addons/gut/gut_cmdln.gd"
    return None


def terms_of(text):
    words = re.findall(r"[A-Za-z_][A-Za-z0-9_\-]{3,}", text)
    out = []
    for w in words:
        lw = w.lower().strip("-_")
        if lw and lw not in STOPWORDS and lw not in out:
            out.append(lw)
    return out[:12]


GREP_FILES_PER_TERM = 200


def _grep_terms(root, terms):
    """{file: set of terms its content mentions}, from ONE `git grep`. -o prints
    each match as file NUL text; a match credits every term it contains, so a
    longer term (retry_count) also counts for the shorter one (retry)."""
    args = ["grep", "-I", "-i", "-F", "-o", "-z", "--no-color"]
    for t in sorted(terms, key=lambda t: (-len(t), t)):
        args += ["-e", t]
    code, out = _git(args, root)
    if code != 0:
        return {}
    found, per_term = {}, dict.fromkeys(terms, 0)
    for line in out.split("\n"):
        f, sep, hit = line.partition("\0")
        if not sep or _skipped(f) or _lockfile(f):
            continue
        hit = hit.lower()
        have = found.setdefault(f, set())
        for t in terms:
            if t in hit and t not in have and per_term[t] < GREP_FILES_PER_TERM:
                have.add(t)
                per_term[t] += 1
    return found


def candidates(root, files, text, is_git, limit=20):
    terms = terms_of(text)
    score = {}
    for f in files:
        if _lockfile(f):
            continue
        lf = f.lower()
        hits = [t for t in terms if t in lf]
        if hits:
            score[f] = score.get(f, 0) + 2 * len(hits)
    if is_git and terms:
        for f, have in _grep_terms(root, terms).items():
            if have:
                score[f] = score.get(f, 0) + len(have)
    ranked = sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))
    return terms, [f for f, _ in ranked[:limit]]


def slugify(text):
    words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOPWORDS][:5]
    return "-".join(words) or "feature"


def field(value, confidence, *evidence):
    return {"value": value, "confidence": confidence, "evidence": [e for e in evidence if e]}


SECRET_NAME_PATTERNS = [
    r"projects/[\w-]+/secrets/([A-Za-z0-9_-]+)",            # resource paths (any Google client)
    r"defineSecret\(\s*['\"]([A-Za-z0-9_-]+)['\"]",         # Firebase functions params
    r"secrets:\s*\[([^\]]+)\]",                              # runWith({ secrets: ["A", "B"] })
    r"--secret[=\s]+['\"]?([A-Za-z0-9_-]+)",                 # gcloud / with_secrets calls
    r"(?:getSecret|get_secret|accessSecret|access_secret|fetchSecret|loadSecret)\w*\(\s*['\"]([A-Za-z0-9_-]+)['\"]",
    r"process\.env\.([A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|DSN|WEBHOOK)[A-Z0-9_]*)",
    r"os\.(?:environ(?:\.get)?|getenv)\(?\[?\s*['\"]([A-Z][A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|DSN|WEBHOOK)[A-Z0-9_]*)['\"]",
]
SECRET_MANAGER_CALLS = ["secretmanager", "defineSecret", "accessSecretVersion", "access_secret_version",
                        "SecretManagerServiceClient"]


def discover_secrets(root, files, cands, is_git, project):
    """Names of secrets the code reads (never values). Candidate files first,
    then .env.example-style templates, then a repo-wide grep capped at 40 files."""
    names, where = {}, {}

    def harvest(rel):
        try:
            txt = (root / rel).read_text(encoding="utf-8", errors="replace")[:200000]
        except OSError:
            return
        for pat in SECRET_NAME_PATTERNS:
            for m in re.finditer(pat, txt):
                raw = m.group(1)
                for n in re.split(r"[,\s]+", raw):
                    n = n.strip("'\" ")
                    if 2 < len(n) <= 64 and n.upper() not in ("NODE_ENV", "PATH", "HOME"):
                        names[n] = names.get(n, 0) + 1
                        where.setdefault(n, rel)

    for rel in cands[:20]:
        harvest(rel)
    templates = [f for f in files if re.search(r"(^|/)\.env(\.example|\.sample|\.template)?$", f) or f.endswith(("secrets.yaml", "secrets.yml"))]
    template_keys = []
    for rel in templates[:5]:
        try:
            for line in (root / rel).read_text(encoding="utf-8", errors="replace").splitlines():
                m = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]+)\s*[=:]", line)
                if m and re.search(r"KEY|SECRET|TOKEN|PASSWORD|DSN|WEBHOOK|CREDENTIAL", m.group(1)):
                    template_keys.append(m.group(1))
        except OSError:
            pass
    gsm_files = []

    def secret_manager_files():
        """Files that call Secret Manager: one grep, at most once per scan."""
        if not gsm_files and is_git:
            args = ["grep", "-l", "-I", "-z"]
            for call in SECRET_MANAGER_CALLS:
                args += ["-e", call]
            code, out = _git(args + ["--", "."], root)
            gsm_files.append([f for f in out.split("\0") if f and not _skipped(f)] if code == 0 else [])
        return gsm_files[0] if gsm_files else []

    if is_git and len(names) < 3:
        for rel in secret_manager_files()[:40]:
            harvest(rel)
    uses_gsm = any(where.get(n, "").endswith((".py", ".js", ".ts", ".mjs", ".go", ".java", ".rb")) for n in names) \
        and bool(secret_manager_files())
    return {
        "provider": "google-secret-manager" if uses_gsm or project else "unknown",
        "project": project,
        "names": sorted(names, key=lambda n: (-names[n], n))[:30],
        "template_keys": sorted(set(template_keys))[:30],
        "where": {n: where[n] for n in list(where)[:30]},
    }


ALGORITHMIC = ["algorithm", "parser", "lexer", "compiler", "interpreter", "scheduler", "solver", "optimizer",
               "state machine", "pathfinding", "a-star", "a*", "dijkstra", "graph", "tree", "trie", "heap",
               "cache eviction", "lru", "rate limit", "token bucket", "consensus", "crdt", "merkle", "hash",
               "crypto", "cipher", "signature", "rng", "random", "seed", "deterministic", "replay", "simulation",
               "physics", "collision", "procedural", "generator", "matching", "ranking", "scoring", "elo",
               "leaderboard", "anti-cheat", "anticheat", "validation", "sync", "conflict", "retry", "backoff",
               "idempot", "transaction", "concurren", "lock", "queue", "stream", "websocket", "protocol", "binary format", "codec"]


def codex_recommendation(fields, text, route):
    """Deterministic. A feature run never reviews: Codex, adversarial tests and
    second opinions are an end-of-session step (/feature-ladder:wrap --review).
    The hints only say why that step may be worth scheduling."""
    hints = []
    if route.get("size") == "L" or route.get("model") == "opus":
        hints.append(f"routed {route.get('agent')} ({route.get('model')}/{route.get('effort')}), score {route.get('score')}")
    if int(fields.get("novelty", 0) or 0) >= 2:
        hints.append("novel to this repo")
    hits = [k for k in ALGORITHMIC if router.keyword_hit(text.lower(), [k])]
    if hits:
        hints.append("algorithmic: " + ", ".join(hits[:4]))
    return {"review": False, "at_wrap": hints}


# Coordinator turns per size. The coordinator's maxTurns (agents/coordinator.md)
# is the hard ceiling over all of them.
COORDINATOR_TURNS = {"S": 12, "M": 20, "L": 24}
WORKERS = {"S": 1, "M": 2, "L": 4}


def budget_for(route, fields=None, agents_left=None):
    """What one feature may spend. Binding for the coordinator; the spawn hook
    (session_budget.py) enforces the agent part. The budget follows the route
    alone; `fields` is accepted for callers that pass it and is not read."""
    size = route.get("size", "S")
    workers = WORKERS[size]
    design = bool(route.get("design_first"))
    notes = []
    if agents_left is not None:
        left = max(0, int(agents_left))
        if design and left < 2:
            design = False
            notes.append("design step dropped: the session budget leaves room for the worker only")
        workers = max(1, min(workers, left - (1 if design else 0))) if left else 0
        if not left:
            notes.append("session subagent budget is spent: nothing can be dispatched")
    shape = {"S": "one worker does the whole feature",
             "M": "one worker; two only if the files form two independent sets",
             "L": f"up to {workers} workers on independent cards"}[size]
    if design:
        shape += ", after one design step (hard flag)"
    return {"size": size, "workers": workers, "design": design, "mechanic_max": 1,
            "independent_check": False, "codex": False, "adversarial": False, "scribe": False,
            "second_opinion": False, "fable": False,
            "escalation": "re-dispatch once, then one band up once; then report partial",
            "coordinator_turns": COORDINATOR_TURNS[size],
            "shape": shape + "; verify.py checks it; no review, no scribe, no Fable call",
            "notes": notes}


# --------------------------------------------------------------------- scan --
NONPROD_TOKENS = {"dev", "devel", "development", "test", "testing", "staging", "stage", "qa", "sandbox", "local", "demo"}
PROD_TOKENS = {"prod", "production", "live"}


def _env_tokens(text):
    """Name tokens split on punctuation, trailing digits dropped (qa2 -> qa)."""
    return {re.sub(r"\d+$", "", t) for t in re.split(r"[^a-z0-9]+", text.lower())} - {""}


def environments(root, files=None):
    """Deployment targets the outside-repo audit needs: Firebase aliases and
    which projects are production. A project is non-production only when its
    alias or id has a non-production TOKEN (myapp-dev, qa2, shop_staging) and
    no production token; 'backstage-prod' and 'latest-app' are production.
    Reads .firebaserc only, so pr_checkpoint.py can call it without a scan."""
    root = Path(root)
    env = {"aliases": {}, "production": [], "nonproduction": [], "default": None}
    frc = root / ".firebaserc"
    if frc.is_file():
        try:
            projects = json.loads(frc.read_text(encoding="utf-8-sig")).get("projects", {})
            env["aliases"] = dict(projects)
            env["default"] = projects.get("default")
            for alias, pid in projects.items():
                toks = _env_tokens(f"{alias} {pid}")
                if toks & NONPROD_TOKENS and not toks & PROD_TOKENS:
                    env["nonproduction"].append(pid)
                else:
                    env["production"].append(pid)
        except (OSError, ValueError, AttributeError, TypeError):
            pass
    if files is None:
        firebase = (root / "firebase.json").is_file() or (root / "functions").is_dir()
    else:
        firebase = any(f == "firebase.json" or f.startswith("functions/") for f in files)
    if not env["production"] and firebase:
        env["production"].append("(firebase project, alias unknown)")
    return env


def _git_state(root):
    """Branch, HEAD and uncommitted paths from ONE `git status` call."""
    code, out = _git(["status", "--porcelain=v2", "--branch", "-z"], root)
    branch, head, dirty = "", None, []
    toks = out.split("\0") if code == 0 else []
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        path = None
        if t.startswith("# branch.oid "):
            head = t[13:] if t[13:] != "(initial)" else None
        elif t.startswith("# branch.head "):
            branch = "HEAD" if t[14:] == "(detached)" else t[14:]
        elif t.startswith("1 "):
            path = t.split(" ", 8)[-1]
        elif t.startswith("2 "):
            path = t.split(" ", 9)[-1]
            i += 1  # a rename or copy is followed by its original path
        elif t.startswith("u "):
            path = t.split(" ", 10)[-1]
        elif t.startswith("? "):
            path = t[2:]
        if path and ".feature-ladder/" not in path:
            dirty.append(path)
    rc, rem = _run(["git", "remote", "get-url", "origin"], str(root))
    return {"branch": branch, "head": head, "dirty_files": dirty[:20], "dirty_count": len(dirty),
            "remote": rem.strip() if rc == 0 else None}


def scan(text, cwd):
    if not Path(cwd).is_dir():
        raise ValueError(f"{cwd} is not a folder")
    root, is_git = repo_root(cwd)
    files = list_files(root, is_git)
    tests = [f for f in files if is_test(f)]
    terms, cands = candidates(root, files, text, is_git)
    tl = text.lower()

    area_repo = {a: [f for f in files if _match(f, pats)] for a, pats in AREA_MARKERS.items()}
    area_cand = {a: [f for f in cands if _match(f, pats)] for a, pats in AREA_MARKERS.items()}

    fields = {}
    docsy = any(w in tl for w in ("readme", "document", "docs", "documentation", "changelog")) and \
        not any(w in tl for w in ("endpoint", "function", "class", "api", "bug", "feature"))
    fields["kind"] = field("docs" if docsy else "code", "medium" if docsy else "high",
                           "request reads as documentation" if docsy else "request asks for a code change")

    n = len(cands)
    est_files = 1 if n <= 1 else 3 if n <= 4 else 6 if n <= 10 else 8
    fields["files"] = field(est_files, "low", f"{n} candidate files matched the request terms")
    fields["loc"] = field(120, "low", "no estimate yet; the scoper sizes it from the code")
    tops = sorted({c.split("/")[0] for c in cands[:8] if "/" in c})
    fields["modules"] = field(max(1, min(3, len(tops))), "low",
                              f"candidate files span top-level folders: {', '.join(tops) or '(repo root)'}")

    for flag in ("public_api", "persistence", "security", "concurrency", "cross_system"):
        kw = router.keyword_hit(tl, router.KEYWORDS.get(flag, []))
        in_c = area_cand.get(flag, [])
        in_r = area_repo.get(flag, [])
        if kw and in_c:
            fields[flag] = field(True, "high", f'request mentions "{kw.strip()}"', f"candidate file {in_c[0]}")
        elif kw or in_c:
            fields[flag] = field(True, "medium", f'request mentions "{kw.strip()}"' if kw else None,
                                 f"candidate file {in_c[0]}" if in_c else None)
        elif not in_r:
            fields[flag] = field(False, "high", f"repo has no {flag.replace('_', ' ')} code (no matching paths)")
        else:
            fields[flag] = field(False, "low", f"repo has {flag.replace('_', ' ')} code (e.g. {in_r[0]}) but nothing ties it to this request yet")

    kw = router.keyword_hit(tl, router.KEYWORDS["new_dependency"])
    fields["new_dependency"] = field(bool(kw), "medium" if kw else "low",
                                     f'request mentions "{kw.strip()}"' if kw else "no dependency mentioned")

    near = [t for t in tests if any(term in t.lower() for term in terms)]
    if not tests:
        fields["no_tests"] = field(True, "high", "repo has no test files")
    elif near:
        fields["no_tests"] = field(False, "high", f"tests exist near this area (e.g. {near[0]})")
    else:
        fields["no_tests"] = field(False, "low", f"repo has {len(tests)} test files, none obviously for this area")

    kw = router.keyword_hit(tl, router.KEYWORDS["irreversible"])
    fields["irreversible"] = field(bool(kw), "medium" if kw else "low",
                                   f'request mentions "{kw}"' if kw else "nothing outside the repo mentioned")

    words = len(text.split())
    # Word count says nothing about ambiguity; the scoper judges it from the code.
    fields["ambiguity"] = field(1, "low", f"request is {words} words; approach not yet checked")
    fields["novelty"] = field(1, "low", "not yet checked against the repo or memory")
    fields["blast_radius"] = field(1, "low", "not yet checked")
    fields["design_agreed"] = field(False, "low", "no accepted design yet")

    # Deployment targets the outside-repo audit needs: Firebase aliases, env names.
    envs = environments(root, files)
    secrets = discover_secrets(root, files, cands, is_git, envs.get("default"))
    git = _git_state(root) if is_git else {}
    return {
        "request": text,
        "slug": slugify(text),
        "git": git,
        "environments": envs,
        "secrets": secrets,
        "repo_root": str(root),
        "is_git": is_git,
        "repo": {"files": len(files), "test_files": len(tests), "test_command": test_command(root),
                 "area_examples": {a: v[:3] for a, v in area_repo.items() if v}},
        "terms": terms,
        "candidate_files": cands,
        "fields": fields,
        "open_decisions": [],
    }


# ------------------------------------------------------------------- decide --
def _normalised(prop):
    """The proposal with every field as {"value", "confidence", "evidence"}.
    The scoper's proposal is model-written: a bare value (`"files": 3`) is
    read as a low-confidence field instead of crashing the intake."""
    if not isinstance(prop, dict) or not isinstance(prop.get("fields"), dict):
        raise ValueError('the proposal needs a "fields" object ({name: {value, confidence, evidence}})')
    fields = {k: (v if isinstance(v, dict) else {"value": v, "confidence": "low", "evidence": []})
              for k, v in prop["fields"].items()}
    return dict(prop, fields=fields)


def _values(prop):
    return {k: v.get("value") for k, v in prop["fields"].items()}


def _key(r):
    """What a question may change: the rung, and whether a design step comes first."""
    return (r["agent"], r["model"], r["effort"], bool(r.get("design_first")))


def _label(r):
    if r["agent"] == "escalate":
        return "ESCALATE to coordinator (Fable)"
    return f'{r["agent"]} ({r["model"]}/{r["effort"]})' + (" after a design step" if r.get("design_first") else "")


def _bucket(name, v):
    """Index of the COUNT_BUCKETS entry the router scores `v` in. The bucket
    values are upper bounds (1, 3, 6, 8 files), so 2 files is the '2-3' bucket."""
    reps = [b[0] for b in COUNT_BUCKETS[name]]
    return next((i for i, r in enumerate(reps) if router._num(v) <= r), len(reps) - 1)


def _alternatives(name, f):
    v = f.get("value")
    if "range" in f and isinstance(f["range"], list) and f["range"]:
        return [x for x in f["range"] if x != v]
    if name in BOOL_FIELDS or name == "design_agreed":
        return [not router._flag(v)]
    if name in SCALE_FIELDS:
        n = router._num(v)
        return [x for x in (n - 1, n + 1) if 0 <= x <= 3]
    if name in COUNT_BUCKETS:
        reps = [b[0] for b in COUNT_BUCKETS[name]]
        idx = _bucket(name, v)
        return [reps[i] for i in (idx - 1, idx + 1) if 0 <= i < len(reps)]
    if name == "kind":
        return [k for k, _ in KIND_OPTIONS if k != v]
    return []


def _options(name, f):
    v = f.get("value")
    ev = "; ".join(f.get("evidence", [])[:2])
    if name in BOOL_FIELDS or name == "design_agreed":
        if name == "design_agreed":
            opts = [(False, "Design first", "An Opus domain expert writes DESIGN.md; you or the coordinator approve it"),
                    (True, "My request is the design", "Build what I described; no design step")]
        else:
            opts = [(True, "Yes", ""), (False, "No", "")]
    elif name in COUNT_BUCKETS:
        opts = [(val, lab, "") for val, lab in COUNT_BUCKETS[name]]
        v = COUNT_BUCKETS[name][_bucket(name, v)][0] if v is not None else None
    elif name in SCALE_OPTIONS:
        opts = [(val, lab, "") for val, lab in SCALE_OPTIONS[name]]
    elif name == "kind":
        opts = [(val, lab, "") for val, lab in KIND_OPTIONS]
    else:
        return []
    out = []
    for val, lab, desc in opts:
        rec = (val == v)
        out.append({"label": lab + (" (Recommended)" if rec else ""), "value": val,
                    "description": (("Scout's estimate: " + ev) if rec and ev else desc)[:200]})
    out.sort(key=lambda o: 0 if o["label"].endswith("(Recommended)") else 1)
    return out[:4]


def decide(prop, mode="default", max_q=8):
    if mode not in ("default", "auto"):
        raise ValueError(f"--mode must be default or auto, not {mode!r}")
    prop = _normalised(prop)
    vals = _values(prop)
    base = router.route(vals)
    asks, assumed = [], []

    for name in ALL_FIELDS:
        f = prop["fields"].get(name)
        if not f:
            continue
        conf = f.get("confidence", "low")
        flips = []
        for alt in _alternatives(name, f):
            r = router.route(dict(vals, **{name: alt}))
            if _key(r) != _key(base):
                flips.append((alt, _label(r)))
        reason = None
        if mode != "auto" and conf != "high" and flips:
            reason = "uncertain and it changes the route: " + "; ".join(f"{name}={alt} -> {lab}" for alt, lab in flips[:2])
        if reason:
            asks.append((1, name, flips, reason))
        else:
            assumed.append({"field": name, "value": f.get("value"), "confidence": conf,
                            "evidence": f.get("evidence", [])[:2],
                            "changes_model": bool(flips)})

    # Joint check: several small uncertainties that together move the rung.
    if mode == "default":
        unsure = [a for a in assumed if a["confidence"] != "high" and a["field"] not in ("kind",)]
        worst = dict(vals)
        base_score = router.score(vals)[0]
        for a in unsure:
            alts = _alternatives(a["field"], prop["fields"][a["field"]])
            if alts:
                best_alt = max(alts, key=lambda x: router.score(dict(vals, **{a["field"]: x}))[0])
                if router.score(dict(vals, **{a["field"]: best_alt}))[0] > base_score:
                    worst[a["field"]] = best_alt  # only alternatives that raise the score
        if unsure and _key(router.route(worst)) != _key(base):
            deltas = []
            for a in unsure:
                alt = worst[a["field"]]
                d = router.score(dict(vals, **{a["field"]: alt}))[0] - router.score(vals)[0]
                deltas.append((d, a["field"]))
            for d, name in sorted(deltas, reverse=True)[:2]:
                if d > 0:
                    asks.append((1, name, [], "together with other guesses it could change the route"))
                    assumed = [a for a in assumed if a["field"] != name]

    # Open design decisions from the scoper: ask them when they would otherwise escalate.
    decisions = []
    if prop.get("open_decisions") and mode != "auto":
        for d in prop["open_decisions"][:4]:
            opts = d.get("options", [])[:4]
            if len(opts) >= 2:
                decisions.append({"id": d.get("id") or slugify(d.get("question", "decision")),
                                  "header": (d.get("header") or "Decision")[:12],
                                  "question": d.get("question"),
                                  "options": [{"label": o if isinstance(o, str) else o.get("label"),
                                               "description": "" if isinstance(o, str) else o.get("description", "")}
                                              for o in opts]})

    git = prop.get("git") or {}
    if git.get("dirty_count") and mode != "auto":
        decisions.insert(0, {"id": "workspace", "header": "Uncommitted",
                             "question": f"You have {git['dirty_count']} uncommitted change(s) on {git.get('branch')}. The ladder works on its own branch. What should happen to them?",
                             "options": [{"label": "Include them (Recommended)", "description": "Committed first, as a separate commit marked as yours"},
                                         {"label": "Stash them", "description": "git stash; you get them back with git stash pop"},
                                         {"label": "Stop", "description": "Do not start; I will commit them myself"}]})

    # Session subagent budget: asked once per coding session, in this round.
    sess = session_status(prop.get("repo_root"))
    if sess.get("budget") == "unset" and mode != "auto":
        rec = base.get("size", "S")
        opts = [{"label": f"{session_budget.SIZE_LABELS[k]}" + (" (Recommended)" if k == rec else ""),
                 "description": {"S": "Enough for one or two small features",
                                 "M": "Several features, or one with a design step",
                                 "L": "A long session of many features"}[k]}
                for k in ("S", "M", "L")]
        opts.sort(key=lambda o: 0 if o["label"].endswith("(Recommended)") else 1)
        decisions.insert(0, {"id": "session_agents", "header": "Subagents",
                             "question": "How many subagents may the ladder spawn in this whole coding session? "
                                         "(The intake scout and the coordinator are not counted; GLM workers are.)",
                             "options": opts})

    # The orchestrator: asked once per coding session, first of all. Fable is
    # only ever used when the user picks it here (or passed --orchestrator).
    if not sess.get("orchestrator", {}).get("chosen") and mode != "auto":
        decisions.insert(0, {"id": "orchestrator", "header": "Orchestrator",
                             "question": "Which model should orchestrate this coding session (run the coordinator)?",
                             "options": [{"label": o["label"], "description": o["description"]}
                                         for o in (session_budget.ORCHESTRATORS[k] for k in ("opus", "fable"))]})

    # No outside-repo policy question since v1.11: every step the feature needs
    # (deploys, data steps, the merge) runs and is logged by irreversible.py
    # with "review: pending" where it touched production; the report lists it.

    asks.sort(key=lambda a: (a[0], ALL_FIELDS.index(a[1])))
    budget = max_q
    budget_left = max(0, budget - len(decisions))
    questions = []
    for _, name, flips, reason in asks[:budget_left]:
        f = prop["fields"][name]
        header, q = QUESTION_TEXT[name]
        questions.append({"field": name, "header": header, "question": f.get("question") or q,
                          "options": _options(name, f), "why": reason})
    for _, name, _, _ in asks[budget_left:]:
        f = prop["fields"][name]
        assumed.append({"field": name, "value": f.get("value"), "confidence": f.get("confidence"),
                        "evidence": f.get("evidence", [])[:2], "changes_model": True,
                        "note": "question budget used up"})

    return {
        "mode": mode,
        "predicted_route": base,
        "questions": questions,
        "decisions": decisions,
        "assumed": assumed,
        "ask_batches": _batches(len(decisions) + len(questions)),
    }


def _batches(n):
    return [min(4, n - i) for i in range(0, n, 4)]


# -------------------------------------------------------------------- apply --
def _coerce(name, v):
    if name in BOOL_FIELDS or name == "design_agreed":
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "y", "1")
        return bool(v)
    if name in SCALE_FIELDS:
        return max(0, min(3, int(v)))
    if name in COUNT_FIELDS:
        return max(1 if name != "loc" else 0, int(v))
    return v


def apply(prop, answers):
    prop = _normalised(prop)
    if not isinstance(answers, dict):
        raise ValueError("the answers must be a JSON object")
    final, sources = {}, {}
    for name in ALL_FIELDS:
        f = prop["fields"].get(name, {})
        if name in answers and answers[name] is not None:
            final[name] = _coerce(name, answers[name])
            sources[name] = "user"
        elif "value" in f and f["value"] is not None:
            final[name] = _coerce(name, f["value"])
            sources[name] = "scout:" + f.get("confidence", "low")
    decisions = dict(answers.get("decisions") or {})
    assumed_decisions = {}
    for d in prop.get("open_decisions", []):
        did = d.get("id") or slugify(d.get("question", "decision"))
        opts = d.get("options") or []
        if did not in decisions and opts:
            first = opts[0] if isinstance(opts[0], str) else opts[0].get("label")
            decisions[did] = first
            assumed_decisions[did] = first
    # Answering every design decision the scout raised removes the ambiguity
    # that would otherwise escalate. Built-in questions (workspace, policy) are
    # not design decisions, and an explicit user answer for ambiguity stands.
    builtin = {"workspace", "outside_policy", "session_agents", "orchestrator"}
    scout_ids = {d.get("id") or slugify(d.get("question", "decision")) for d in prop.get("open_decisions", [])}
    answered = [k for k in decisions if k in scout_ids and k not in assumed_decisions]
    unresolved = [k for k in answered if not decisions[k] or str(decisions[k]).lower().startswith("you decide")]
    if (scout_ids and answered and not unresolved and not assumed_decisions
            and sources.get("ambiguity") != "user" and final.get("ambiguity", 0) >= 2):
        final["ambiguity"] = 1
        sources["ambiguity"] = "user (open decisions answered)"
    decisions = {k: v for k, v in decisions.items() if k in scout_ids or k in builtin}
    r = router.route(final)
    sess_size = str(decisions.get("session_agents", "")).strip()[:1].upper() or None
    if sess_size not in session_budget.SIZES:
        sess_size = None
    agents_left = answers.get("agents_left")
    if agents_left is None and sess_size:
        agents_left = session_budget.SIZES[sess_size]
    if agents_left is None:
        agents_left = session_status(prop.get("repo_root")).get("left")
    switches = sorted({str(x).strip().lstrip("-").replace("-", "_") for x in answers.get("switches") or []}
                      & {"second_opinion", "escalate", "ultra"})
    # The --orchestrator switch wins for this run; then the up-front answer;
    # then what the session already holds; then Opus xhigh. Never Fable unasked.
    orch_pick = answers.get("orchestrator")
    orch_asked = None if orch_pick else decisions.get("orchestrator")
    orch = session_budget.orchestrator(state_root(prop.get("repo_root")), orch_pick or orch_asked)
    orch["asked"] = bool(orch_asked)
    secrets = prop.get("secrets") or {}
    needed = answers.get("secrets") if isinstance(answers.get("secrets"), list) else secrets.get("names", [])
    return {
        "request": prop.get("request"),
        "slug": prop.get("slug"),
        "repo_root": prop.get("repo_root"),
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_head": (prop.get("git") or {}).get("head"),  # `find` rejects it once this left HEAD's history
        "fields": final,
        "sources": sources,
        "codex": codex_recommendation(final, prop.get("request", ""), r),
        "budget": budget_for(r, final, agents_left),
        "session_agents": sess_size,
        "orchestrator": orch,
        "switches": switches,
        "secrets": {"provider": secrets.get("provider"), "project": secrets.get("project"),
                    "names": needed, "template_keys": secrets.get("template_keys", [])},
        "decisions": decisions,
        "assumed_decisions": assumed_decisions,
        "notes": answers.get("notes", []),
        "workspace": ("stash" if str(decisions.get("workspace", "")).lower().startswith("stash")
                      else "stop" if str(decisions.get("workspace", "")).lower().startswith("stop")
                      else "include"),
        # Informational since v1.11: the guard hook is an audit log, not a gate.
        "outside_policy": "all",
        "environments": prop.get("environments", {}),
        "test_command": (prop.get("repo") or {}).get("test_command"),
        "candidate_files": prop.get("candidate_files", []),
        "route": r,
    }


def _ensure_ignore(root):
    """Keep <root>/.feature-ladder/.gitignore listing every runtime file (one shared list)."""
    ladder_state.state_dir(root).mkdir(parents=True, exist_ok=True)
    ladder_state.ensure_gitignore(root)


# ------------------------------------------------------------ saved intakes --
MAX_INTAKE_AGE_DAYS = 7


def _when(stamp):
    try:
        t = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def stale_reason(saved, root, cwd=None):
    """Why a confirmed intake must not be reused, or None when it is fresh."""
    at = _when(saved.get("confirmed_at"))
    if at is None:
        return "no confirmed_at"
    if datetime.now(timezone.utc) - at > timedelta(days=MAX_INTAKE_AGE_DAYS):
        return f"older than {MAX_INTAKE_AGE_DAYS} days"
    run = ladder_state.read_json(ladder_state.state_dir(root) / "run.json")
    if isinstance(run, dict) and not run.get("active") and run.get("slug") == saved.get("slug"):
        done = _when(run.get("finished"))
        if done and done >= at:
            return "the run it was confirmed for finished"
    head = saved.get("git_head")
    if head and _git(["merge-base", "--is-ancestor", str(head), "HEAD"], cwd or root)[0] != 0:
        return "confirmed on a commit that is not in HEAD's history"
    return None


def mark_used(path):
    """Mark a saved intake used so `find` never returns it again. True when the
    file is a saved intake (already marked counts), False when it is missing
    or unreadable."""
    saved = ladder_state.read_json(path)
    if not isinstance(saved, dict):
        return False
    if not saved.get("used"):
        saved["used"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ladder_state.write_json(path, saved)
    return True


def find(text, cwd=None, mark=False):
    """Path of the newest confirmed, unused and still fresh intake for exactly
    this request text, or None. Looking does not use it up; mark=True does."""
    cwd = cwd or os.getcwd()
    root = state_root(cwd)
    d = ladder_state.state_dir(root) / "intake"
    want = " ".join(str(text).split())
    best = None
    if want and d.is_dir():
        for f in sorted(d.glob("*.json")):
            if f.name.endswith(("-proposal.json", "-answers.json", "-scan.json")):
                continue
            saved = ladder_state.read_json(f)
            if not isinstance(saved, dict) or saved.get("used") or not saved.get("confirmed_at"):
                continue
            if " ".join(str(saved.get("request", "")).split()) != want:
                continue
            if best is None or str(saved["confirmed_at"]) > str(best[1]["confirmed_at"]):
                best = (f, saved)
    if best is None or stale_reason(best[1], root, cwd):
        return None
    if mark:
        mark_used(best[0])
    return best[0]


# --------------------------------------------------------------------- main --
FLAG_NAMES = ("security", "persistence", "concurrency", "cross_system", "public_api", "irreversible", "new_dependency")


def skill_flags(task):
    """Router booleans for the skill selector, read the way the router reads
    them ("no" and "false" are false)."""
    return {k: True for k in FLAG_NAMES if router._flag(task.get(k))}


def runner_command(worker_script, card_file, tid, agent):
    """The external-worker command, on the interpreter that is running now
    (`python3` is missing or a Store stub on stock Windows)."""
    return f'"{sys.executable}" "{worker_script}" run --card "{card_file}" --id {tid} --rung {agent}'


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def pop_opt(rest, name, default=None):
    """Remove `name VALUE` from the argument list and return VALUE."""
    if name in rest:
        i = rest.index(name)
        val = rest[i + 1] if i + 1 < len(rest) else default
        del rest[i:i + 2]
        return val
    return default


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    ladder_state.utf8_stdio()
    cmd, rest = argv[0], list(argv[1:])

    def opt(name, default=None):
        return pop_opt(rest, name, default)

    try:
        if cmd == "scan":
            cwd = opt("--cwd", os.getcwd())
            save_it = "--save" in rest
            if save_it:
                rest.remove("--save")
            text = " ".join(rest).strip()
            if not text:
                print("scan needs the feature request text", file=sys.stderr)
                return 1
            result = scan(text, cwd)
            if save_it:  # write <state root>/.feature-ladder/intake/<slug>-scan.json and print its path
                root = state_root(result["repo_root"])
                _ensure_ignore(root)
                out = ladder_state.state_dir(root) / "intake" / f"{result['slug']}-scan.json"
                ladder_state.write_json(out, result)
                result["written_to"] = str(out)
            print(json.dumps(result, indent=2))
            return 0
        if cmd == "card":
            # Route one task and pick its skills in a single call: prints the
            # router result, then the ready-to-paste skills block.
            task = json.loads(" ".join(rest))
            if not isinstance(task, dict):
                raise ValueError("the task must be a JSON object")
            r = router.route(task)
            sel = {"role": r["agent"], "kind": task.get("kind", "code"), "paths": task.get("paths", []),
                   "text": task.get("text", ""), "flags": skill_flags(task), "add": task.get("add", []),
                   "exclude": task.get("exclude", [])}
            root = state_root(os.getcwd())
            tid = re.sub(r"[^A-Za-z0-9_-]", "-", str(task.get("id") or "task"))
            # The GLM tier: T0 and small T2 run on GLM 5.3 Flash through
            # OpenRouter whenever it is set up (workers "auto", the default, or
            # "openrouter"); otherwise on the Claude rung the router named.
            try:
                import external_worker as ew
                cfg = ew.load_config(str(root))
                ready, why = ew.available(cfg) if r.get("provider") == "glm" else (False, "")
                if r.get("provider") == "glm" and cfg.get("workers") in ("auto", "openrouter") and ready:
                    card_file = root / ".feature-ladder" / "cards" / f"{tid}.md"
                    r["runner"] = {"kind": "external", "provider": "openrouter",
                                   "model": ew.model_for(cfg, r["agent"], r.get("effort")),
                                   "card_file": str(card_file),
                                   "command": runner_command(ew.HERE / "external_worker.py", card_file, tid, r["agent"])}
                else:
                    r["runner"] = {"kind": "subagent"}
                    if r.get("provider") == "glm":
                        r["runner"]["note"] = ("GLM tier off: workers=claude" if cfg.get("workers") == "claude"
                                               else f"GLM tier unavailable: {why}")[:160]
            except Exception as e:  # noqa: BLE001 - routing must never fail because of this
                r["runner"] = {"kind": "subagent", "note": f"external worker config ignored: {e}"[:160]}
            print(json.dumps(r, indent=2))
            print()
            selector = Path(__file__).resolve().parents[2] / "skill-selector" / "scripts" / "select_skills.py"
            out = root / ".feature-ladder" / "cards" / f"{tid}-skills.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run([sys.executable, str(selector), "--inject", "--out", str(out), json.dumps(sel)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            sys.stdout.write(res.stdout if res.returncode == 0 else
                             "## Skills for this task\nNone selected (selector error: " + res.stderr.strip()[-200:] + ")\n")
            return 0
        if cmd == "find":
            cwd = opt("--cwd", os.getcwd())
            mark = "--mark" in rest
            if mark:
                rest.remove("--mark")
            hit = find(" ".join(rest).strip(), cwd, mark=mark)
            if hit:
                print(str(hit))
            return 0
        if cmd == "mark-used":
            if not rest:
                raise ValueError("mark-used needs the path of a saved intake")
            if not mark_used(rest[0]):
                raise ValueError(f"{rest[0]} is not a saved intake")
            print(json.dumps({"used": True, "path": rest[0]}))
            return 0
        if cmd == "decide":
            mode = opt("--mode", "default")
            max_q = int(opt("--max", "8"))
            if not rest:
                raise ValueError("decide needs the proposal file")
            print(json.dumps(decide(_load(rest[0]), mode, max_q), indent=2))
            return 0
        if cmd == "apply":
            out = opt("--out")
            if not rest:
                raise ValueError("apply needs the proposal file")
            prop = _load(rest[0])
            answers = _load(rest[1]) if len(rest) > 1 else {}
            result = apply(prop, answers)
            sroot = state_root(prop.get("repo_root"))
            if result["orchestrator"].pop("asked", False):  # the session's answer; a switch is one run only
                session_budget.set_orchestrator(sroot, result["orchestrator"]["key"])
                result["orchestrator"]["source"] = "asked"
            session_budget.set_run_orchestrator(  # the switch, for the hook; cleared when there is none
                sroot, result["orchestrator"]["key"] if result["orchestrator"]["source"] == "switch" else None,
                prop.get("slug"))
            if result.get("session_agents"):
                result["session"] = session_budget.set_size(sroot, result["session_agents"])
            if not out:
                _ensure_ignore(sroot)
                out = str(ladder_state.state_dir(sroot) / "intake" / f"{prop.get('slug', 'feature')}.json")
            ladder_state.write_json(out, result)
            result["written_to"] = out
            print(json.dumps(result, indent=2))
            return 0
    except (ValueError, KeyError, IndexError, OSError, TypeError, AttributeError) as e:
        kind = "" if isinstance(e, (ValueError, OSError)) else f"{type(e).__name__}: "
        print(f"intake {cmd}: {kind}{e}", file=sys.stderr)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
