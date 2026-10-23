# feature-ladder

**An orchestration ladder for adding features to your applications with Claude
subagents.** A top-tier coordinator plans and verifies, but delegates
everything it can. Each task is sent to the cheapest model that can do it
correctly and given only the skills it needs. Both choices are made by
deterministic scripts, never by a model's mood. Project memory from
[Hindsight](https://github.com/vectorize-io/hindsight) carries lessons from
one feature to the next.

It works in **Claude Code** (as a plugin) and in the **Claude app** (as a skill).
Both use the same scripts and rules.

---

## Scope of work

**What it does**

- Takes a feature request ("add rate limiting to /search, 100 req/min per key")
  and implements **just that change and its tests**, atomically: one scoped
  recall, one Task Card, one cheap worker, a scripted check, a PR.
- **Asks first, then finishes.** Before anything is delegated, a Sonnet scout
  works out every routing variable from your code, with evidence and a
  confidence level. You're asked only about the ones that are uncertain *and*
  would change which model does the work, the design choices a worker would
  otherwise guess, and, once per coding session, **how many subagents the
  session may spawn** (Small 1-2, Medium 3-4, Large up to 8). After that the
  run goes to completion without stopping.
- **Pushes the work down.** Sonnet medium is the default implementation rung,
  and mechanical and small coding cards run on **GLM 5.3 Flash** through
  OpenRouter (billed to OpenRouter credits, not your Claude plan) whenever a
  key is present.
- **No Fable unless you ask.** The router never picks Fable; the coordinator
  is the only Fable call in a run. Second opinions, deep verification and
  production-write verdicts need an explicit switch.
- **Reviews and memory once per session, not per feature.** Codex review,
  adversarial tests and a Fable second opinion run only when you schedule
  them with `/feature-ladder:wrap`, once, over everything the session built.
  Memory is written at the same point, by a script, through a strict gate.
- **Hooks enforce the budget.** A spawn hook denies any subagent past the
  session budget, a second mechanic or design step, and any review, scribe or
  Fable rung inside a run. The coordinator is capped at 24 turns.
- Decides **which model and effort level** handles each task with a fixed
  scoring function (`route.py`), and **which skills** each worker gets with a
  fixed rule engine (`select_skills.py`); the skill text goes to a file only
  the worker reads.
- Works on its own branch: a critical decision mid-run becomes a draft PR and
  work continues stacked on it. Outside-repo steps (deploys, data writes) run
  only as far as the policy you pick at intake; the default is code only.
- Requires evidence: every worker quotes real command output, and acceptance
  checks are re-run by a script (`verify.py`), not by another model.

**What it does not do**

- It does not merge, push to your base branch, or force-push. It pushes its
  own `feature-ladder/...` branches and opens PRs for you to review. It never
  deletes a project, database, repo or backup, and never weakens database
  protection; a guard hook enforces that list while a run is active.
- It does not write undo scripts, backups or rehearsals for live changes.
  Tokens go to the feature; the services' own safeguards are relied on.
- It does not invent a design when the request is ambiguous. It asks at
  intake, or escalates to a design step if you'd rather it chose.
- It does not replace your tests. "Done" means your checks passed, quoted
  verbatim.

---

## The ladder

| Level | Agent | Model / effort | Daily job |
|---|---|---|---|
| I | `scoper` | Sonnet 5.5 / medium | Intake: settles every routing variable with evidence before anything is delegated |
| T0 | `mechanic` | GLM 5.3 Flash, else Haiku / low | Mechanical patches, docs drafts, fixtures; at most one per feature |
| T1 | `evidence-collector` | Haiku / medium | Collect current sources, extract claims, prepare evidence |
| T2 | `implementer` | GLM 5.3 Flash (score <= 6), else **Sonnet 5.5 / medium** | Bounded coding and its tests: the default rung |
| T3 | `implementer` | Sonnet 5.5 / high | Bounded coding, harder |
| T4–T5 | `senior-implementer` | Opus 5.5 / medium–high | Difficult implementation under an agreed design |
| D | `domain-expert`, `domain-expert-high` | Opus 5.5 / medium–high | One design step, only on a hard flag |
| **F-Med** | **`coordinator`** | **Fable 5.1 / medium** | **Card, dispatch, verify.py, accept: 12-24 turns** |
| F-Low | `second-opinion` | Fable 5.1 / low | Only with `--second-opinion` |
| F-High | `deep-verifier` | Fable 5.1 / high | Only with `--escalate` |
| F-Extra | `architect-escalation` | Fable 5.1 / xhigh | Only with `--escalate` |
| F-Ultra | `ultra-investigator` | Fable 5.1 / max | Only with `--ultra` |
| X | `codex-reviewer` | Haiku / low driving **OpenAI Codex** | Only at `/feature-ladder:wrap --review`, once per session |

### How a feature run flows

0. **Intake.** The scoper scans the repo and reads the code the feature will
   touch, and proposes a value, confidence and evidence for every routing
   variable. `intake.py decide` picks the questions: the session's subagent
   budget (first feature only), uncertain variables whose answer changes the
   model, open design decisions, and the live-changes policy. You answer once;
   the confirmed intake is binding. The run moves to `feature-ladder/<slug>`.
1. **Recall once.** One Hindsight recall, scoped by a hook to this repo's
   bank and capped at 1000 tokens; only the snippets that bear on the card go
   into it.
2. **One Task Card.** `intake.py card` routes it and picks its skills in one
   call. A hard flag adds one design step before the worker; nothing else.
3. **One worker.** GLM 5.3 Flash or Sonnet medium does the change and its
   tests and returns an *Evidence Packet* with real check output.
4. **Verify with no model.** `verify.py` re-runs the checks and flags files
   outside the card's scope. A failure is re-dispatched once, then one band
   up once, then reported `partial`.
5. **Accept.** The coordinator queues the packet's memory candidates (through
   the memory gate), writes `FEATURE-REPORT.md` with the token table, and
   opens the PR.

At the end of a session (many features), `/feature-ladder:wrap` writes the
queued memories with a script and, only if you ask, runs one Codex review,
adversarial tests or a Fable second opinion over the whole session.

Throughout, nothing stops for you. Non-critical choices are decided and
listed under Assumptions. A **critical decision** is decided with the
recommended option and parked in a draft PR that explains how to switch.

---

## Features

### Deterministic model routing

`skills/complexity-router/scripts/route.py` is a pure function: task facts
in, rung out. Points come from files, lines changed, modules touched, new
dependencies, public API changes, persistence, security, concurrency,
cross-system work, missing tests, ambiguity, novelty and blast radius. Score
bands: mechanic (0–3), implementer medium (4–14; 4–6 is the GLM tier), Sonnet
high (15–19), Opus medium (20–23), Opus high (24+, design required). The
router never returns Fable. A hard flag (approach genuinely open, whole-app
blast radius) adds one design step and never changes the size. A short
request is not an ambiguous one: ambiguity is judged from the code. Run
`route.py --table` for the full table.

### Budgets: the session and the feature

Cost scales with agents × turns × context, because every turn of every agent
re-reads its whole context. Two budgets cap it, both enforced by hooks:

**Session** (asked once per coding session): Small = 2 subagents, Medium = 4,
Large = 8. The intake scout and the coordinator aren't counted; every
worker, expert, reviewer and GLM worker is. A new Claude Code session asks
again.

**Feature** (from the score):

| Size | Score | Workers | Coordinator turns |
|---|---|---|---|
| S | 0–9 | 1 | ~12 |
| M | 10–19 | 1, or 2 for two independent file sets | ~20 |
| L | 20+ | up to 4 | ~24 (hard cap at every size) |

Every size: at most one mechanic, a design step only on a hard flag, no Codex,
no scribe, no Fable rung, no check-runner. For the Firestore-write class of
feature this is one GLM or Sonnet-medium worker and a coordinator of about a
dozen turns (a test pins it).

### Intake: ask once, up front

`skills/complexity-router/scripts/intake.py` has three steps:

- `scan` reads the repo without changing it: candidate files, test files and
  the test command, the areas present (database, auth, API, workers, outside
  clients) and git state. It gives each variable a first guess.
- The `scoper` agent reads the code and corrects every value with evidence and
  a confidence level.
- `decide` asks about a variable only when its confidence is not high **and**
  flipping it changes the rung. It also checks whether several small guesses
  together would change the rung. Open design decisions are always asked.
  Uncommitted changes in your checkout get one question: include them as their
  own commit, stash them, or stop. If a production target is detected
  (`.firebaserc` aliases, for instance), you get one more: how far live
  changes may go (all, non-production only, nothing outside the repo).

`--auto` asks nothing and takes the scout's values and recommended options.
`/feature-ladder:scope` runs intake alone and saves it; the next
`/feature-ladder:feature` with the same request text reuses it without asking.

Switches: `--auto`, `--second-opinion` (allows one Fable verdict, e.g.
before a production overwrite), `--escalate` (allows the Fable debugging
rungs) and `--ultra`. Reviews are not feature switches: they belong to
`/feature-ladder:wrap`.

### Secrets from Google Secret Manager

API keys for the services your code talks to (Firestore, Stripe, Discord,
OpenAI, anything) stay in Secret Manager. The scout reads the code and lists
the secret **names** the feature needs (`defineSecret("X")`,
`projects/p/secrets/X`, `process.env.X_KEY`, `.env.example` keys); the
coordinator puts them on each Task Card. A worker that must run something
needing a key runs it as

```
with_secrets.py --secret NAME[=ENV_VAR] -- <command>
```

which fetches the value with your own `gcloud` sign-in, puts it in that one
command's environment and masks it in the output. The guard denies anything
that would print a value into the transcript (`gcloud secrets versions
access`, `cat .env`, `printenv`, `echo $X_KEY`). Deleting, disabling or
re-sharing a secret is on the never-list; creating one on production is a
risky step. Values never reach a report, PR, commit, memory or Codex prompt.

The guard classifies the command after `--` exactly as if it ran on its own,
so a production overwrite inside the wrapper is still left for you.

### Run to completion: branches and checkpoint PRs

`skills/orchestration-ladder/scripts/pr_checkpoint.py` handles the branches.

| Step | What it does |
|---|---|
| `start` | Creates `feature-ladder/<slug>` from your branch, records the intake policy, and turns on the guard hook for this repo |
| `checkpoint` | Commits, pushes and opens a **draft PR** saying what was assumed and how to switch, then continues on `feature-ladder/<slug>-pt2` |
| `finish` | Opens the final PR on top of the stack and prints the merge order |
| `abort` | Turns the guard off after a run died midway; branches and commits stay |

Without a remote or the `gh` CLI (or with `--no-pr`), the same branches are
made locally and each PR description is saved in `.feature-ladder/checkpoints/`.

### The GLM tier: GLM 5.3 Flash through OpenRouter

Worker turns are where the tokens go, and on Claude they all count against
the plan. Cards whose route says `provider: glm` (every mechanic card, and
implementer cards at medium effort with a score of 6 or less) run as a child
`claude -p` whose environment points at OpenRouter
(`ANTHROPIC_BASE_URL=https://openrouter.ai/api`, the key from Google Secret
Manager or `OPENROUTER_API_KEY`, the model pinned through
`ANTHROPIC_DEFAULT_*_MODEL`). Same tools, same Evidence Packet, same guard
hook; billed to OpenRouter credits at GLM prices (about $0.15/M in, $0.50/M
out). It is on by default whenever the Claude CLI and a key are present;
without them the card runs on the routed Claude rung and says why.

| Setting | Cheap cards run on |
|---|---|
| `/feature-ladder:workers glm` (default) | `z-ai/glm-5.3-flash:nitro` |
| `/feature-ladder:workers jev` | `typesafe/jev-router` (picks a model and effort per request; variable price) |
| `/feature-ladder:workers claude` | Haiku / Sonnet-medium subagents |

Everything else (Sonnet medium by default, Opus, the coordinator) stays on
Claude. External workers count against the session budget like any other.
Usage lands in the ledger with what OpenRouter actually charged.
`/feature-ladder:workers check --probe` validates the key, shows remaining
credit and pings each model.

The child process is Claude Code itself, so OpenRouter's documented Claude
Code integration applies ([their cookbook](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration.md)).
Their API is a single stable `v1` with changes announced in the changelog,
so nothing is pinned ([versioning](https://openrouter.ai/docs/api_reference/versioning)).

### Changes outside the repo

Code on a branch is always reversible, so `irreversible` only means effects
outside the repo (in routing it is a +3 risk weight, not a stop). Those steps
are run, not parked. `skills/orchestration-ladder/scripts/irreversible.py`
classifies every shell command during a run, with no model tokens, and the
`PreToolUse` guard applies the result:

| Tier | Examples | What happens |
|---|---|---|
| **mundane** | emulator, dev or staging targets; `firebase deploy`; index builds; exports; dry runs; anything that doesn't destroy production data | runs at once, logged |
| **risky** | `firestore:delete`, `firestore import`, destructive SQL, migration/backfill scripts against production | left for you with the exact command; with `--second-opinion`, one Fable verdict, `irreversible.py approve`, then it runs |
| **never** | deleting a project, database, repo or backups; weakening database protection; merging PRs; pushing to your base branch; force-pushing or deleting `main`; `rm -rf` of `/` or your home folder | left for you, logged |

The intake policy narrows this: *nothing outside the repo* (the recommended
default) leaves all of them for you; *non-production only* leaves every
production action. Production is
recognised from `.firebaserc` aliases, `--project` flags and words like
`prod`/`staging` in the command; an unknown target on a real cloud tool is
treated as production. Only the executable part of a command is classified
(quoted strings, heredocs and comments are stripped), so a commit message or a
`grep` that mentions `DROP TABLE` is not a command. Code a shell still runs is
classified too: `bash -c '...'`, `eval`, a heredoc fed to a shell, `$(...)`,
backticks, and the command after `--` in `with_secrets.py`. Recursively
deleting `/` or your home folder is on the never-list. Every action, approved,
rejected or left for you, is listed in a table in `FEATURE-REPORT.md`.

### Deterministic skill selection

See [Skill selection](#skill-selection) below.

### Token ledger

Every run records what each rung cost, with no model tokens spent. When a
subagent stops, the `SubagentStop` hook (`hooks/scripts/ledger.py`) reads its
transcript, sums input, cache-read, cache-write and output tokens, parses the
rung, model, effort and task id from its Task Card, and appends one line to
`.feature-ladder/runs/<slug>/usage.jsonl`. A subagent bounced by the retain
gate replaces its earlier line rather than counting twice.

`ledger.py report` prints a table by rung and model (agents, turns, tokens by
kind, each rung's share of output tokens). The coordinator pastes it into
`FEATURE-REPORT.md` at acceptance, and `/feature-ladder:feature` prints the
complete table after the coordinator has finished, so the coordinator's own
cost is included. The ledger is what the router bands, skill budgets and rung
choices get tuned against; without it those are guesses.

### Hindsight memory, tightly controlled

Every Hindsight call goes through a hook (`resolve_bank.py` plus
`memory_gate.py`), with no model in the loop:

- **The right bank:** one bank per repository, found or created
  deterministically (a pinned id in `.feature-ladder/hindsight.json`, else
  `coding-agent::<owner>-<repo>`, `<owner>-<repo>`, `coding-agent::<project>`,
  `<project>`); the hook adds the `bank_id` to every call.
- **Scoped reads:** `recall` only, tags forced to `project:<name>`, at most
  1000 tokens, at most 3 recalls per feature (scout, coordinator, one worker
  without a Memory line). `reflect`, knowledge pages and bank edits are
  denied: `reflect` is a free-form summary by the server's model.
- **No writes during a run.** Workers list facts as `[kind] fact` under
  `Memory candidates:`; the coordinator queues the ones a future task could
  act on, and the gate checks each line on the spot.
- **Only useful facts:** a memory must be one fact of 25–280 characters, of
  kind `decision`, `convention`, `pitfall`, `interface` or `test-strategy`,
  name a path, identifier, command or flag in the repo, contain no narration
  or summary, and not duplicate anything already stored. There is no
  `outcome` kind: what a feature did is in git.
- **Written once, verbatim:** `/feature-ladder:wrap` runs
  `memory_gate.py flush`, which writes the queue straight to the bank over MCP.
  No scribe agent, no rewording.

### Cross-model review (OpenAI Codex), once per session

A different model family catches different mistakes, but paying for it on
every small feature is what burned the budget. Codex reviews run only at
`/feature-ladder:wrap --review`, once, over the whole session's diff, through
the official Codex CLI and your own Codex sign-in.

- **Review:** read-only sandbox, structured findings (severity, file, line,
  why, fix, repro). The script drops `low` findings and duplicates with no
  model tokens.
- **Adversarial tests (`--adversarial`):** Codex writes attack tests only
  under `.feature-ladder/adversarial/<slug>/` and runs them; a `git status`
  check flags anything it touched outside that folder.
- **Report, don't loop:** findings are listed with their repro commands; each
  confirmed bug becomes its own `/feature-ladder:feature` request. No fix
  rounds, no re-review, no mechanic per finding. A disputed finding gets a
  Fable answer only with `--second-opinion`.
- **Optional:** if Codex isn't installed or signed in, wrap says so.

Records are saved to `.feature-ladder/reviews/`. On demand, for any branch or
uncommitted work: `/feature-ladder:codex-review [--adversarial]`.

### Evidence and escalation discipline

- Task cards, evidence packets and design docs follow fixed formats
  (`skills/orchestration-ladder/SKILL.md`).
- Acceptance checks cost no tokens:
  `skills/orchestration-ladder/scripts/verify.py run --id T3 --check "pytest tests/x -q" --scope src/x.py --changed src/x.py`
  runs the card's checks exactly as written, saves exit codes and output
  tails to `.feature-ladder/evidence/<task>.json`, and compares the files that
  really changed with the card's scope and the worker's claim. A check that
  would touch production data or print a secret is refused, not run. No
  subagent re-checks the work.
- A failed task is re-dispatched once, then moves up **one** band once, then
  is reported `partial`.

---

## Skill selection

### Why

A subagent normally starts with the description of every installed skill and
can load any of them. With hundreds of skills installed, that wastes tokens
and invites the wrong skill. feature-ladder makes skill choice explicit,
repeatable and auditable.

### How it works

1. After routing, the coordinator runs
   `skills/skill-selector/scripts/select_skills.py` with the task's `role`
   (from the router), `kind`, `paths` (the card's scope), `text` (goal and
   description) and router `flags`.
2. The script scans the skills installed on that machine. That covers
   personal skills, project `.claude/skills`, installed plugins, and in the
   Claude app the synced account and plugin skills. It then applies the rules
   in `skill-rules.json`.
3. It returns at most `max_skills` skills (default 3) within a character
   budget of skill text: `max_chars_by_role` per router role (shipped
   defaults: mechanic 0, implementer 8,000, senior implementer and domain
   experts 30,000), else `max_chars` (default 20,000). Anything cut is
   reported, never silently included.
4. `--inject --out .feature-ladder/cards/<task>-skills.md` writes those
   skills' full instructions to that file and prints a three-line block that
   names it. The coordinator pastes the block into the task card and logs the
   choice in `FEATURE-REPORT.md`; the worker reads the file before it starts.
   The skill text (up to 60,000 characters per card) therefore never enters
   the coordinator's context or its output, which is where a large share of a
   run's Fable tokens went before. Plain `--inject` still prints the full text
   for dry runs.
5. **Enforcement:**
   - **Claude Code:** every ladder agent has `Skill` in `disallowedTools`, so
     the injected skills are the only ones a worker can use. A worker that
     needs another skill returns `blocked`. The coordinator re-dispatches with
     an explicit, logged `add`.
   - **Claude app:** subagents cannot have tools removed per call. The worker
     prompt instructs them not to load other skills, and the selection is still
     logged.

Same task, same rules and same installed skills always give the same skills.

### Rules

A rule fires when **every** group in `when` matches (any value within a
group), and nothing in `unless` matches:

```json
{"id": "bigquery", "skill": "data-agent-kit:bigquery-sql", "group": "sql", "priority": 80,
 "when":   {"keywords": ["bigquery", "bq"], "roles": ["implementer", "senior-implementer"]},
 "unless": {"keywords": ["prototype"]}}
```

| Group | Matches |
|---|---|
| `roles` | the router role / agent (`implementer`, `mechanic`, `domain-expert`, …) |
| `kinds` | `code`, `docs`, `fixture`, `check`, `research`, `analysis` |
| `globs` | each path in scope, or its file name (`*.sql`, `cdk.json`) |
| `keywords` | whole words in the task text, case-insensitive |
| `all_keywords` | every listed word must appear |
| `flags` | router booleans (`security`, `persistence`, …) |

- **Priority:** the highest priority wins. Ties keep file order.
- **Groups of alternatives:** rules sharing a `group` are alternatives, and
  only the highest-priority *installed* one is used. That is how one rules
  file serves both environments: `superpowers:systematic-debugging` in Claude
  Code, `engineering:debug` in the Claude app.
- **Skill names:** personal and project skills use their own name
  (`aws-cdk`). Plugin skills use `plugin:skill`
  (`superpowers:test-driven-development`).

Rules are merged in this order; a later file replaces a rule with the same
`id`, and `"disable": ["id"]` removes one:

1. `plugins/feature-ladder/skills/skill-selector/skill-rules.json`, the shipped defaults
2. `~/.claude/feature-ladder/skill-rules.json`, your personal rules (survive plugin updates)
3. `<repo>/.feature-ladder/skill-rules.json`, per-project rules

The shipped defaults cover debugging, TDD, planning, code review, library
docs (Context7), Figma, BigQuery/dbt/Dataform/Airflow/Spark, Python
dependencies, notebooks, ML practice, data-loss prevention, the AWS family,
Checkly, Railway, Cerebras, ComfyUI, front-end design, MCP servers and Office
files. Rules that name skills you don't have are simply skipped.
`--list` shows which ones those are.

### Try it

```
/feature-ladder:skills Fix the crash when the S3 upload times out     # dry run
/feature-ladder:skills --list                                          # what it sees, broken rules
python3 select_skills.py '{"role":"implementer","kind":"code","paths":["src/Form.tsx"],"text":"Fix crash on empty submit"}'
```

---

## Install

### Claude Code

```bash
claude plugin marketplace add jakemctigue/feature-ladder
claude plugin install feature-ladder@feature-ladder
```

Or inside a session: `/plugin marketplace add jakemctigue/feature-ladder`,
then `/plugin install feature-ladder@feature-ladder`. To check the install,
`/agents` should list the thirteen `feature-ladder:*` agents, and `/mcp` should
show `plugin:feature-ladder:hindsight`.

Requirements: Python 3 on PATH (`python3` or `python`). On Windows, Git Bash
for the hooks (Claude Code already requires it).

### Claude app

Add `claude-app/model-ladder-router/SKILL.md` as a skill on your account. At
the start of a conversation it downloads `route.py`, `intake.py`,
`pr_checkpoint.py`, `irreversible.py`, `with_secrets.py`, `verify.py`,
`external_worker.py`, `session_budget.py`, `select_skills.py`,
`skill-rules.json`, `resolve_bank.py` and `memory_gate.py` from this repo, pinned to a release
tag (currently `v1.10.0`), so the app and
Claude Code always run identical logic. The app session needs code execution
and network access to GitHub.

### Hindsight (memory)

Use [Hindsight Cloud](https://ui.hindsight.vectorize.io) or self-host. Set
these in the environment that launches Claude Code:

| Variable | Default | Meaning |
|---|---|---|
| `HINDSIGHT_MCP_URL` | `https://api.hindsight.vectorize.io/mcp/` | MCP endpoint (`http://localhost:8888/mcp/` when self-hosted) |
| `HINDSIGHT_API_KEY` | *(empty)* | Your `hsk_…` Cloud key, or the server's API key if you enabled auth |
| `HINDSIGHT_BANK_ID` | `feature-ladder` | Fallback bank for folders that aren't git repos (per-repo banks are chosen automatically) |

To self-host with the cheapest-adequate model per operation:
`cd plugins/feature-ladder/hindsight && cp hindsight.env.example hindsight.env`,
add your key, then run `./start-hindsight.sh` (Docker).
`test-hindsight.ps1` runs a save-and-recall round trip with exactly the
headers the plugin uses.

In the Claude app, Hindsight works when it is added as a connector. Without
one, the app skill skips memory and says so.

---

## Commands (Claude Code)

| Command | What it does |
|---|---|
| `/feature-ladder:feature <request> [--auto] [--second-opinion] [--escalate] [--ultra]` | Intake, then implement one feature atomically on its own branch and open its PR |
| `/feature-ladder:wrap [--review] [--adversarial] [--second-opinion]` | End of session: write the queued memories (script), and only if asked, one review over everything the session built |
| `/feature-ladder:workers [glm \| jev \| claude \| check]` | Where the cheap cards run, and a setup check |
| `/feature-ladder:scope <request>` | Intake only; the next `/feature-ladder:feature` with the same request reuses it |
| `/feature-ladder:codex-review [--adversarial]` | Codex second opinion on the commits since main, or your uncommitted work |
| `/feature-ladder:route <task or JSON>` | Dry-run the model router |
| `/feature-ladder:skills <task, JSON or --list>` | Dry-run the skill selector |
| `/feature-ladder:recall <topic>` | Ask Hindsight what it knows, without starting a run |

## Where it works

| Capability | Claude Code | Claude app |
|---|---|---|
| Deterministic model routing | ✅ plugin script | ✅ same script, fetched from this repo |
| Deterministic skill selection | ✅ enforced (`Skill` tool removed from workers) | ✅ selected and injected; enforcement by instruction |
| Named ladder agents (12 rungs + scoper) | ✅ | General-purpose subagents with role briefs |
| Hindsight memory | ✅ plugin MCP server, per-repo bank via hooks | ✅ if a Hindsight connector is added; per-repo bank via the same resolver |
| Session subagent budget | ✅ enforced by a spawn hook | ✅ `session_budget.py charge` before each subagent |
| Memory gate (scoped recall, gated writes) | ✅ enforced by the Hindsight hook | ✅ same script, run by the skill |
| GLM tier through OpenRouter | ✅ | ✅ when the Claude CLI and a key are on that machine |
| Codex review at end of session | ✅ via your local Codex CLI | ✅ when the Codex CLI is signed in there |
| Intake (ask once, up front) | ✅ scoper agent + question cards | ✅ same script; questions asked in chat |
| Branches and checkpoint PRs | ✅ plus guard hook | ✅ only when the work is a git repo with `gh` |
| Token ledger per rung | ✅ `SubagentStop` hook, table in the report | — (no hooks in the app) |

## Repository layout

```
.claude-plugin/marketplace.json        marketplace: feature-ladder
plugins/feature-ladder/
  .claude-plugin/plugin.json
  .mcp.json                            Hindsight HTTP MCP server (env-driven)
  agents/                              13 agents (12 rungs + scoper)
  commands/                            feature, wrap, scope, route, skills, recall, codex-review, workers
  skills/orchestration-ladder/         protocol + handoff contracts + pr_checkpoint.py, irreversible.py, with_secrets.py, verify.py, session_budget.py, external_worker.py
  skills/complexity-router/            route.py + intake.py
  skills/skill-selector/               select_skills.py + skill-rules.json
  skills/worker-contract/              the short contract every worker preloads instead of the full protocol
  skills/hindsight-memory/             memory rules + resolve_bank.py + memory_gate.py
  skills/cross-model-review/           codex_review.py (Codex review + adversarial tests)
  hooks/                               bank resolution and memory gate, session budget and spawn rules, retain gate, token ledger, irreversible-command guard
  hindsight/                           self-host env, start script, round-trip test
claude-app/model-ladder-router/        the Claude app skill
```

## Known limits

- The `fable` model must be available on your account for the Fable rungs. If
  it isn't, change those agents' `model:` lines.
- Claude Code documents that preloaded agent skills are separate from the
  `Skill` tool, so the protocol skills should still load with `Skill`
  disabled. Confirm this on your first run by asking an agent whether its
  orchestration protocol is in context.
- The guard hook matches command patterns; it's a safety net behind the
  agents' instructions, not a sandbox. A production write hidden inside a
  script whose name doesn't look like a migration isn't caught by it, and a
  step on a target it can't recognise is treated as production.
- Checkpoint PRs stack: merge them in the order the final report lists.
  GitHub retargets the next PR when you merge one and delete its branch.
- Subagents have a fixed startup cost. Acceptance checks therefore run through
  `verify.py` rather than a mechanic; for other very small jobs, running them
  directly can be cheaper than delegating.
- The Codex second opinion requires the Codex CLI (`npm install -g @openai/codex`)
  signed in with `codex login`. A review takes a few minutes; it is bounded by
  `FEATURE_LADDER_CODEX_TIMEOUT` (default 570 seconds, just under Claude Code's 10-minute command limit). Set
  `FEATURE_LADDER_CODEX_MODEL` / `FEATURE_LADDER_CODEX_EFFORT` to override
  Codex's model or reasoning effort.
- Gemini is deliberately not integrated through a consumer subscription:
  Google's Antigravity FAQ says wrapping its CLI in third-party coding agents
  isn't supported on consumer plans. Use a paid Gemini API key for that.
- On Hindsight Cloud, Vectorize chooses the model for Hindsight's own
  extraction. The per-operation model pinning applies to self-hosted servers.

## Evidence

Deterministic, checked-in evidence: a CycloneDX SBOM and SARIF scan in `evidence/`, golden corpora under `plugins/feature-ladder/tests/fixtures/`, a [harness compatibility matrix](docs/harness-compat.md) and [CI troubleshooting](docs/ci-troubleshooting.md). See [docs/evidence.md](docs/evidence.md); regenerate with `python plugins/feature-ladder/scripts/evidence/generate_evidence.py` after changing a script or the version.

## License

[MIT](LICENSE) © 2026 Jake McTigue
