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
          the router picks. Open design decisions the scoper listed (choices a
          worker would otherwise have to guess) are always asked, so the run
          never has to stop for them later. Prints the predicted route,
          the questions (ready for AskUserQuestion: 2-4 options, each with the
          value it maps to) and what was assumed without asking.

  apply   PROPOSAL.json ANSWERS.json [--out FILE]
          Merges the user's answers ({"field": value, ..., "decisions":
          {"<id>": "<chosen option>"}}) into the final, user-confirmed intake,
          routes it, and writes .feature-ladder/intake/<slug>.json (git-ignored).

          The output also carries `codex` ({"review": bool, "why": [...]}: a
          second-model review is recommended for Opus-tier, risk-flagged,
          novel or algorithmic work) and `secrets` (names the feature needs,
          from the scan or the scoper's `secrets` list in ANSWERS; never values).

  card    '<task json>'
          Route one task AND select its skills in one call. The JSON takes
          the router fields plus "id" (task id), "paths" (files in scope),
          "text" (goal and description), optional "add"/"exclude" skill names.
          Prints the route JSON, a blank line, then the short "## Skills for
          this task" block to paste into the Task Card; the skill text itself
          goes to .feature-ladder/cards/<id>-skills.md for the worker to read.

  find    "<request>" [--cwd DIR]
          Prints the path of a confirmed, not-yet-used intake for exactly this
          request text (from /feature-ladder:scope or an aborted run) and marks
          it used; prints nothing if there is none.

Exit 0 on success, 1 on bad input.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import route as router  # noqa: E402

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

# Path patterns that mark an area of the repo (lower-case substring or glob).
AREA_MARKERS = {
    "persistence": ["migration", "alembic", "prisma", "*.sql", "schema.", "models.py",
                    "/models/", "/db/", "database", "repository", "repositories",
                    "*.sqlite", "*.db", "knexfile", "sequelize", "typeorm", "drizzle",
                    "savegame", "save_game", "/storage/"],
    "security": ["auth", "login", "session", "jwt", "oauth", "password", "crypto",
                 "permission", "rbac", "acl", "csrf", "secret", "token", "anticheat",
                 "anti_cheat"],
    "concurrency": ["worker", "queue", "celery", "thread", "/jobs/", "scheduler",
                    "websocket", "async_", "mutex", "lock."],
    "cross_system": ["webhook", "/clients/", "client.", "/integrations/", "integration",
                     "grpc", "*.proto", "api_client", "sdk", "stripe", "discord",
                     "openai", "anthropic"],
    "public_api": ["/routes/", "routes.", "/api/", "/controllers/", "controller.",
                   "/handlers/", "openapi", "swagger", "/cmd/", "cli.", "/endpoints/",
                   "graphql", "schema.graphql"],
}

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
    "irreversible": ("Live changes", "Does any step have effects outside the repo (migrate or delete real data, deploy, publish, call an outside service)? They run under the policy you pick; production data overwrites get a Fable check first."),
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


def repo_root(cwd):
    code, out = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    return (Path(out.strip()) if code == 0 and out.strip() else Path(cwd)).resolve(), code == 0


def list_files(root, is_git, cap=20000):
    if is_git:
        code, out = _run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], str(root))
        if code == 0:
            files = [f for f in out.splitlines() if f and not any(p in SKIP_DIRS for p in f.split("/"))]
            return files[:cap]
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/"))
            if len(files) >= cap:
                return files
    return files


def _match(path, pats):
    p = "/" + path.lower()
    name = p.rsplit("/", 1)[-1]
    for pat in pats:
        if any(ch in pat for ch in "*?["):
            if fnmatch.fnmatchcase(name, pat):
                return pat
        elif pat in p:
            return pat
    return None


def is_test(path):
    parts = path.lower().split("/")
    return any(d in TEST_DIRS for d in parts[:-1]) or any(fnmatch.fnmatchcase(parts[-1], g.lower()) for g in TEST_GLOBS)


def test_command(root):
    pj = root / "package.json"
    if pj.is_file():
        try:
            t = json.loads(pj.read_text(encoding="utf-8")).get("scripts", {}).get("test")
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
        if lw not in STOPWORDS and lw not in out:
            out.append(lw)
    return out[:12]


def candidates(root, files, text, is_git, limit=20):
    terms = terms_of(text)
    score = {}
    for f in files:
        lf = f.lower()
        hits = [t for t in terms if t in lf]
        if hits:
            score[f] = score.get(f, 0) + 2 * len(hits)
    if is_git:
        for t in terms:
            code, out = _run(["git", "grep", "-l", "-i", "-I", "--", t], str(root))
            if code == 0:
                for f in out.splitlines()[:200]:
                    if not any(p in SKIP_DIRS for p in f.split("/")):
                        score[f] = score.get(f, 0) + 1
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
SECRET_FILE_HINTS = ["secretmanager", "secret_manager", "SecretManagerServiceClient", "defineSecret",
                     "accessSecretVersion", "access_secret_version", "projects/", "process.env.", "os.environ", "getenv"]


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
    if is_git and len(names) < 3:
        code, out = _run(["git", "grep", "-l", "-I", "-e", "secretmanager", "-e", "defineSecret", "-e", "accessSecretVersion",
                          "-e", "access_secret_version", "-e", "SecretManagerServiceClient", "--", "."], str(root))
        if code == 0:
            for rel in out.splitlines()[:40]:
                if not any(p in SKIP_DIRS for p in rel.split("/")):
                    harvest(rel)
    uses_gsm = any(where.get(n, "").endswith((".py", ".js", ".ts", ".mjs", ".go", ".java", ".rb")) for n in names) and (
        bool(_run(["git", "grep", "-l", "-I", "-q", "-e", "secretmanager", "-e", "defineSecret", "--", "."], str(root))[0] == 0) if is_git else False)
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
    """Deterministic: a second model's review is automatic only for size L
    (Opus-tier or escalated work). Anything else lists reasons the user might
    want --codex, but does not spend Codex time on its own."""
    size = route.get("size", "S")
    why = []
    if size == "L":
        why.append(f"size L: routed {route.get('agent')} ({route.get('model')}/{route.get('effort')}), score {route.get('score')}")
    hints = []
    if int(fields.get("novelty", 0) or 0) >= 2:
        hints.append("novel to this repo")
    hits = [k for k in ALGORITHMIC if router.keyword_hit(text.lower(), [k])]
    if hits:
        hints.append("algorithmic: " + ", ".join(hits[:4]))
    return {"review": bool(why), "why": why, "consider_codex": hints if not why else []}


def budget_for(route, fields):
    """What a feature of this size may spend. Binding for the coordinator."""
    size = route.get("size", "S")
    if size == "S":
        return {"size": "S", "workers": 1, "design": False, "independent_check": False,
                "codex": False, "adversarial": False, "scribe": False, "escalation": "one band up on failure only",
                "coordinator_turns": 12,
                "shape": "one worker does the whole feature and quotes its own check output; the coordinator writes the card, reads the packet, accepts"}
    if size == "M":
        return {"size": "M", "workers": 2, "design": False, "independent_check": False,
                "codex": False, "adversarial": False, "scribe": False, "escalation": "one band up on failure only",
                "coordinator_turns": 20,
                "shape": "one worker unless the work splits into two independent file sets; no design doc, no separate check-runner, no Codex unless --codex"}
    return {"size": "L", "workers": 4, "design": not fields.get("design_agreed", False), "independent_check": True,
            "codex": True, "adversarial": False, "scribe": True, "escalation": "ladder as written",
            "coordinator_turns": 45,
            "shape": "full protocol: design if not agreed, split into cards, independent check-runner, Codex review once (no retries), one scribe at the end"}


# --------------------------------------------------------------------- scan --
def scan(text, cwd):
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
    fields["ambiguity"] = field(2 if words < 8 else 1, "low", f"request is {words} words; approach not yet checked")
    fields["novelty"] = field(1, "low", "not yet checked against the repo or memory")
    fields["blast_radius"] = field(1, "low", "not yet checked")
    fields["design_agreed"] = field(False, "low", "no accepted design yet")

    # Deployment targets the outside-repo gate needs: Firebase aliases, env names.
    environments = {"aliases": {}, "production": [], "nonproduction": [], "default": None}
    frc = root / ".firebaserc"
    if frc.is_file():
        try:
            projects = json.loads(frc.read_text(encoding="utf-8-sig")).get("projects", {})
            environments["aliases"] = dict(projects)
            environments["default"] = projects.get("default")
            for alias, pid in projects.items():
                a = f"{alias} {pid}".lower()
                if re.search(r"(dev|devel|development|test|testing|staging|stage|qa|sandbox|local|demo)(\b|[-_])", a):
                    environments["nonproduction"].append(pid)
                else:
                    environments["production"].append(pid)
        except (OSError, ValueError):
            pass
    if not environments["production"] and any(f == "firebase.json" or f.startswith("functions/") for f in files):
        environments["production"].append("(firebase project, alias unknown)")

    secrets = discover_secrets(root, files, cands, is_git, environments.get("default"))

    git = {}
    if is_git:
        _, br = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], str(root))
        _, st = _run(["git", "status", "--porcelain"], str(root))
        rc, rem = _run(["git", "remote", "get-url", "origin"], str(root))
        dirty = [l[3:] for l in st.splitlines() if l.strip() and ".feature-ladder/" not in l]
        git = {"branch": br.strip(), "dirty_files": dirty[:20], "dirty_count": len(dirty),
               "remote": rem.strip() if rc == 0 else None}
    return {
        "request": text,
        "slug": slugify(text),
        "git": git,
        "environments": environments,
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
def _values(prop):
    return {k: v.get("value") for k, v in prop["fields"].items()}


def _key(r):
    return (r["agent"], r["model"], r["effort"])


def _label(r):
    return f'{r["agent"]} ({r["model"]}/{r["effort"]})' if r["agent"] != "escalate" else "ESCALATE to coordinator (Fable)"


def _alternatives(name, f):
    v = f.get("value")
    if "range" in f and isinstance(f["range"], list) and f["range"]:
        return [x for x in f["range"] if x != v]
    if name in BOOL_FIELDS or name == "design_agreed":
        return [not bool(v)]
    if name in SCALE_FIELDS:
        return [x for x in (int(v) - 1, int(v) + 1) if 0 <= x <= 3]
    if name in COUNT_BUCKETS:
        reps = [b[0] for b in COUNT_BUCKETS[name]]
        idx = min(range(len(reps)), key=lambda i: abs(reps[i] - int(v)))
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
        reps = [b[0] for b in COUNT_BUCKETS[name]]
        near = min(range(len(reps)), key=lambda i: abs(reps[i] - int(v)))
        opts = [(val, lab, "") for val, lab in COUNT_BUCKETS[name]]
        v = reps[near]
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
    vals = _values(prop)
    base = router.route(vals)
    asks, assumed, why_map = [], [], {}

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
            reason = "uncertain and it changes the model: " + "; ".join(f"{name}={alt} -> {lab}" for alt, lab in flips[:2])
        if reason:
            why_map[name] = reason
            asks.append((1 if flips else 2, name, flips, reason))
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
                    asks.append((1, name, [], "together with other guesses it could change the model"))
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

    # Outside-repo policy: asked once when the feature may touch real data or
    # deployments (a production target is known, or the scout flagged it).
    env = prop.get("environments") or {}
    touches_outside = bool(env.get("production")) or bool((prop["fields"].get("irreversible") or {}).get("value"))
    if touches_outside and mode != "auto":
        prod = ", ".join(str(p) for p in env.get("production", [])[:3]) or "the production target"
        decisions.append({"id": "outside_policy", "header": "Live changes",
                          "question": f"This may change things outside the repo (data, deploys, {prod}). How far may it go on its own?",
                          "options": [{"label": "All of it (Recommended)", "description": "Routine changes run at once; production data overwrites/deletes get one Fable check first; a short never-list is left for you"},
                                      {"label": "Non-production only", "description": "Emulator, dev and staging targets run; anything on production is left for you"},
                                      {"label": "Nothing outside the repo", "description": "Code only; every outside step is listed for you to run"}]})

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
    final, sources = {}, {}
    for name in ALL_FIELDS:
        f = prop["fields"].get(name, {"value": router.route({}).get(name)})
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
    builtin = {"workspace", "outside_policy"}
    scout_ids = {d.get("id") or slugify(d.get("question", "decision")) for d in prop.get("open_decisions", [])}
    answered = [k for k in decisions if k in scout_ids and k not in assumed_decisions]
    unresolved = [k for k in answered if not decisions[k] or str(decisions[k]).lower().startswith("you decide")]
    if (scout_ids and answered and not unresolved and not assumed_decisions
            and sources.get("ambiguity") != "user" and final.get("ambiguity", 0) >= 2):
        final["ambiguity"] = 1
        sources["ambiguity"] = "user (open decisions answered)"
    decisions = {k: v for k, v in decisions.items() if k in scout_ids or k in builtin}
    r = router.route(final)
    secrets = prop.get("secrets") or {}
    needed = answers.get("secrets") if isinstance(answers.get("secrets"), list) else secrets.get("names", [])
    return {
        "request": prop.get("request"),
        "slug": prop.get("slug"),
        "repo_root": prop.get("repo_root"),
        "confirmed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fields": final,
        "sources": sources,
        "codex": codex_recommendation(final, prop.get("request", ""), r),
        "budget": budget_for(r, final),
        "secrets": {"provider": secrets.get("provider"), "project": secrets.get("project"),
                    "names": needed, "template_keys": secrets.get("template_keys", [])},
        "decisions": decisions,
        "assumed_decisions": assumed_decisions,
        "notes": answers.get("notes", []),
        "workspace": ("stash" if str(decisions.get("workspace", "")).lower().startswith("stash")
                      else "stop" if str(decisions.get("workspace", "")).lower().startswith("stop")
                      else "include"),
        "outside_policy": ("nonprod" if str(decisions.get("outside_policy", "")).lower().startswith("non")
                           else "none" if str(decisions.get("outside_policy", "")).lower().startswith("nothing")
                           else "all"),
        "environments": prop.get("environments", {}),
        "test_command": (prop.get("repo") or {}).get("test_command"),
        "candidate_files": prop.get("candidate_files", []),
        "route": r,
    }


def _ensure_ignore(root):
    d = Path(root) / ".feature-ladder"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    changed = False
    for entry in ("intake/", "reviews/", "adversarial/", "checkpoints/", "run.json"):
        if entry not in lines:
            lines.append(entry)
            changed = True
    if changed:
        gi.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------- main --
def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]

    def opt(name, default=None):
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1] if i + 1 < len(rest) else default
            del rest[i:i + 2]
            return val
        return default

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
            if save_it:  # write <root>/.feature-ladder/intake/<slug>-scan.json and print its path
                root = result["repo_root"]
                _ensure_ignore(root)
                out = Path(root) / ".feature-ladder" / "intake" / f"{result['slug']}-scan.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
                result["written_to"] = str(out)
            print(json.dumps(result, indent=2))
            return 0
        if cmd == "card":
            # Route one task and pick its skills in a single call: prints the
            # router result, then the ready-to-paste skills block.
            task = json.loads(" ".join(rest))
            r = router.route(task)
            flags = {k: True for k in ("security", "persistence", "concurrency", "cross_system",
                                       "public_api", "irreversible", "new_dependency") if task.get(k)}
            sel = {"role": r["agent"], "kind": task.get("kind", "code"), "paths": task.get("paths", []),
                   "text": task.get("text", ""), "flags": flags, "add": task.get("add", []),
                   "exclude": task.get("exclude", [])}
            print(json.dumps(r, indent=2))
            print()
            selector = Path(__file__).resolve().parents[2] / "skill-selector" / "scripts" / "select_skills.py"
            root, _ = repo_root(os.getcwd())
            tid = re.sub(r"[^A-Za-z0-9_-]", "-", str(task.get("id") or "task"))
            out = root / ".feature-ladder" / "cards" / f"{tid}-skills.md"
            out.parent.mkdir(parents=True, exist_ok=True)
            res = subprocess.run([sys.executable, str(selector), "--inject", "--out", str(out), json.dumps(sel)],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace")
            sys.stdout.write(res.stdout if res.returncode == 0 else
                             "## Skills for this task\nNone selected (selector error: " + res.stderr.strip()[-200:] + ")\n")
            return 0
        if cmd == "find":
            cwd = opt("--cwd", os.getcwd())
            text = " ".join(rest).strip()
            root, _ = repo_root(cwd)
            d = root / ".feature-ladder" / "intake"
            best = None
            if text and d.is_dir():
                for f in d.glob("*.json"):
                    if f.name.endswith(("-proposal.json", "-answers.json", "-scan.json")):
                        continue
                    try:
                        saved = _load(f)
                    except (OSError, ValueError):
                        continue
                    if " ".join(str(saved.get("request", "")).split()) == " ".join(text.split()) \
                            and not saved.get("used") and saved.get("confirmed_at"):
                        if best is None or saved["confirmed_at"] > best[1].get("confirmed_at", ""):
                            best = (f, saved)
            if best:
                best[1]["used"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                best[0].write_text(json.dumps(best[1], indent=2) + "\n", encoding="utf-8")
                print(str(best[0]))
            return 0
        if cmd == "decide":
            mode = opt("--mode", "default")
            max_q = int(opt("--max", "8"))
            print(json.dumps(decide(_load(rest[0]), mode, max_q), indent=2))
            return 0
        if cmd == "apply":
            out = opt("--out")
            prop = _load(rest[0])
            answers = _load(rest[1]) if len(rest) > 1 else {}
            result = apply(prop, answers)
            if not out:
                root = prop.get("repo_root") or os.getcwd()
                _ensure_ignore(root)
                out = str(Path(root) / ".feature-ladder" / "intake" / f"{prop.get('slug', 'feature')}.json")
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            result["written_to"] = out
            print(json.dumps(result, indent=2))
            return 0
    except (ValueError, KeyError, IndexError, OSError) as e:
        print(f"intake {cmd}: {e}", file=sys.stderr)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
