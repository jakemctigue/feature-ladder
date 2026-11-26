#!/usr/bin/env python3
"""
feature-ladder brainstorm (the --brainstorm switch): settle every application
and architecture detail of the app at the very front, asking as many
questions as it takes (a first round, then follow-up rounds while details
are still open or the answers raised new ones), before the work is narrowed
into features and built. A concluded brainstorm hands its feature queue to
/feature-ladder:feature (`--from-brainstorm`), and its architecture is
stored in Hindsight (`remember`) so later feature runs recall it. Costs no
model tokens itself; the scout (the Sonnet scoper in Claude Code, the
conversation in the Claude app) fills in the judgment between the steps,
exactly as it does for intake.py.

  guide
          Prints the scout's instructions for a brainstorm. Both surfaces read
          them from here, so nothing about the switch is loaded unless it is used.

  scan    "<request>" [--cwd DIR] [--save]
          Read-only repo scan. Prints the app sheet: every detail an app can
          need (application: purpose, users, workflows, data, roles, ...;
          architecture: platform, stack, data store, auth, hosting, ...), each
          already settled where the repo's manifests and files settle it
          ("detected", with the evidence). --save writes it to
          .feature-ladder/brainstorm/<slug>-scan.json.

  decide  SHEET.json [--mode default|auto] [--max N]
          Picks the questions. Unlike intake.py, which asks only what changes
          the model, EVERY applicable detail that is not high confidence is
          asked, application first, in batches of four, with no cap (--max N
          sets one). First come the once-per-session questions (orchestrator,
          subagent budget) and what to build afterwards (every feature, the
          first, or plan only), so nothing is asked again later. `auto` asks
          nothing.

  apply   SHEET.json [ANSWERS.json] [--out FILE]
          Merges {"answers": {"<detail id>": "<chosen label or the user's
          words>"}, "decisions": {"build": ..., "orchestrator": ...,
          "session_agents": ...}} into the confirmed app sheet
          (.feature-ladder/brainstorm/<slug>.json and a readable <slug>.md),
          stores the session answers, and queues the drafted features in
          build order. A feature whose details the user changed is marked
          "revise"; one whose details were all answered "Not needed" is dropped.
          `followup` lists the details still open, and `next_step` says what
          comes next: "revise" (the scout proposes the open and newly needed
          details and rewrites the features) or "build".

  followup APP.json DETAILS.json
          Follow-up round. Merges the scout's {"details": {"<id>": {value,
          confidence, options, ...}}} (proposals for the open details, and any
          new detail the answers called for) into the confirmed sheet and
          prints the questions for this round, decide-style. After
          MAX_ROUNDS follow-up rounds nothing more is asked: proposals are
          taken as they are.

  refine  APP.json [ANSWERS.json]
          Settles the follow-up round's answers ({"answers": {...}}; a detail
          left unanswered keeps the scout's proposal), re-plans the queue and
          prints the sheet with its new `followup` and `next_step`.

  features APP.json FEATURES.json
          Replaces the queue with {"features": [...]} after a revision. Exit 1
          and nothing written when the list is invalid (unknown detail or
          dependency, a cycle, a missing request).

  next    [--sheet APP.json | --app SLUG] [--feature ID] [--build all|first] [--cwd DIR]
          The next feature to build: its request text for one ladder run and
          the `brainstorm` object for that run's intake answers (the short,
          binding app context for its cards). `feature` is null when the
          queue is done, blocked, plan-only, or the session budget is spent.
          This is the hand-off /feature-ladder:feature --from-brainstorm uses:
          --app picks a confirmed sheet by its slug (default: the latest),
          --feature a queued feature by id, --build overrides what the sheet
          builds (a plan-only sheet is built when --build is given).

  remember [--sheet APP.json | --app SLUG] [--no-flush]
          Queues the settled architecture (every architecture detail, the
          data model and permissions, that the user decided or the scout
          proposed; nothing the repo already shows) as one "[decision]
          `arch:<id>` ..." fact per detail in
          .feature-ladder/session-memory-architecture.md, and starts the
          unattended Hindsight write (memory_gate.py flush --background)
          unless --no-flush. These facts are written outside the per-session
          cap and tagged `architecture`; recall.py gives a feature the ones
          that bear on it on its Memory: line.

  done    ID [--status done|partial|skipped|queued] [--note TEXT] [--sheet APP.json]
          Records how a feature ended (`queued` puts it back for a retry).
          Features that depend on one that is not `done` stay blocked.

  context --feature ID [--json] [--sheet APP.json]
          Only the settled details that feature covers, plus the stack, layout
          and tests: what goes on its Task Cards instead of the whole sheet.

  find    "<request>" [--cwd DIR]
          Path of the confirmed app sheet for exactly this request, so a later
          run continues the queue instead of asking again (a plan-only sheet
          becomes "the first feature"). Prints nothing when there is none.

  show    [PATH | "<request>"] [--cwd DIR]
          Prints the readable app sheet.

Exit 0 on success, 1 on bad input.
"""
import fnmatch
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import intake  # noqa: E402

sys.path.insert(0, str(HERE.parents[1] / "orchestration-ladder" / "scripts"))
import session_budget  # noqa: E402

# Every detail the brainstorm settles: (id, group, header <= 12 chars, question).
DIMENSIONS = [
    ("purpose", "application", "Purpose", "What is the app for, and what is the one job it must do well?"),
    ("users", "application", "Users", "Who uses it: which kinds of people, how many, internal or public?"),
    ("workflows", "application", "Workflows", "What are the core things a user does, start to finish?"),
    ("surfaces", "application", "Screens", "Which screens, pages, commands or endpoints does a user touch?"),
    ("entities", "application", "Data model", "What does the app store, and how do those things relate?"),
    ("roles", "application", "Permissions", "Who may see or change what?"),
    ("integrations", "application", "Integrations", "Which outside services does it talk to (APIs, payments, AI models)?"),
    ("notifications", "application", "Notifications", "What does the app tell users, and how (email, push, in-app)?"),
    ("content", "application", "Content", "Languages, accessibility, wording and branding it must follow?"),
    ("edge_cases", "application", "Edge cases", "Which empty, error, offline and limit states must be handled?"),
    ("non_goals", "application", "Not in scope", "What is explicitly out of scope for now?"),
    ("success", "application", "Success", "How will you know it works: the acceptance check or demo you want to see?"),
    ("platform", "architecture", "Platform", "Where does it run (web, mobile, desktop, command line, a service)?"),
    ("stack", "architecture", "Stack", "Which languages, frameworks and runtime versions?"),
    ("frontend", "architecture", "Frontend", "UI framework, state, styling and routing?"),
    ("backend", "architecture", "Backend", "API style and service boundaries (REST, RPC, serverless functions, one service or several)?"),
    ("data_store", "architecture", "Data store", "Which database or storage, who owns the schema, how do migrations run?"),
    ("auth", "architecture", "Sign-in", "How do users sign in, and how are sessions and identities kept?"),
    ("hosting", "architecture", "Hosting", "Where is it hosted, which environments exist, how does a deploy happen?"),
    ("config_secrets", "architecture", "Secrets", "Which settings and secrets does it need, and where do they live?"),
    ("background", "architecture", "Background", "Any jobs, queues, schedules or realtime connections?"),
    ("files_media", "architecture", "Files", "Does it store or process files or media, and where?"),
    ("observability", "architecture", "Monitoring", "Logging, error reporting, metrics and analytics?"),
    ("testing", "architecture", "Testing", "Which tests gate acceptance, and what is the test command?"),
    ("ci_cd", "architecture", "CI", "Which CI runs, and what is the branch and release flow?"),
    ("performance", "architecture", "Scale", "Expected load, data size, response time and cost ceiling?"),
    ("security_privacy", "architecture", "Security", "How sensitive is the data; which validation, rate limits or compliance rules apply?"),
    ("structure", "architecture", "Layout", "Repo layout, module boundaries and conventions to follow?"),
    ("dependencies", "architecture", "Libraries", "Libraries to use or avoid; licence limits?"),
    ("rollout", "architecture", "Rollout", "Migration from what exists today, feature flags, rollback?"),
]
CATALOG = {d[0]: {"group": d[1], "header": d[2], "question": d[3]} for d in DIMENSIONS}
GROUPS = ("application", "architecture")
GROUP_TITLES = {"application": "Application", "architecture": "Architecture"}
# Details that frame the app rather than name something to build: a feature
# list is not warned about for leaving them uncovered.
FRAMING = {"purpose", "users", "non_goals", "success", "platform", "stack", "structure", "dependencies",
           "performance", "testing", "ci_cd", "rollout"}
ALWAYS_ON_CARD = ("stack", "structure", "testing")   # every card gets these, covered or not

# What a repo's manifests and files settle without a model. Each signal:
# (label, dependency names, file patterns). A name ending in "*" is a prefix.
SIGNALS = {
    "platform": [
        ("web app", ["react-dom", "vue", "svelte", "next", "nuxt", "@angular/core", "astro", "solid-js"], []),
        ("mobile app (React Native)", ["react-native", "expo"], []),
        ("mobile app (Flutter)", ["flutter"], []),
        ("desktop app (Electron)", ["electron"], []),
        ("desktop app (Tauri)", ["@tauri-apps/api"], ["tauri.conf.json"]),
        ("game (Godot)", [], ["project.godot"]),
        ("command-line tool", ["click", "typer", "commander", "yargs", "github.com/spf13/cobra", "clap"], []),
        ("serverless functions", ["firebase-functions"], ["serverless.yml", "wrangler.toml"]),
        ("Claude Code plugin", [], [".claude-plugin/plugin.json"]),
    ],
    "stack": [
        ("TypeScript", ["typescript"], ["tsconfig.json"]),
        ("JavaScript (Node)", [], ["package.json"]),
        ("Python", [], ["pyproject.toml", "requirements*.txt", "setup.py"]),
        ("Go", [], ["go.mod"]),
        ("Rust", [], ["Cargo.toml"]),
        ("Dart", [], ["pubspec.yaml"]),
        ("Ruby", [], ["Gemfile"]),
        ("Java or Kotlin", [], ["pom.xml", "build.gradle", "build.gradle.kts"]),
        ("C# (.NET)", [], ["*.csproj"]),
        ("PHP", [], ["composer.json"]),
        ("Swift", [], ["Package.swift"]),
        ("GDScript", [], ["project.godot"]),
    ],
    "frontend": [
        ("React", ["react"], []), ("Next.js", ["next"], []), ("Vue", ["vue"], []), ("Nuxt", ["nuxt"], []),
        ("Svelte", ["svelte", "@sveltejs/kit"], []), ("Angular", ["@angular/core"], []), ("Vite", ["vite"], []),
        ("Tailwind CSS", ["tailwindcss"], []), ("Redux", ["redux", "@reduxjs/toolkit"], []),
        ("Zustand", ["zustand"], []), ("React Router", ["react-router", "react-router-dom"], []),
        ("TanStack Query", ["@tanstack/react-query"], []), ("MUI", ["@mui/material"], []),
    ],
    "backend": [
        ("Express", ["express"], []), ("Fastify", ["fastify"], []), ("NestJS", ["@nestjs/core"], []),
        ("Hono", ["hono"], []), ("FastAPI", ["fastapi"], []), ("Django", ["django"], []), ("Flask", ["flask"], []),
        ("Rails", ["rails"], []), ("Firebase Cloud Functions", ["firebase-functions"], []),
        ("tRPC", ["@trpc/server"], []), ("GraphQL", ["graphql", "@apollo/server", "apollo-server"], ["schema.graphql"]),
        ("Gin", ["github.com/gin-gonic/gin"], []), ("Axum", ["axum"], []), ("Actix", ["actix-web"], []),
        ("Next.js route handlers", [], ["pages/api/*", "app/api/*/route.*"]),
    ],
    "data_store": [
        ("Firebase / Firestore", ["firebase", "firebase-admin", "@google-cloud/firestore", "cloud_firestore"],
         ["firestore.rules", "firestore.indexes.json"]),
        ("PostgreSQL", ["pg", "postgres", "psycopg", "psycopg2", "psycopg2-binary", "asyncpg"], []),
        ("MySQL", ["mysql2", "mysqlclient", "pymysql"], []),
        ("SQLite", ["sqlite3", "better-sqlite3", "aiosqlite"], ["*.sqlite", "*.sqlite3"]),
        ("MongoDB", ["mongodb", "mongoose", "pymongo", "motor"], []),
        ("Redis", ["redis", "ioredis"], []),
        ("Prisma", ["prisma", "@prisma/client"], ["schema.prisma"]),
        ("Drizzle", ["drizzle-orm"], []), ("SQLAlchemy", ["sqlalchemy"], ["alembic.ini"]),
        ("Supabase", ["@supabase/supabase-js", "supabase"], []),
        ("DynamoDB", ["@aws-sdk/client-dynamodb"], []),
    ],
    "auth": [
        ("Auth.js (NextAuth)", ["next-auth", "@auth/core"], []), ("Passport", ["passport"], []),
        ("Clerk", ["@clerk/*"], []), ("Auth0", ["auth0", "@auth0/*"], []),
        ("Supabase Auth", ["@supabase/auth-helpers-*", "@supabase/ssr"], []),
        ("Firebase Auth", ["firebase_auth", "firebaseui", "react-firebase-hooks"], []),
        ("JWT", ["jsonwebtoken", "jose", "pyjwt", "djangorestframework-simplejwt"], []),
        ("django-allauth", ["django-allauth"], []), ("password hashing", ["bcrypt", "bcryptjs", "argon2", "argon2-cffi"], []),
    ],
    "hosting": [
        ("Firebase Hosting", [], ["firebase.json"]), ("Vercel", [], ["vercel.json"]), ("Netlify", [], ["netlify.toml"]),
        ("Docker", [], ["Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml"]),
        ("Fly.io", [], ["fly.toml"]), ("Google App Engine", [], ["app.yaml"]),
        ("Google Cloud Build", [], ["cloudbuild.yaml"]), ("Serverless Framework", [], ["serverless.yml"]),
        ("Cloudflare Workers", [], ["wrangler.toml"]), ("Render", [], ["render.yaml"]), ("Heroku", [], ["Procfile"]),
        ("Kubernetes", [], ["k8s/*", "Chart.yaml"]), ("Terraform", [], ["*.tf"]),
    ],
    "background": [
        ("Celery", ["celery"], []), ("BullMQ", ["bullmq", "bull"], []), ("RQ", ["rq"], []), ("Sidekiq", ["sidekiq"], []),
        ("cron jobs", ["node-cron", "cron", "apscheduler"], []),
        ("Pub/Sub", ["@google-cloud/pubsub", "google-cloud-pubsub"], []), ("Kafka", ["kafkajs", "confluent-kafka"], []),
        ("WebSockets", ["ws", "socket.io", "websockets", "channels"], []),
        ("Cloud Tasks", ["@google-cloud/tasks", "google-cloud-tasks"], []),
        ("Temporal", ["@temporalio/client", "temporalio"], []),
    ],
    "files_media": [
        ("Firebase Storage", ["firebase_storage"], ["storage.rules"]),
        ("Amazon S3", ["@aws-sdk/client-s3", "aws-sdk", "boto3"], []),
        ("Google Cloud Storage", ["@google-cloud/storage", "google-cloud-storage"], []),
        ("Cloudinary", ["cloudinary"], []), ("uploads (multer)", ["multer"], []),
        ("image processing", ["sharp", "pillow"], []), ("ffmpeg", ["fluent-ffmpeg", "ffmpeg-python"], []),
    ],
    "observability": [
        ("Sentry", ["@sentry/*", "sentry-sdk"], []), ("Datadog", ["dd-trace", "datadog"], []),
        ("OpenTelemetry", ["@opentelemetry/*", "opentelemetry-sdk", "opentelemetry-api"], []),
        ("structured logging", ["pino", "winston", "structlog", "loguru"], []),
        ("PostHog", ["posthog-js", "posthog-node", "posthog"], []),
        ("Prometheus", ["prom-client", "prometheus-client"], []),
    ],
    "testing": [
        ("Jest", ["jest"], []), ("Vitest", ["vitest"], []), ("Playwright", ["@playwright/test", "playwright"], []),
        ("Cypress", ["cypress"], []), ("Testing Library", ["@testing-library/*"], []), ("pytest", ["pytest"], []),
        ("Mocha", ["mocha"], []), ("GUT", [], ["addons/gut/*"]),
    ],
    "ci_cd": [
        ("GitHub Actions", [], [".github/workflows/*.yml", ".github/workflows/*.yaml"]),
        ("GitLab CI", [], [".gitlab-ci.yml"]), ("CircleCI", [], [".circleci/config.yml"]),
        ("Jenkins", [], ["Jenkinsfile"]), ("Azure Pipelines", [], ["azure-pipelines.yml"]),
        ("Bitbucket Pipelines", [], ["bitbucket-pipelines.yml"]),
        ("release tooling", ["@changesets/cli", "semantic-release", "release-please"], []),
    ],
    "integrations": [
        ("Stripe", ["stripe", "@stripe/stripe-js"], []), ("OpenAI", ["openai"], []),
        ("Anthropic", ["@anthropic-ai/sdk", "anthropic"], []), ("Twilio", ["twilio"], []),
        ("Slack", ["@slack/web-api", "@slack/bolt", "slack-sdk"], []), ("Discord", ["discord.js", "discord.py"], []),
        ("GitHub API", ["@octokit/*", "octokit", "pygithub"], []),
        ("Google APIs", ["googleapis", "google-api-python-client"], []), ("PayPal", ["@paypal/*"], []),
        ("Algolia", ["algoliasearch"], []), ("maps", ["mapbox-gl", "@googlemaps/*", "leaflet"], []),
        ("Shopify", ["@shopify/*"], []), ("Plaid", ["plaid"], []),
    ],
    "notifications": [
        ("SendGrid", ["@sendgrid/mail", "sendgrid"], []), ("Resend", ["resend"], []),
        ("Nodemailer", ["nodemailer"], []), ("Postmark", ["postmark"], []), ("Mailgun", ["mailgun.js", "mailgun-js"], []),
        ("Firebase Cloud Messaging", ["firebase_messaging"], ["firebase-messaging-sw.js"]),
        ("Expo notifications", ["expo-notifications"], []), ("web push", ["web-push"], []),
        ("OneSignal", ["react-onesignal", "onesignal-node"], []),
    ],
    "content": [
        ("i18next", ["i18next", "react-i18next"], []), ("react-intl", ["react-intl"], []),
        ("vue-i18n", ["vue-i18n"], []), ("next-intl", ["next-intl"], []),
        ("translation files", [], ["locales/*", "i18n/*"]),
        ("accessibility linting", ["eslint-plugin-jsx-a11y", "@axe-core/*"], []),
    ],
    "roles": [
        ("Firestore security rules", [], ["firestore.rules"]), ("CASL", ["@casl/ability"], []),
        ("Casbin", ["casbin"], []), ("django-guardian", ["django-guardian"], []),
    ],
    "security_privacy": [
        ("HTTP hardening (helmet)", ["helmet"], []), ("CORS policy", ["cors", "django-cors-headers"], []),
        ("rate limiting", ["express-rate-limit", "django-ratelimit", "slowapi"], []),
        ("input validation", ["zod", "joi", "yup", "pydantic", "valibot"], []),
    ],
    "rollout": [
        ("feature flags", ["launchdarkly-node-server-sdk", "launchdarkly-js-client-sdk", "unleash-client",
                           "@growthbook/growthbook", "flagsmith"], []),
    ],
}
# Where the scout should read for details no manifest settles: (dimension, file patterns).
POINTERS = {
    "entities": ["schema.prisma", "models.py", "schema.graphql", "firestore.indexes.json", "migrations/*.sql", "*.sql"],
    "surfaces": ["routes/*", "pages/*", "screens/*", "app/*/page.*", "commands/*", "urls.py"],
    "workflows": ["README.md", "README.rst", "docs/*.md"],
    "purpose": ["README.md", "README.rst"],
}
# Languages by file extension, for repos that check in no manifest.
LANGUAGES = {".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
             ".mjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".dart": "Dart", ".rb": "Ruby", ".java": "Java",
             ".kt": "Kotlin", ".cs": "C#", ".php": "PHP", ".swift": "Swift", ".gd": "GDScript", ".sh": "Shell"}
DOC_SUFFIXES = (".md", ".rst", ".txt", ".mdx")
MANIFESTS = ["package.json", "pyproject.toml", "requirements*.txt", "go.mod", "Cargo.toml", "Gemfile", "pubspec.yaml"]
MONOREPO = ["pnpm-workspace.yaml", "turbo.json", "nx.json", "lerna.json"]
NOT_NEEDED = ("not needed", "none", "n/a", "na", "not applicable", "no", "skip")
BUILD_OPTIONS = [
    ("all", "Every feature, in order (Recommended)",
     "One ladder run per feature until the session's subagent budget is spent; the rest stay queued"),
    ("first", "The first feature only", "Builds the foundation feature; the rest stay queued for later runs"),
    ("plan", "Plan only", "Stops after the feature list; nothing is built"),
]
MAX_VALUE = 220      # characters of one detail on a card
MAX_CONTEXT = 1800   # characters of the whole card block
MAX_ROUNDS = 3       # follow-up rounds of questions after the first; then proposals stand
# What `remember` stores in Hindsight as the app's architecture: every
# architecture detail, plus the two application details features must agree on.
ARCH_EXTRA = ("entities", "roles")
ARCH_QUEUE = "session-memory-architecture.md"   # beside session-memory.md (already git-ignored)

GUIDE = """\
BRAINSTORM GUIDE - follow exactly. You settle the whole app before anything is
scoped or built: you estimate, the user confirms. Change no code.
(`python` if `python3` is missing. SCRIPT = {script}
 RECALL = {recall})

1. Scan. From the repo root:
     python3 "SCRIPT" scan --save "<the request>"
   It prints every application and architecture detail an app can need, each
   already settled where the repo's manifests and files settle it
   (status "detected"), and saves it as .feature-ladder/brainstorm/<slug>-scan.json.
   Evidence that starts with "read:" names the files to read for that detail.

2. Recall. What this repository's memory already decided:
     python3 "RECALL" --query "<the request>" --arch-cap 40 --out .feature-ladder/brainstorm/<slug>-recall.json
   (no model; it keeps only the facts that bear on the request, plus every
   architecture decision an earlier brainstorm stored, "`arch:<detail>` ...").
   Each fact on its "memory" line is evidence: a recorded decision, convention
   or interface settles its detail with confidence "high" and evidence
   "memory: [kind] ..." (an `arch:<detail>` fact settles <detail>); a pitfall
   goes into the evidence of the detail it affects. "Memory: none" means there
   is nothing to use; go on.

3. Read. The request first; then README, the manifests, the entry points and
   the "read:" files. At most ~20 files. For a new, empty project read nothing
   and propose.

4. Settle EVERY detail. Write the file the scan named as "sheet_file"
   (.feature-ladder/brainstorm/<slug>-sheet.json, the scan's own slug) as
     {{"request": "<the request>", "dimensions": {{"<id>": {{...}}}}, "features": [...]}}
   with only the details you settled or corrected (a detected detail you agree
   with can be left out; the scan fills it in). Per detail:
   - "value": one or two plain sentences, concrete: names, not adjectives.
   - "confidence": "high" only when the request states it or you saw it in the
     code; "medium" when it follows from the repo's conventions; "low" when it
     is your proposal. The user is asked about everything that is not high.
   - "evidence": ["file:line", "the words of the request", ...].
   - "options": for every detail that is not high, 2-4 real alternatives for
     THIS app, {{"label": "<= 60 chars", "description": "what it means or costs"}},
     your recommendation first. No filler options.
   - "status": "not_applicable" plus one line of evidence when the app has no
     such thing (no notifications, no files). Do not invent needs.
   The sheet's details are a floor, not a ceiling: add EVERY detail this app
   needs that the sheet lacks (a payment flow, offline sync, an import
   format, a pricing rule...), each under a new id with "group"
   ("application" or "architecture"), "header" (<= 12 chars) and "question".
   There is no limit on questions: a detail you are not sure of is asked,
   not guessed. Ask nothing about how to build a feature (that is the
   feature run's job), only what the app is and how it is put together.

5. Draft "features": the app split into the smallest features that can each be
   built and tested on their own, in build order, foundation first. Each:
     {{"id": "kebab-id", "title": "...", "request": "<the whole request one
       ladder run gets: what to build, where, and what done looks like>",
       "depends_on": ["id", ...], "covers": ["<detail ids it implements>"]}}
   One concern per feature: a request that needs "and" twice is two features.
   Every detail that needs code is covered by at least one feature.

6. Run:
     python3 "SCRIPT" decide <sheet_file> --mode <the Decide mode from your prompt: default | auto>

7. Return the sheet path, then the decide output verbatim, then
   `Retained: none - brainstorm only`.

REVISE (only when asked to, after `apply` or `refine`; skip steps 1-7). Read
the confirmed app sheet (.json) named in your prompt.
 a. Follow-up details, when its "followup" list is not empty or the user's
    answers call for a detail the sheet does not have yet (they picked
    Stripe: how refunds and webhooks work; they want teams: how invites
    work). For each, propose "value", "confidence", "evidence" and 2-4
    "options" exactly as in step 4 (new ones also "group", "header",
    "question"). Save {{"details": {{"<id>": {{...}}}}}} as
    <slug>-followup.json beside the sheet and run
     python3 "SCRIPT" followup <the confirmed app .json> <that file>
 b. Features, when any is marked "revise": true, "feature_errors" is not
    empty, or a detail settled since has no feature: rewrite the "request",
    "covers" and "depends_on" of those features to match the confirmed
    details (the user changed the ones listed under "changed"; take your own
    recommendation for any follow-up detail still being asked), add a feature
    for an uncovered detail, and keep every other feature as it is. Save the
    whole list as {{"features": [...]}} in <slug>-features.json and run
     python3 "SCRIPT" features <the confirmed app .json> <that file>
Return each output verbatim (followup first), then
`Retained: none - brainstorm only`.
"""


# ------------------------------------------------------------------ helpers --
def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(text):
    return " ".join(str(text or "").split())


def _clip(text, n):
    text = norm(text)
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _strip_rec(label):
    return re.sub(r"\s*\(recommended\)\s*$", "", str(label or ""), flags=re.I).strip()


def _same(a, b):
    return bool(norm(a)) and norm(a).lower().rstrip(".") == norm(b).lower().rstrip(".")


def _load(path):
    with open(path, encoding="utf-8-sig") as fh:
        return json.load(fh)


def _dump(path, data):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _dir(root):
    return Path(root) / ".feature-ladder" / "brainstorm"


def _file_hit(path, pat):
    p = path.lower()
    pat = pat.lower()
    if "/" not in pat:
        return fnmatch.fnmatchcase(p.rsplit("/", 1)[-1], pat)
    return fnmatch.fnmatchcase(p, pat) or fnmatch.fnmatchcase(p, "*/" + pat)


def _dep_hit(deps, name):
    name = name.lower()
    if name.endswith("*"):
        return next((d for d in deps if d.startswith(name[:-1])), None)
    return name if name in deps else None


def _read(root, rel, cap=400000):
    try:
        return (Path(root) / rel).read_text(encoding="utf-8-sig", errors="replace")[:cap]
    except OSError:
        return ""


def dependencies(root, files):
    """{dependency name (lower case): manifest it was found in}. Names only,
    from the manifests a repo checks in; at most 30 manifests are read."""
    deps = {}

    def add(name, rel):
        name = str(name or "").strip().lower()
        if name and name not in deps:
            deps[name] = rel

    manifests = [f for f in files if any(_file_hit(f, m) for m in MANIFESTS)]
    manifests.sort(key=lambda f: (f.count("/"), f))
    for rel in manifests[:30]:
        base, text = rel.rsplit("/", 1)[-1].lower(), _read(root, rel)
        if base == "package.json":
            try:
                pkg = json.loads(text)
            except ValueError:
                continue
            for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for name in (pkg.get(key) or {}) if isinstance(pkg.get(key), dict) else []:
                    add(name, rel)
        elif base.startswith("requirements"):
            for line in text.splitlines():
                m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                if m and not line.lstrip().startswith(("#", "-")):
                    add(m.group(1), rel)
        elif base in ("pyproject.toml", "cargo.toml"):
            # Quoted requirement strings ("fastapi>=0.110") and `name = ...` keys of
            # the dependency tables: enough to name the libraries, no TOML parser needed.
            table = ""
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("["):
                    table = s.strip("[]").lower()
                    continue
                in_deps = "dependencies" in table or "dependency-groups" in table or "requires" in table
                if in_deps or re.match(r"(dependencies|requires)\s*=", s) or table == "project":
                    for q in re.findall(r"[\"']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:[<>=!~\[;@ ][^\"']*)?[\"']", s):
                        if table != "project" or re.match(r"(dependencies|requires)\s*=", s) or s.startswith(("\"", "'")):
                            add(q, rel)
                    m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*=", s)
                    if m and in_deps:
                        add(m.group(1), rel)
        elif base == "go.mod":
            for m in re.finditer(r"^\s*(?:require\s+)?([a-z0-9.-]+\.[a-z]+/[^\s]+)\s+v", text, re.M):
                add(m.group(1), rel)
        elif base == "gemfile":
            for m in re.finditer(r"^\s*gem\s+[\"']([^\"']+)[\"']", text, re.M):
                add(m.group(1), rel)
        elif base == "pubspec.yaml":
            section = False
            for line in text.splitlines():
                if re.match(r"(dev_)?dependencies:\s*$", line):
                    section = True
                elif line and not line[0].isspace():
                    section = False
                elif section:
                    m = re.match(r"\s{2}([A-Za-z0-9_]+):", line)
                    if m:
                        add(m.group(1), rel)
    return deps


def _dim(dim_id, **over):
    d = dict(CATALOG.get(dim_id, {}), value=None, confidence="low", evidence=[], status="open", options=[])
    d.update(over)
    return d


# --------------------------------------------------------------------- scan --
def scan(text, cwd):
    root, is_git = intake.repo_root(cwd)
    files = intake.list_files(root, is_git)
    deps = dependencies(root, files)
    tests = [f for f in files if intake.is_test(f)]
    dims = {dim_id: _dim(dim_id) for dim_id, *_ in DIMENSIONS}

    for dim_id, signals in SIGNALS.items():
        labels, evidence = [], []
        for label, names, pats in signals:
            hit = next((h for h in (_dep_hit(deps, n) for n in names) if h), None)
            where = f"{deps[hit]}: {hit}" if hit else next((f for f in files for p in pats if _file_hit(f, p)), None)
            if where:
                labels.append(label)
                evidence.append(where)
        if labels:
            dims[dim_id].update(value=", ".join(labels), confidence="high", evidence=evidence[:6], status="detected")

    # Languages actually written here (three files or more), so a repo without a
    # manifest still has a stack.
    by_lang = {}
    for f in files:
        lang = LANGUAGES.get(os.path.splitext(f)[1].lower())
        if lang:
            by_lang[lang] = by_lang.get(lang, 0) + 1
    st = dims["stack"]
    known = [v.strip() for v in (st["value"] or "").split(",") if v.strip()]
    extra = [(n, lang) for lang, n in by_lang.items() if n >= 3 and not any(k.startswith(lang) for k in known)]
    if extra:
        extra.sort(key=lambda x: (-x[0], x[1]))
        st.update(value=", ".join(known + [lang for _, lang in extra[:4]]), confidence="high", status="detected",
                  evidence=(st["evidence"] + [f"{n} {lang} files" for n, lang in extra[:4]])[:6])

    test_cmd = intake.test_command(root)
    if tests or test_cmd:
        t = dims["testing"]
        parts = ([t["value"]] if t["value"] else []) + ([f"`{test_cmd}`"] if test_cmd else []) + [f"{len(tests)} test files"]
        named = [f for f in tests if any(fnmatch.fnmatchcase(f.rsplit("/", 1)[-1].lower(), g.lower())
                                         for g in intake.TEST_GLOBS)]
        t.update(value=", ".join(parts), confidence="high", status="detected",
                 evidence=(t["evidence"] + [f"e.g. {(named or tests)[0]}"] if tests else t["evidence"])[:6])

    tops = sorted({f.split("/")[0] for f in files if "/" in f and not f.startswith(".")})
    mono = [f for f in files if any(_file_hit(f, m) for m in MONOREPO) and "/" not in f]
    if tops:
        dims["structure"].update(
            value=("monorepo (" + ", ".join(mono) + "); " if mono else "") + "top-level folders: " + ", ".join(tops[:10]),
            confidence="high", status="detected", evidence=[f"{len(files)} files in {len(tops)} top-level folders"])

    aliases = {}
    frc = _read(root, ".firebaserc")
    if frc:
        try:
            aliases = dict(json.loads(frc).get("projects", {}))
        except ValueError:
            pass
    if aliases and dims["hosting"]["value"]:
        dims["hosting"]["value"] += "; environments: " + ", ".join(f"{a} = {p}" for a, p in sorted(aliases.items()))
        dims["hosting"]["evidence"].append(".firebaserc")

    # Secret and setting names (never values): those read by code, and the
    # keys of a checked-in .env template. A name that only a document mentions
    # is not evidence.
    secrets = intake.discover_secrets(root, files, [], is_git, aliases.get("default"))
    where = secrets.get("where") or {}
    names = [n for n in secrets.get("names") or [] if where.get(n) and not where[n].lower().endswith(DOC_SUFFIXES)]
    names += [k for k in secrets.get("template_keys") or [] if k not in names]
    if names:
        dims["config_secrets"].update(
            value="secret and setting names: " + ", ".join(names[:12]), confidence="high", status="detected",
            evidence=([f"provider: {secrets['provider']}"] if secrets.get("provider") not in (None, "unknown") else [])
            + sorted({where[n] for n in names if n in where})[:3])

    for dim_id, pats in POINTERS.items():
        if dims[dim_id]["status"] == "open":
            found = [f for p in pats for f in files if _file_hit(f, p)]
            if found:
                dims[dim_id]["evidence"] = ["read: " + ", ".join(sorted(dict.fromkeys(found))[:5])]

    manifests = [f for f in files if any(_file_hit(f, m) for m in MANIFESTS)]
    return {
        "request": text,
        "slug": intake.slugify(text),
        "repo_root": str(root),
        "is_git": is_git,
        "greenfield": not manifests and len(files) < 10,
        "repo": {"files": len(files), "test_files": len(tests), "test_command": test_cmd,
                 "manifests": manifests[:12], "top_level": tops[:12]},
        "dimensions": dims,
        "features": [],
    }


# ------------------------------------------------------------ the scout's sheet --
def normalise(sheet, base=None):
    """The scout writes only what it settled or corrected. Fill the rest from
    the scan (`base`) and the catalog, and give every detail a status:
    not_applicable | open | detected (the repo settles it) | proposed."""
    base = base or {}
    out = dict(base, **{k: v for k, v in sheet.items() if k != "dimensions"})
    base_dims = base.get("dimensions") or {}
    mine = sheet.get("dimensions") or {}
    dims = {}
    for dim_id in list(dict.fromkeys([d[0] for d in DIMENSIONS] + list(base_dims) + list(mine))):
        b, s = base_dims.get(dim_id) or {}, mine.get(dim_id) or {}
        if not isinstance(s, dict):
            s = {"value": s}
        d = _dim(dim_id)
        d.update(b)
        d.update(s)
        if d.get("group") not in GROUPS:
            d["group"] = "application"
        d["header"] = str(d.get("header") or dim_id.replace("_", " ").title())[:14]
        d["question"] = d.get("question") or f"{d['header']}?"
        d["evidence"] = [str(e) for e in (d.get("evidence") if isinstance(d.get("evidence"), list)
                                           else [d.get("evidence")]) if e]
        d["options"] = [o for o in (d.get("options") or []) if (o if isinstance(o, str) else (o or {}).get("label"))]
        value = norm(d.get("value")) if d.get("value") not in (None, "", [], {}) else None
        if isinstance(d.get("value"), list):
            value = ", ".join(norm(v) for v in d["value"] if v) or None
        d["value"] = value
        if str(s.get("status") or "").lower() in ("not_applicable", "n/a", "na"):
            d["status"] = "not_applicable"
        elif not value:
            d["status"] = "open"
        elif b.get("status") == "detected" and ("value" not in s or _same(s.get("value"), b.get("value"))):
            d["status"] = "detected"
        else:
            d["status"] = "proposed"
        if d.get("confidence") not in ("high", "medium", "low"):
            d["confidence"] = "high" if d["status"] == "detected" else "low"
        dims[dim_id] = d
    out["dimensions"] = dims
    out["features"] = sheet.get("features") or base.get("features") or []
    out["request"] = out.get("request") or ""
    out["slug"] = out.get("slug") or intake.slugify(out["request"])
    return out


def load_sheet(path):
    """A scout's sheet, completed from the scan saved beside it (same slug)."""
    path = Path(path)
    sheet = _load(path)
    names = [path.name[:-len("-sheet.json")]] if path.name.endswith("-sheet.json") else []
    names.append(intake.slugify(str(sheet.get("request") or "")))   # a sheet saved under another name
    for name in names:
        scan_file = path.with_name(name + "-scan.json")
        if scan_file.is_file():
            return normalise(sheet, _load(scan_file))
    return normalise(sheet)


def _ordered(dims):
    order = {d[0]: i for i, d in enumerate(DIMENSIONS)}
    ids = sorted(dims, key=lambda k: (GROUPS.index(dims[k]["group"]), order.get(k, len(order)), k))
    return [(k, dims[k]) for k in ids]


def _options(d):
    """2-4 options for AskUserQuestion, the recommendation first."""
    opts = []
    for o in d["options"][:4]:
        label, desc = (o, "") if isinstance(o, str) else (o.get("label"), o.get("description", ""))
        opts.append({"label": _clip(_strip_rec(label), 80), "description": _clip(desc, 200)})
    if not opts and d["value"]:
        opts.append({"label": _clip(d["value"], 80), "description": _clip("; ".join(d["evidence"][:2]), 200)})
    if len(opts) == 1:
        opts.append({"label": "Not needed", "description": "Leave this out of the app"})
    if opts:
        opts[0]["label"] += " (Recommended)"
    return opts


# ------------------------------------------------------------------- decide --
def decide(sheet, mode="default", max_q=None):
    dims = sheet["dimensions"]
    questions, open_text, assumed, not_applicable = [], [], [], []
    for dim_id, d in _ordered(dims):
        row = {"detail": dim_id, "group": d["group"], "header": d["header"]}
        if d["status"] == "not_applicable":
            not_applicable.append(dict(row, why="; ".join(d["evidence"][:1])))
        elif (d["confidence"] == "high" and d["value"]) or mode == "auto" or \
                (max_q is not None and len(questions) + len(open_text) >= max_q):
            entry = dict(row, value=d["value"], status=d["status"], confidence=d["confidence"], evidence=d["evidence"][:2])
            if mode != "auto" and not (d["confidence"] == "high" and d["value"]):
                entry["note"] = "question budget used up"
            assumed.append(entry)
        else:
            opts = _options(d)
            q = dict(row, header=d["header"][:12], question=d["question"])
            if opts:
                questions.append(dict(q, options=opts))
            else:
                open_text.append(q)   # nothing proposed: asked in the user's own words

    decisions = []
    if mode != "auto":
        decisions = intake.session_decisions(sheet.get("repo_root"), "M", mode)
        decisions.append({"id": "build", "header": "Build",
                          "question": "Once the details are settled and the app is split into features, what should be built?",
                          "options": [{"label": label, "description": desc} for _, label, desc in BUILD_OPTIONS]})
    return {
        "mode": mode,
        "request": sheet.get("request"),
        "counts": {"details": len(dims), "asked": len(questions) + len(open_text),
                   "settled": len(assumed), "not_applicable": len(not_applicable),
                   "features_drafted": len(sheet.get("features") or [])},
        "decisions": decisions,
        "questions": questions,
        "open_text": open_text,
        "assumed": assumed,
        "not_applicable": not_applicable,
        "ask_batches": intake._batches(len(decisions) + len(questions)),
    }


# ----------------------------------------------------------------- features --
def plan_features(dims, features):
    """Validate the feature list against the settled details and put it in
    build order. Returns (queue, errors, notes). Deterministic."""
    errors, notes, items, seen = [], [], [], set()
    if not isinstance(features, list) or not features:
        return [], ["no features: the app must be split into at least one feature"], []
    for i, f in enumerate(features):
        if not isinstance(f, dict):
            errors.append(f"feature {i + 1}: not an object")
            continue
        fid = re.sub(r"[^a-z0-9]+", "-", str(f.get("id") or f.get("title") or "").lower()).strip("-")
        if not fid:
            errors.append(f"feature {i + 1}: needs an id or a title")
            continue
        if fid in seen:
            errors.append(f"feature {fid}: id used twice")
            continue
        seen.add(fid)
        request = norm(f.get("request"))
        if len(request.split()) < 4:
            errors.append(f"feature {fid}: needs a request of at least four words (the text one ladder run gets)")
        covers = [str(c) for c in f.get("covers") or []]
        unknown = [c for c in covers if c not in dims]
        if unknown:
            errors.append(f"feature {fid}: covers unknown detail(s) {', '.join(unknown)}")
        items.append({"id": fid, "title": norm(f.get("title")) or fid, "request": request,
                      "depends_on": [re.sub(r"[^a-z0-9]+", "-", str(d).lower()).strip("-") for d in f.get("depends_on") or []],
                      "covers": covers, "status": f.get("status") if f.get("status") in STATUSES else "queued",
                      **({"note": f["note"]} if f.get("note") else {})})
    ids = {f["id"] for f in items}
    for f in items:
        bad = [d for d in f["depends_on"] if d not in ids or d == f["id"]]
        if bad:
            errors.append(f"feature {f['id']}: depends on unknown feature(s) {', '.join(bad)}")
    if errors:
        return [], errors, notes

    # A feature every one of whose details the user answered "Not needed" is not built.
    dropped = set()
    for f in items:
        if f["covers"] and all(dims[c]["status"] == "not_applicable" and dims[c].get("source") == "user"
                               for c in f["covers"]):
            f.update(status="dropped", note="every detail it covers was answered Not needed")
            dropped.add(f["id"])
            notes.append(f"dropped {f['id']}: {f['note']}")
    for f in items:
        gone = [d for d in f["depends_on"] if d in dropped]
        if gone and f["id"] not in dropped:
            f["depends_on"] = [d for d in f["depends_on"] if d not in dropped]
            notes.append(f"{f['id']} no longer depends on dropped {', '.join(gone)}")
        changed = [c for c in f["covers"] if dims[c].get("changed")]
        if changed and f["status"] == "queued":
            f.update(revise=True, changed=changed)

    # Build order: dependencies first, otherwise the order drafted (stable).
    order, placed, pending = [], set(), list(items)
    while pending:
        ready = [f for f in pending if all(d in placed for d in f["depends_on"])]
        if not ready:
            return [], ["dependency cycle between: " + ", ".join(f["id"] for f in pending)], notes
        order.append(ready[0])
        placed.add(ready[0]["id"])
        pending.remove(ready[0])

    covered = {c for f in order if f["status"] != "dropped" for c in f["covers"]}
    uncovered = [k for k, d in _ordered(dims) if k not in covered and k not in FRAMING
                 and d["status"] in ("decided", "assumed", "proposed") and d.get("value")]
    if uncovered:
        notes.append("settled but in no feature: " + ", ".join(uncovered))
    return order, [], notes


STATUSES = ("queued", "running", "done", "partial", "skipped", "dropped")


# -------------------------------------------------------------------- apply --
def build_mode(label):
    """The `build` decision's label (or all | first | plan) -> all | first | plan."""
    v = _strip_rec(label).lower()
    if v in ("first", "plan"):
        return v
    if v.startswith(("the first", "first")):
        return "first"
    if v.startswith("plan"):
        return "plan"
    return "all"


def apply(sheet, answers, mode="default"):
    answers = answers or {}
    given = answers.get("answers") or {}
    unknown = [k for k in given if k not in sheet["dimensions"]]
    if unknown:
        raise ValueError("answers name unknown detail(s): " + ", ".join(unknown))
    dims = {dim_id: resolve(d, given.get(dim_id)) for dim_id, d in _ordered(sheet["dimensions"])}
    decisions = dict(answers.get("decisions") or {})
    features, errors, notes = plan_features(dims, sheet.get("features") or [])
    app = {
        "request": sheet.get("request"),
        "slug": sheet.get("slug"),
        "repo_root": sheet.get("repo_root"),
        "confirmed_at": now(),
        "mode": mode,
        "build": build_mode(decisions.get("build")),
        "decisions": {k: v for k, v in decisions.items() if k in ("build", "orchestrator", "session_agents")},
        "dimensions": dims,
        "features": features,
        "feature_errors": errors,
        "notes": notes + [str(n) for n in answers.get("notes") or []],
        "round": 0,
    }
    return progress(app)


def resolve(d, answer):
    """One detail after the user's answer (None: not asked, or skipped)."""
    out = {"group": d["group"], "header": d["header"], "value": d.get("value"), "evidence": (d.get("evidence") or [])[:4]}
    if d.get("question"):
        out["question"] = d["question"]
    ans = norm(answer)
    options = d.get("options") or []
    first = options[0] if options else None
    rec = _strip_rec(first if isinstance(first, str) else (first or {}).get("label"))
    confidence = d.get("confidence") or "low"
    if not ans or ans.lower().startswith("you decide"):
        if d.get("status") == "not_applicable":
            out.update(status="not_applicable", source=d.get("source") or "scout")
        elif d.get("value"):
            detected = d.get("status") == "detected" and confidence == "high"
            out.update(status="detected" if detected else "assumed",
                       source="repo" if detected else "scout:" + confidence)
        else:
            out.update(status="open", source="none")
    elif _strip_rec(ans).lower() in NOT_NEEDED:
        out.update(status="not_applicable", source="user", value=None,
                   changed=d.get("status") != "not_applicable" and bool(d.get("value")))
    elif _same(_strip_rec(ans), rec) or _same(_strip_rec(ans), d.get("value")) or \
            (d.get("value") and _same(_strip_rec(ans), _clip(d["value"], 80))):
        out.update(status="decided", source="user (confirmed)", value=d.get("value") or _strip_rec(ans))
    else:
        picked = next((o for o in options if not isinstance(o, str)
                       and _same(_strip_rec(o.get("label")), _strip_rec(ans))), None)
        out.update(status="decided", source="user", value=_strip_rec(ans), changed=True)
        if picked and picked.get("description"):
            out["detail"] = norm(picked["description"])
    if not out.get("changed"):
        out.pop("changed", None)
    return out


def _uncovered(app):
    """Settled details that need code but no live feature covers."""
    covered = {c for f in app.get("features") or [] if f["status"] != "dropped" for c in f["covers"]}
    return [k for k, d in app["dimensions"].items() if k not in covered and k not in FRAMING
            and d["status"] in ("decided", "assumed") and d.get("value") and d.get("source") != "repo"]


def progress(app):
    """Counts, the details still open (asked again in a follow-up round while
    rounds are left), what the scout must still fix, and the next step."""
    dims = app["dimensions"]
    counts = {}
    for d in dims.values():
        counts[d["status"]] = counts.get(d["status"], 0) + 1
    rounds_left = max(0, MAX_ROUNDS - int(app.get("round") or 0))
    still_open = [k for k, d in _ordered(dims) if d["status"] == "open"]
    app["counts"] = counts
    app["followup"] = still_open if rounds_left else []
    app["rounds_left"] = rounds_left
    fix = [f["id"] for f in app.get("features") or [] if f.get("revise")]
    reasons = []
    if app["followup"]:
        reasons.append("details still open: " + ", ".join(app["followup"]))
    if fix:
        reasons.append("features to rewrite: " + ", ".join(fix))
    if app.get("feature_errors"):
        reasons.append("feature list errors: " + "; ".join(app["feature_errors"][:3]))
    new = [k for k in _uncovered(app) if dims[k].get("added_in_round")]
    if new and app.get("features"):
        reasons.append("details added in a follow-up round with no feature: " + ", ".join(new))
    app["next_step"] = "revise" if reasons else "build"
    app["revise_because"] = reasons
    app["handoff"] = handoff(app)
    return app


def handoff(app):
    """How a concluded brainstorm is continued in /feature-ladder:feature."""
    live = [f for f in app.get("features") or [] if f["status"] not in ("dropped", "done")]
    return {"command": f"/feature-ladder:feature --from-brainstorm {app.get('slug') or ''}".rstrip(),
            "next": live[0]["id"] if live else None, "queued": len(live)}


# ----------------------------------------------------------- follow-up rounds --
def followup(app, details):
    """Merge the scout's proposals for open and newly needed details; return the
    questions of this follow-up round (none once the rounds are used up)."""
    if not isinstance(details, dict):
        raise ValueError('followup needs {"details": {"<id>": {...}}}')
    asking = int(app.get("round") or 0) < MAX_ROUNDS
    dims = app["dimensions"]
    for dim_id, s in details.items():
        dim_id = re.sub(r"[^a-z0-9_]+", "_", str(dim_id).lower()).strip("_")
        if not dim_id:
            continue
        s = s if isinstance(s, dict) else {"value": s}
        old = dims.get(dim_id)
        if old and old["status"] in ("decided", "detected") and old.get("source") != "scout":
            continue   # the user (or the repo) already settled it: not asked again
        d = normalise({"dimensions": {dim_id: dict({k: v for k, v in (old or {}).items()
                                                     if k in ("group", "header", "question")}, **s)}})["dimensions"][dim_id]
        settled_now = d["status"] == "not_applicable" or (d["value"] and (d["confidence"] == "high" or not asking))
        out = resolve(d, None) if settled_now else dict(
            {k: d[k] for k in ("group", "header", "question", "value", "evidence", "confidence", "options")},
            status="proposed" if d["value"] else "open", source="scout:" + d["confidence"])
        if old is None:
            out["added_in_round"] = int(app.get("round") or 0) + 1
        dims[dim_id] = out
    asked = {k: d for k, d in dims.items() if d["status"] in ("proposed", "open") and asking}
    questions, open_text = [], []
    for k, d in _ordered(asked):
        q = {"detail": k, "group": d["group"], "header": d["header"][:12], "question": d.get("question") or d["header"] + "?"}
        opts = _options(dict(d, options=d.get("options") or [], evidence=d.get("evidence") or []))
        (questions.append(dict(q, options=opts)) if opts else open_text.append(q))
    app["pending_round"] = bool(asked)
    progress(app)
    return {"round": int(app.get("round") or 0) + 1, "asking": asking, "questions": questions, "open_text": open_text,
            "ask_batches": intake._batches(len(questions)),
            "note": None if asking else f"the {MAX_ROUNDS} follow-up rounds are used: the scout's proposals stand"}


def refine(app, answers):
    """Settle a follow-up round: answered details as the user said, the rest of
    this round's proposals as proposed. Re-plans the queue."""
    given = (answers or {}).get("answers") or {}
    dims = app["dimensions"]
    unknown = [k for k in given if k not in dims]
    if unknown:
        raise ValueError("answers name unknown detail(s): " + ", ".join(unknown))
    for k, d in list(dims.items()):
        if d["status"] in ("proposed", "open") or k in given:
            prev = dict(d)
            if prev["status"] not in ("proposed", "open"):
                prev.setdefault("confidence", "low")
            dims[k] = dict(resolve(prev, given.get(k)), **({"added_in_round": d["added_in_round"]}
                                                          if d.get("added_in_round") else {}))
    app["round"] = int(app.get("round") or 0) + 1
    app.pop("pending_round", None)
    features, errors, notes = plan_features(dims, app.get("features") or [])
    app.update(features=features or app.get("features") or [], feature_errors=errors,
               notes=list(dict.fromkeys((app.get("notes") or []) + notes)), refined_at=now())
    return progress(app)


# ------------------------------------------------------------------- render --
def _cell(text):
    return norm(text).replace("|", "\\|") or "-"


SOURCE_WORDS = {"user": "you", "user (confirmed)": "you (confirmed the proposal)", "repo": "found in the repo",
                "scout:high": "the request or the code", "scout": "scout", "none": "-"}


def render(app):
    dims, c = app["dimensions"], app.get("counts") or {}
    lines = [f"<!-- request: {norm(app.get('request'))} -->", f"# App sheet: {norm(app.get('request'))}", "",
             f"Settled {app.get('confirmed_at')}. {len(dims)} details: {c.get('decided', 0)} answered by you, "
             f"{c.get('assumed', 0)} proposed by the scout, {c.get('detected', 0)} found in the repo, "
             f"{c.get('not_applicable', 0)} not needed, {c.get('open', 0)} still open.", ""]
    for group in GROUPS:
        rows = [(k, d) for k, d in dims.items() if d["group"] == group and d["status"] not in ("not_applicable", "open")]
        lines += [f"## {GROUP_TITLES[group]}", "", "| Detail | Settled as | From |", "|---|---|---|"]
        for _, d in rows:
            src = SOURCE_WORDS.get(d["source"], "scout's proposal" if d["source"].startswith("scout:") else d["source"])
            value = d["value"] + (f" ({d['detail']})" if d.get("detail") else "")
            lines.append(f"| {_cell(d['header'])} | {_cell(value)} | {_cell(src)} |")
        if not rows:
            lines.append("| - | nothing settled | - |")
        lines.append("")
    na = [d["header"] for d in dims.values() if d["status"] == "not_applicable"]
    still = [f"{d['header']} ({k})" for k, d in dims.items() if d["status"] == "open"]
    lines += ["## Not needed", "", ", ".join(na) or "none", "", "## Still open", "",
              ", ".join(still) or "none", "", "## Features, in build order", ""]
    feats = app.get("features") or []
    for i, f in enumerate(feats, 1):
        dep = f" (after {', '.join(f['depends_on'])})" if f["depends_on"] else ""
        flag = " - REVISE: you changed " + ", ".join(f.get("changed", [])) if f.get("revise") else ""
        lines.append(f"{i}. [{f['status']}] **{f['title']}** `{f['id']}`{dep}{flag}")
        lines.append(f"   {f['request']}")
    if not feats:
        lines.append("none yet" + ("".join(f"\n- {e}" for e in app.get("feature_errors") or [])))
    if app.get("notes"):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in app["notes"]]
    if app.get("round"):
        lines += ["", f"Follow-up rounds of questions: {app['round']} of at most {MAX_ROUNDS}."]
    ho = app.get("handoff") or {}
    if ho.get("next"):
        lines += ["", "## Continue", "", f"Build the queue with `{ho['command']}` "
                  f"(next: `{ho['next']}`; add a feature id after the slug to build just that one)."]
    if app.get("architecture_memory"):
        am = app["architecture_memory"]
        lines += ["", f"Architecture stored in Hindsight memory: {am.get('queued', 0)} fact(s) queued {am.get('at', '')}."]
    lines.append("")
    return "\n".join(lines)


def save_app(app, out=None):
    root = app.get("repo_root") or os.getcwd()
    if not out:
        intake._ensure_ignore(root)
        out = str(_dir(root) / f"{app.get('slug') or 'app'}.json")
    _dump(out, app)
    md = Path(out).with_suffix(".md")
    md.write_text(render(app), encoding="utf-8", newline="\n")
    return out, str(md)


# ------------------------------------------------------- the confirmed sheet --
def _confirmed(root):
    d = _dir(root)
    found = []
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            if f.name.endswith(("-scan.json", "-sheet.json", "-answers.json", "-features.json")):
                continue
            try:
                app = _load(f)
            except (OSError, ValueError):
                continue
            if app.get("confirmed_at") and isinstance(app.get("dimensions"), dict):
                found.append((f, app))
    return found


def current(root, sheet=None, slug=None):
    """The sheet named (a path, or a slug under .feature-ladder/brainstorm/),
    else the one most recently confirmed in this repo."""
    if sheet:
        return Path(sheet), _load(sheet)
    if slug:
        slug = str(slug).strip()
        hits = [fa for fa in _confirmed(root) if fa[0].stem == slug or fa[1].get("slug") == slug]
        if not hits:
            raise ValueError(f"no confirmed app sheet {slug!r} in {_dir(root)}")
        return max(hits, key=lambda fa: fa[1]["confirmed_at"])
    found = _confirmed(root)
    if not found:
        raise ValueError("no confirmed app sheet here; run the brainstorm first")
    return max(found, key=lambda fa: fa[1]["confirmed_at"])


def context(app, feature_id):
    """The short block a feature's cards carry: the details it covers plus the
    stack, layout and tests. Never the whole sheet."""
    feats = [f for f in app.get("features") or [] if f["status"] != "dropped"]
    f = next((x for x in feats if x["id"] == feature_id), None)
    if f is None:
        raise ValueError(f"no feature {feature_id!r} in this app sheet")
    dims = app["dimensions"]
    titles = {x["id"]: x["title"] for x in feats}

    def settled(k):
        d = dims.get(k) or {}
        return d.get("value") if d.get("status") in ("decided", "assumed", "detected") else None

    lines = ["## App context (settled in the brainstorm; binding)",
             "App: " + _clip(settled("purpose") or app.get("request"), MAX_VALUE),
             f"Feature {feats.index(f) + 1} of {len(feats)}: {f['title']}. Builds on: "
             + (", ".join(titles.get(d, d) for d in f["depends_on"]) or "nothing") + "."]
    for k in list(dict.fromkeys(list(f["covers"]) + list(ALWAYS_ON_CARD))):
        if settled(k) and k != "purpose":
            extra = f" ({dims[k]['detail']})" if dims[k].get("detail") else ""
            lines.append(f"{dims[k]['header']}: " + _clip(settled(k) + extra, MAX_VALUE))
    if settled("non_goals"):
        lines.append("Out of scope: " + _clip(settled("non_goals"), MAX_VALUE))
    others = [x["title"] for x in feats if x["id"] != feature_id and x["status"] != "done"]
    if others:
        lines.append("Other features, not yours: " + _clip("; ".join(others), MAX_VALUE))
    text = "\n".join(lines)
    return text if len(text) <= MAX_CONTEXT else text[:MAX_CONTEXT - 3].rstrip() + "..."


def summary(app):
    """Counts per status, and the queued features that cannot run: they wait,
    directly or through another feature, on one that ended partial or skipped."""
    feats = app.get("features") or []
    counts = {s: len([f for f in feats if f["status"] == s]) for s in STATUSES}
    stuck = {f["id"] for f in feats if f["status"] in ("partial", "skipped")}
    blocked = []
    for f in feats:   # build order: a blocked dependency is seen before its dependents
        if f["status"] == "queued" and any(d in stuck for d in f["depends_on"]):
            blocked.append(f["id"])
            stuck.add(f["id"])
    return dict(counts, blocked=blocked, total=len([f for f in feats if f["status"] != "dropped"]))


def next_feature(path, app, root, feature_id=None, build=None):
    """Pick (and mark running) the next feature: one already running, else the
    first queued one whose dependencies are all done. `feature_id` asks for one
    feature by id; `build` (all | first) overrides the sheet's choice, so a
    plan-only sheet can be handed to /feature-ladder:feature later."""
    feats = app.get("features") or []
    done = {f["id"] for f in feats if f["status"] == "done"}
    if build in ("all", "first") and app.get("build") != build:
        app["build"] = build
        save_app(app, str(path))
    result = {"sheet": str(path), "slug": app.get("slug"), "build": app.get("build", "all"), "feature": None}
    if app.get("next_step") == "revise" and app.get("revise_because"):
        result["warning"] = "the sheet still has work for the scout: " + "; ".join(app["revise_because"])
    sess = intake.session_status(root)
    if "left" in sess:
        result["session_left"] = sess["left"]
    pick = None
    if app.get("build") == "plan":
        result["note"] = "plan only: nothing is built in this run"
    elif result.get("session_left") == 0:
        result["note"] = "the session's subagent budget is spent; the rest stay queued"
    elif feature_id:
        pick = next((f for f in feats if f["id"] == feature_id), None)
        if pick is None:
            raise ValueError(f"no feature {feature_id!r} in this app sheet")
        waiting = [d for d in pick["depends_on"] if d not in done]
        if pick["status"] not in ("queued", "running"):
            result["note"] = f"{feature_id} is {pick['status']}; `done {feature_id} --status queued` queues it again"
            pick = None
        elif waiting:
            result["note"] = f"{feature_id} waits on {', '.join(waiting)}, not done yet"
            pick = None
    else:
        pick = next((f for f in feats if f["status"] == "running"), None) or \
            next((f for f in feats if f["status"] == "queued" and all(d in done for d in f["depends_on"])), None)
    if pick:
        pick.update(status="running", started_at=pick.get("started_at") or now())
        save_app(app, str(path))
        live = [f for f in feats if f["status"] != "dropped"]
        text = context(app, pick["id"])
        result["feature"] = {"id": pick["id"], "title": pick["title"], "request": pick["request"],
                             "n": live.index(pick) + 1, "of": len(live), "revise": bool(pick.get("revise")),
                             "brainstorm": {"sheet": str(path), "feature": pick["id"], "context": text}}
    result["remaining"] = summary(app)
    if not pick and "note" not in result:
        left = result["remaining"]
        result["note"] = ("blocked: " + ", ".join(left["blocked"]) + " wait on a feature that did not finish"
                          if left["blocked"] else "every feature is finished")
    return result


# --------------------------------------------------- architecture -> Hindsight --
def _memory_gate():
    sys.path.insert(0, str(HERE.parents[1] / "hindsight-memory" / "scripts"))
    import memory_gate
    return memory_gate


def architecture_facts(app):
    """One '[decision] `arch:<id>` Header: value' line per architecture detail
    the brainstorm settled (decided by the user or proposed by the scout and
    kept). Details the repo already shows (detected) are left out: the scan
    finds them again for free. Each line is one fact for the memory gate:
    at most two sentences and 280 characters."""
    mg = _memory_gate()
    out = []
    for k, d in _ordered(app.get("dimensions") or {}):
        if (d["group"] != "architecture" and k not in ARCH_EXTRA) or d["status"] not in ("decided", "assumed"):
            continue
        value = norm(d.get("value"))
        if d.get("detail") and len(value) < 120:
            value += f" ({norm(d['detail'])})"
        if not value:
            continue
        head = f"`arch:{k}` {d['header']}: "
        body = " ".join(mg._sentences(value)[:mg.MAX_SENTENCES])
        if len(head) + len(body) > mg.MAX_CHARS:
            body = body[:mg.MAX_CHARS - len(head) - 3].rstrip(" ,;:.") + "..."
        out.append(f"[decision] {head}{body}")
    return out


def remember(app, root, flush=True):
    """Queue the architecture for Hindsight and (in Claude Code) start the
    unattended write. Written outside the per-session cap, tagged
    `architecture`, never during a feature run."""
    mg = _memory_gate()
    q = Path(root) / ".feature-ladder" / ARCH_QUEUE
    queued_before = set()
    if q.is_file():
        queued_before = {mg._hash(l.split("]", 1)[-1].strip()) for l in q.read_text(encoding="utf-8").splitlines() if "]" in l}
    seen = mg.retained_hashes(root) | queued_before
    ok, skipped = [], []
    for line in architecture_facts(app):
        try:
            kind, fact, h = mg.check_line(root, line, seen)
            seen.add(h)
            ok.append(f"[{kind}] {fact}")
        except ValueError as e:
            skipped.append({"line": line[:200], "reason": str(e)})
    if ok:
        q.parent.mkdir(parents=True, exist_ok=True)
        with open(q, "a", encoding="utf-8") as fh:
            for l in ok:
                fh.write(f"- {l}\n")
        intake._ensure_ignore(root)
    started = None
    if flush and (ok or q.is_file()):
        try:
            started = mg.background(Path(root))
        except Exception as e:  # noqa: BLE001 - memory never stops the brainstorm
            started = {"started": False, "reason": f"{type(e).__name__}: {e}"[:200]}
    return {"queued": len(ok), "facts": ok, "skipped": skipped, "queue": str(q), "flush": started}


# --------------------------------------------------------------------- main --
def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], list(argv[1:])

    def opt(name, default=None):
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1] if i + 1 < len(rest) else default
            del rest[i:i + 2]
            return val
        return default

    def flag(name):
        if name in rest:
            rest.remove(name)
            return True
        return False

    try:
        cwd = opt("--cwd", os.getcwd())
        if cmd == "guide":
            here = Path(__file__).resolve()
            sys.stdout.write(GUIDE.format(script=here, recall=here.parents[2] / "hindsight-memory" / "scripts" / "recall.py"))
            return 0
        if cmd == "scan":
            save_it = flag("--save")
            text = " ".join(rest).strip()
            if not text:
                print("scan needs the request text", file=sys.stderr)
                return 1
            result = scan(text, cwd)
            if save_it:
                intake._ensure_ignore(result["repo_root"])
                out = _dir(result["repo_root"]) / f"{result['slug']}-scan.json"
                _dump(out, result)
                result["written_to"] = str(out)
                result["sheet_file"] = str(out.with_name(f"{result['slug']}-sheet.json"))
            print(json.dumps(result, indent=2))
            return 0
        if cmd == "decide":
            mode, max_q = opt("--mode", "default"), opt("--max")
            max_q = int(max_q) if max_q else None
            print(json.dumps(decide(load_sheet(rest[0]), mode, max_q), indent=2))
            return 0
        if cmd == "apply":
            out, mode = opt("--out"), opt("--mode", "default")
            sheet = load_sheet(rest[0])
            answers = _load(rest[1]) if len(rest) > 1 else {}
            app = apply(sheet, answers, mode)
            sroot = session_budget.find_root(sheet.get("repo_root") or cwd)
            decisions = answers.get("decisions") or {}
            if decisions.get("orchestrator"):     # the session's answer, asked once, up front
                session_budget.set_orchestrator(sroot, decisions["orchestrator"])
            size = str(decisions.get("session_agents") or "").strip()[:1].upper()
            if size in session_budget.SIZES:
                app["session"] = session_budget.set_size(sroot, size)
            app["repo_root"] = app.get("repo_root") or str(Path(cwd).resolve())
            path, md = save_app(app, out)
            print(json.dumps(dict(app, written_to=path, readable=md), indent=2))
            return 0
        if cmd == "features":
            path, app = Path(rest[0]), _load(rest[0])
            new = _load(rest[1])
            feats, errors, notes = plan_features(app["dimensions"], new.get("features") if isinstance(new, dict) else new)
            if errors:
                print(json.dumps({"errors": errors}, indent=2))
                return 1
            for f in feats:   # the list was just rewritten against the confirmed sheet
                f.pop("revise", None)
                f.pop("changed", None)
            for d in app["dimensions"].values():   # ... so no detail flags a rewrite again
                d.pop("changed", None)
                d.pop("added_in_round", None)
            app.update(features=feats, feature_errors=[], notes=notes, revised_at=now())
            progress(app)
            _, md = save_app(app, str(path))
            print(json.dumps({"features": feats, "notes": notes, "next_step": app["next_step"],
                              "revise_because": app["revise_because"], "handoff": app["handoff"],
                              "written_to": str(path), "readable": md}, indent=2))
            return 0
        if cmd in ("followup", "refine"):
            path, app = Path(rest[0]), _load(rest[0])
            data = _load(rest[1]) if len(rest) > 1 else {}
            if cmd == "followup":
                out = followup(app, data.get("details") if isinstance(data, dict) else None)
                _, md = save_app(app, str(path))
                print(json.dumps(dict(out, written_to=str(path), readable=md), indent=2))
                return 0
            refine(app, data)
            _, md = save_app(app, str(path))
            print(json.dumps(dict(app, written_to=str(path), readable=md), indent=2))
            return 0
        if cmd == "remember":
            sheet_opt, slug, no_flush = opt("--sheet"), opt("--app"), flag("--no-flush")
            root, _ = intake.repo_root(cwd)
            path, app = current(root, sheet_opt, slug)
            out = remember(app, app.get("repo_root") or root, flush=not no_flush)
            facts = list(dict.fromkeys((app.get("architecture_memory") or {}).get("facts", []) + out["facts"]))
            app["architecture_memory"] = {"facts": facts, "queued": len(facts), "at": now()}
            save_app(app, str(path))
            print(json.dumps(dict(out, sheet=str(path)), indent=2))
            return 0
        if cmd in ("next", "done", "context"):
            sheet_opt, slug, as_json = opt("--sheet"), opt("--app"), flag("--json")
            fid, status, note, build = opt("--feature"), opt("--status", "done"), opt("--note"), opt("--build")
            root, _ = intake.repo_root(cwd)
            path, app = current(root, sheet_opt, slug)
            if cmd == "next":
                if build not in (None, "all", "first"):
                    print("next: --build is all or first", file=sys.stderr)
                    return 1
                print(json.dumps(next_feature(path, app, root, fid, build), indent=2))
                return 0
            fid = fid or (rest[0] if rest else None)
            if cmd == "context":
                text = context(app, fid)
                print(json.dumps({"sheet": str(path), "feature": fid, "context": text}, indent=2) if as_json else text)
                return 0
            f = next((x for x in app.get("features") or [] if x["id"] == fid), None)
            if f is None or status not in ("done", "partial", "skipped", "queued"):
                print(f"done: unknown feature {fid!r} or status {status!r} (done, partial, skipped, queued)", file=sys.stderr)
                return 1
            f.update(status=status, finished_at=now(), **({"note": note} if note else {}))
            save_app(app, str(path))
            print(json.dumps({"feature": fid, "status": status, "remaining": summary(app)}, indent=2))
            return 0
        if cmd == "find":
            root, _ = intake.repo_root(cwd)
            text = norm(" ".join(rest))
            hits = [fa for fa in _confirmed(root) if text and norm(fa[1].get("request")) == text]
            if hits:
                path, app = max(hits, key=lambda fa: fa[1]["confirmed_at"])
                if app.get("build") == "plan":   # they came back to build what they planned
                    app["build"] = "first"
                app["resumed_at"] = now()
                save_app(app, str(path))
                print(str(path))
            return 0
        if cmd == "show":
            root, _ = intake.repo_root(cwd)
            arg = " ".join(rest).strip()
            if arg.endswith((".md", ".json")) and Path(arg).is_file():
                path = Path(arg).with_suffix(".md")
            elif arg:
                hits = [fa for fa in _confirmed(root) if norm(fa[1].get("request")) == norm(arg)]
                path = hits[0][0].with_suffix(".md") if hits else None
            else:
                path = current(root)[0].with_suffix(".md")
            if path is None or not path.is_file():
                print("show: no saved app sheet", file=sys.stderr)
                return 1
            sys.stdout.write(path.read_text(encoding="utf-8"))
            return 0
    except (ValueError, KeyError, IndexError, OSError) as e:
        print(f"brainstorm {cmd}: {e}", file=sys.stderr)
        return 1
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    sys.exit(main(sys.argv[1:]))
