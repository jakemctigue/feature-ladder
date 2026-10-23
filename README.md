# feature-ladder

**An orchestration ladder for adding features to your applications with Claude
subagents.** A top-tier coordinator plans and verifies, but delegates
everything it can. Each task is sent to the cheapest model that can do it
correctly and given only the skills it needs. Both choices are made by
deterministic scripts, never by a model's mood. Project memory from
[Hindsight](https://github.com/vectorize-io/hindsight) carries lessons from
one feature to the next.

It works in **Claude Code** (as a plugin) and in the **Claude app** (as a skill).
Both use the same scripts and rules, pinned to the same release tag.

---

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Install](#install)
- [Commands](#commands)
- [How a feature run flows](#how-a-feature-run-flows)
- [The ladder](#the-ladder)
- [Budgets: the session and the feature](#budgets-the-session-and-the-feature)
- [The GLM tier: GLM 5.3 Flash through OpenRouter](#the-glm-tier-glm-53-flash-through-openrouter)
- [Branches, checkpoint PRs and the merge](#branches-checkpoint-prs-and-the-merge)
- [Changes outside the repo: an audit log, not a gate](#changes-outside-the-repo-an-audit-log-not-a-gate)
- [The confidence surface](#the-confidence-surface)
- [Intake: ask once, up front](#intake-ask-once-up-front)
- [Secrets from Google Secret Manager](#secrets-from-google-secret-manager)
- [Skill selection](#skill-selection)
- [Hindsight memory, tightly controlled](#hindsight-memory-tightly-controlled)
- [Reviews and memory, once per session](#reviews-and-memory-once-per-session)
- [Token ledger](#token-ledger)
- [Evidence](#evidence)
- [Claude Code vs Claude app](#claude-code-vs-claude-app)
- [Repository layout](#repository-layout)
- [Known limits](#known-limits)
- [Releases](#releases)

---

## What it does

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
  OpenRouter (billed to OpenRouter credits, not your Claude plan). An
  OpenRouter key is part of the setup: without one every card falls back to
  your Claude plan and the ladder loses most of its savings.
- **Never waits for you.** Deploys, index builds, data steps and the merge
  happen when the feature needs them. An audit hook logs each one, labels it
  `review: pending` when it touched production or was on the former
  never-list, and the report and the PR list them for you to look at
  afterwards. There is no live-changes policy, no verdict and no approve
  step; the only command refused is one that would print a secret value.
- **Confidence instead of gates.** Before each Task Card the coordinator runs
  `confidence.py`: one Hindsight recall becomes a sentiment-scored,
  relevance-weighted surface of what the project remembers about these
  files. At or above 0.60 the card proceeds; below it the card is
  **resharded** into smaller cards. Advice about size, never a block.
- **Fable only to integrate.** The router never picks Fable and the
  coordinator runs on Opus. Fable comes in once, as the `integrator`, only
  when two or more different coding agents wrote code for one feature, with
  no coding tools. Second opinions and deep verification need an explicit
  switch.
- **Reviews and memory once per session, not per feature.** Codex review,
  adversarial tests and a Fable second opinion run only when you schedule
  them with `/feature-ladder:wrap`, once, over everything the session built.
  Memory is written at the same point, by a script, through a strict gate.
- **Hooks enforce the budget.** A spawn hook denies any subagent past the
  session budget, a second mechanic or design step, the integrator before two
  coding agents have returned, and any review, scribe or other Fable rung
  inside a run. The coordinator is capped at 24 turns.
- **Deterministic everywhere a model is not needed.** Model and effort come
  from a fixed scoring function (`route.py`), skills from a fixed rule engine
  (`select_skills.py`), acceptance from a script that re-runs your checks
  (`verify.py`), the merge from `pr_checkpoint.py`. Golden corpora under
  `tests/fixtures/` pin the behaviour of each of these scripts.

**What it does not do**

- It does not stop to ask. Nothing in a run waits for a verdict: not a
  deploy, not a data step, not the merge. What it did outside the repo is
  the first thing in the report, labelled `review: pending`.
- It does not print secret values. A command whose output would put one in
  the transcript is the one thing the audit hook refuses; workers use
  `with_secrets.py`.
- It does not write undo scripts, backups or rehearsals for live changes.
  Tokens go to the feature; the services' own safeguards are relied on.
- It does not invent a design when the request is ambiguous. It asks at
  intake, or escalates to a design step if you'd rather it chose.
- It does not replace your tests. "Done" means your checks passed, quoted
  verbatim.

---

## Requirements

| Need | Why | Without it |
|---|---|---|
| **Python 3** on PATH (`python3` or `python`) | Every script in the ladder is Python, stdlib only | Nothing runs |
| **Git Bash** on Windows | The hooks are shell scripts (Claude Code already requires it) | Hooks don't fire |
| **An [OpenRouter](https://openrouter.ai) account** with `OPENROUTER_API_KEY` in the environment or in Google Secret Manager, and a few dollars of credit | The GLM tier that makes the ladder cheap runs through it; it is the only Anthropic-compatible endpoint a Claude Code child process can point at | Every card bills to your Claude plan |
| `gh` CLI, signed in, and a remote | Checkpoint PRs, the final PR, labels and the merge | Same branches made locally, PR bodies saved to `.feature-ladder/checkpoints/`, merged into your base branch locally |
| [Hindsight](https://ui.hindsight.vectorize.io) (Cloud key, or self-hosted) | Project memory and the confidence surface. Cloud runs under about $3 a month and does Hindsight's own extraction and summary inference on its side, so it is worth it for that offload alone; self-hosting works but you pay that inference yourself | Runs proceed with an empty recall and the size prior alone |
| Codex CLI (`npm install -g @openai/codex`, `codex login`) | The once-per-session cross-model review | `wrap --review` says it was skipped |
| `gcloud` signed in | `with_secrets.py` fetches secret values for the one command that needs them | Workers cannot run commands that need a secret |
| The `fable` model on your account | The integrator and the opt-in Fable rungs | Change those agents' `model:` lines |

Confirm the OpenRouter side with `/feature-ladder:workers check --probe`: it
validates the key, shows remaining credit and pings each model.

---

## Install

### Claude Code

```bash
claude plugin marketplace add jakemctigue/feature-ladder
claude plugin install feature-ladder@feature-ladder
```

Or inside a session: `/plugin marketplace add jakemctigue/feature-ladder`,
then `/plugin install feature-ladder@feature-ladder`. To check the install,
`/agents` should list the fourteen `feature-ladder:*` agents, and `/mcp` should
show `plugin:feature-ladder:hindsight`.

### Claude app

Add `claude-app/model-ladder-router/SKILL.md` as a skill on your account. At
the start of a conversation it downloads `route.py`, `intake.py`,
`pr_checkpoint.py`, `irreversible.py`, `with_secrets.py`, `verify.py`,
`confidence.py`, `external_worker.py`, `session_budget.py`, `select_skills.py`,
`skill-rules.json`, `resolve_bank.py` and `memory_gate.py` from this repo,
pinned to a release tag (currently `v1.13.0`), so the app and Claude Code
always run identical logic. The app session needs code execution and network
access to GitHub.

### Hindsight (memory)

Use [Hindsight Cloud](https://ui.hindsight.vectorize.io) or self-host. Cloud
is the default recommendation: it costs under about $3 a month and runs
Hindsight's own extraction and summary inference on its side, which is the
part you would otherwise pay for in tokens. Set these in the environment that
launches Claude Code:

| Variable | Default | Meaning |
|---|---|---|
| `HINDSIGHT_MCP_URL` | `https://api.hindsight.vectorize.io/mcp/` | MCP endpoint (`http://localhost:8888/mcp/` when self-hosted) |
| `HINDSIGHT_API_KEY` | *(empty)* | Your `hsk_…` Cloud key, or the server's API key if you enabled auth |
| `HINDSIGHT_BANK_ID` | `feature-ladder` | Fallback bank for folders that aren't git repos (per-repo banks are chosen automatically) |

To self-host with the cheapest-adequate model per operation:
`cd plugins/feature-ladder/hindsight && cp hindsight.env.example hindsight.env`,
fill in the keys, then `./start.sh` and `python3 roundtrip_test.py`.

---

## Commands

| Command | What it does |
|---|---|
| `/feature-ladder:feature <request> [--auto] [--no-merge] [--second-opinion] [--escalate] [--ultra]` | Intake, questions, then one atomic run to a merged PR |
| `/feature-ladder:scope <request>` | Intake alone; the next `feature` with the same text reuses it without asking |
| `/feature-ladder:wrap [--review] [--adversarial] [--second-opinion]` | End of session: optional reviews over the whole session's diff, then the memory flush |
| `/feature-ladder:route <task or JSON>` | Dry-run the router: which rung, why |
| `/feature-ladder:skills <task> [--list]` | Dry-run skill selection; `--list` shows installed skills and broken rules |
| `/feature-ladder:recall <query>` | One gated Hindsight recall for this repo |
| `/feature-ladder:codex-review [--adversarial]` | A Codex review of any branch or uncommitted work, on demand |
| `/feature-ladder:workers [glm \| jev \| claude \| check [--probe]]` | Where the cheap cards run, and a setup check |

Switches on `feature`: `--auto` asks nothing and takes the scout's values and
the recommended options; `--no-merge` leaves the PR stack open;
`--second-opinion` allows one Fable answer to one sharp question;
`--escalate` allows the Fable debugging rungs; `--ultra` also allows the
ultra-investigator. Reviews are not feature switches: they belong to `wrap`.

---

## How a feature run flows

0. **Intake.** The scoper scans the repo and reads the code the feature will
   touch, and proposes a value, confidence and evidence for every routing
   variable. `intake.py decide` picks the questions: the session's subagent
   budget (first feature only), uncertain variables whose answer changes the
   model, and open design decisions. You answer once; the confirmed intake
   is binding. The run moves to `feature-ladder/<slug>`.
1. **Recall once.** One Hindsight recall, scoped by a hook to this repo's
   bank and capped at 1000 tokens, saved to the run folder; only the snippets
   that bear on the card go into it.
2. **Confidence, then one Task Card.** `confidence.py card` scores the recall
   against the card's files: at or above 0.60 the card proceeds, below it the
   card is resharded into smaller ones (once). `intake.py card` routes each
   card and picks its skills in one call. A hard flag adds one design step
   before the worker; nothing else.
3. **One worker.** GLM 5.3 Flash or Sonnet medium does the change and its
   tests and returns an *Evidence Packet* with real check output. A GLM card
   is prepared first: `external_worker.py` fills its `Memory:` line from the
   recall, adds a `Bounds:` line and puts the packet template on the card.
4. **Integrate, only for multi-agent work.** If two or more different coding
   agents wrote code for the feature, the Fable `integrator` reads the packets
   and the combined diff (`git add -N . && git diff <base>`, so uncommitted
   and untracked work and every earlier checkpoint are in it), names the
   seams, and returns one integration card or `coherent`. One worker's work
   skips this step.
5. **Verify with no model.** `verify.py` re-runs the checks and flags files
   outside the card's scope. A failure is re-dispatched once, then one band
   up once, then reported `partial`.
6. **Accept and merge.** The coordinator queues the packet's memory
   candidates (through the memory gate), writes `FEATURE-REPORT.md` with the
   confidence lines, the `review: pending` list and the token table, and
   `pr_checkpoint.py finish` opens the PR, labels the stack and merges every
   entry into your base branch.

Throughout, nothing stops for you. Non-critical choices are decided and
listed under Assumptions. A **critical decision** is decided with the
recommended option and parked in a draft PR that explains how to switch.

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
| **C** | **`coordinator`** | **Opus 5.5 / medium** | **Confidence, card, dispatch, verify.py, accept, merge: 12-24 turns** |
| **FI** | **`integrator`** | **Fable 5.1 / medium** | **Once per feature, only after two or more different coding agents wrote code; no coding tools** |
| F-Low | `second-opinion` | Fable 5.1 / low | Only with `--second-opinion` |
| F-High | `deep-verifier` | Fable 5.1 / high | Only with `--escalate` |
| F-Extra | `architect-escalation` | Fable 5.1 / xhigh | Only with `--escalate` |
| F-Ultra | `ultra-investigator` | Fable 5.1 / max | Only with `--ultra` |
| X | `codex-reviewer` | Haiku / low driving **OpenAI Codex** | Only at `/feature-ladder:wrap --review`, once per session |

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
`route.py --table` for the full table. `tests/fixtures/routing/golden_cases.json`
pins 23 routes and budgets; change the router and the golden tells you.

---

## Budgets: the session and the feature

Cost scales with agents × turns × context, because every turn of every agent
re-reads its whole context. Two budgets cap it, both enforced by hooks:

**Session** (asked once per coding session): Small = 2 subagents, Medium = 4,
Large = 8. The intake scout and the coordinator aren't counted; every
worker, expert, reviewer and GLM worker is. A new Claude Code session asks
again; `session_budget.py set --size L` raises it mid-session.

**Feature** (from the score):

| Size | Score | Workers | Coordinator turns |
|---|---|---|---|
| S | 0–9 | 1 | ~12 |
| M | 10–19 | 1, or 2 for two independent file sets | ~20 |
| L | 20+ | up to 4 | ~24 (hard cap at every size) |

Every size: at most one mechanic, a design step only on a hard flag, no Codex,
no scribe, no check-runner, and no Fable rung but the integrator (once, after
two coding agents; it is not counted as a worker).

---

## The GLM tier: GLM 5.3 Flash through OpenRouter

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

**Why OpenRouter is required.** The ladder's cost model assumes the bottom
two rungs are cheap: every mechanic card and every small implementer card
is meant to cost cents, not Claude plan tokens. Only OpenRouter gives a
Claude Code child process a GLM (or `jev-router`) endpoint with the
Anthropic-compatible API that `claude -p` speaks, so it is the one external
account the plugin needs. Treat `OPENROUTER_API_KEY` as a requirement rather
than an option: set it in the environment or in Google Secret Manager before
the first run, keep a few dollars of credit on it, and confirm with
`/feature-ladder:workers check --probe`. The fallback to Claude exists so a
run never fails for lack of a key, not so you can skip it.

**Other providers.** In the Claude app, which only needs a chat endpoint for
the cheap rungs, a [Together.ai](https://www.together.ai) or
[OpenHands](https://www.all-hands.dev) key would probably serve in
OpenRouter's place. Neither has the Claude Code and Codex CLI integrations
that the Claude Code harness relies on (the child `claude -p` process, the
Codex review at wrap), so they are untested here and not a substitute for
the plugin.

**What the runner hands the cheap model.** `external_worker.py run`
prepares the card before the child gets it, because a small model follows
what is on the card and little else. It fills a `Memory:` line from the
feature's saved recall (the same facts `confidence.py` scored, scope-first,
at most five), adds a `Bounds:` line (max turns, timeout, edit only the
`Scope:`, how much session budget is left) and puts the Evidence Packet
template on the card itself instead of only in the system prompt. A
coordinator's own `Memory:` line is kept as written. The prepared card and
what was added are stored in `.feature-ladder/evidence/<id>-external.json`.

| Setting | Cheap cards run on |
|---|---|
| `/feature-ladder:workers glm` (default) | `z-ai/glm-5.3-flash:nitro` |
| `/feature-ladder:workers jev` | `typesafe/jev-router` (picks a model and effort per request; variable price) |
| `/feature-ladder:workers claude` | Haiku / Sonnet-medium subagents |

Everything else (Sonnet medium by default, Opus, the coordinator) stays on
Claude. External workers count against the session budget like any other.
Usage lands in the ledger with what OpenRouter actually charged.

The child process is Claude Code itself, so OpenRouter's documented Claude
Code integration applies ([their cookbook](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration.md)).
Their API is a single stable `v1` with changes announced in the changelog,
so nothing is pinned ([versioning](https://openrouter.ai/docs/api_reference/versioning)).

---

## Branches, checkpoint PRs and the merge

`skills/orchestration-ladder/scripts/pr_checkpoint.py` handles the branches.

| Step | What it does |
|---|---|
| `start` | Creates `feature-ladder/<slug>` from your branch (uncommitted changes committed first or stashed, as you chose), records the budget and switches, and turns on the audit hook for this repo |
| `checkpoint` | Commits, pushes and opens a **draft PR** saying what was assumed and how to switch, then continues on `feature-ladder/<slug>-pt2` stacked on it |
| `finish` | Opens the final PR on top of the stack, labels every PR `review: pending`, merges the stack and prints each result; `--no-merge` leaves it open |
| `abort` | Turns the hook off after a run died midway; branches and commits stay |

**Every entry lands on your base branch.** A stacked checkpoint PR is opened
against the previous feature branch so that its diff is readable, but
`finish` merges each entry, in order, into the *original* base recorded at
`start`: a stacked PR is retargeted with `gh pr edit --base` first, then
marked ready and merged. With `main <- feature/a <- feature/b`, `main` ends
up with both. If GitHub can only queue an entry (auto-merge when checks
pass), the later entries are still retargeted and merged and the queued one
is listed as pending. If GitHub refuses an entry outright (branch
protection, a conflict), `finish` reports which and stops there; nothing is
forced. A test builds a real two-checkpoint stack and asserts the base
contains the final entry's changes.

Without a remote or the `gh` CLI, the same branches are made locally, each
PR description is saved in `.feature-ladder/checkpoints/`, and `finish`
merges each entry into your base branch locally with `--no-ff`.

---

## Changes outside the repo: an audit log, not a gate

Code on a branch is always reversible, so `irreversible` only means effects
outside the repo (in routing it is a +3 risk weight, not a stop). Those steps
are run when the feature needs them, never parked.
`skills/orchestration-ladder/scripts/irreversible.py` classifies every shell
command during a run, with no model tokens, and the `PreToolUse` hook records
the result in `run.json`:

| Tier | Examples | What happens |
|---|---|---|
| **mundane** | emulator, dev or staging targets; `firebase deploy`; index builds; exports; dry runs; anything that doesn't destroy production data | runs, logged |
| **risky** | `firestore:delete`, `firestore import`, destructive SQL, migration/backfill scripts against production | runs, logged `review: pending` |
| **never** | the former never-list: merging PRs; pushing or force-pushing your base branch; deleting a project, database, repo or backups; weakening database protection; `rm -rf` of `/` or your home folder | runs, logged `review: pending`, named first in the report and the PR |
| **secret** | `gcloud secrets versions access`, `cat .env`, `printenv`, `echo $X_KEY` | **refused** - the one denial; use `with_secrets.py` |

Production is recognised from `.firebaserc` aliases, `--project` flags and
words like `prod`/`staging` in the command; an unknown target on a real cloud
tool is treated as production. Only the executable part of a command is
classified (quoted strings, heredocs and comments are stripped), so a commit
message or a `grep` that mentions `DROP TABLE` is not a command. Code a shell
still runs is classified too: `bash -c '...'`, `eval`, a heredoc fed to a
shell, `$(...)`, backticks, and the command after `--` in `with_secrets.py`.
Every action is listed in a table in `FEATURE-REPORT.md` with its review
status; `irreversible.py pending` prints the `review: pending` rows as a
checklist and `finish` puts the same list in the PR body.

---

## The confidence surface

What the gates used to decide is decided from the project's own memory.
`skills/orchestration-ladder/scripts/confidence.py` takes the feature's
Hindsight recall and the card (or the command) and returns one number:

- **An emotionally textured surface.** Every remembered fact is scored for
  sentiment with a lexicon (no tokens) or, with `--scorer jev`, by Jev
  through OpenRouter: a `[pitfall]` reads as a warning, a `[convention]` or
  `[test-strategy]` as steady ground, and the words in the fact ("timed out",
  "passes green") move it further. Each fact is weighted by how closely it
  touches the card's files and commands. The result has a mood: `unknown`,
  `wary`, `steady` or `confident`, and a one-line texture that quotes the
  facts that weighed most.
- **One number.** A prior from the card's size (S 0.75, M 0.65, L 0.55) or
  the command's tier (mundane 0.72, risky 0.55, never 0.50), plus a quarter of
  the surface's sentiment, minus a little for each relevant pitfall, plus or
  minus the checks that have passed or failed so far. Clamped to [0.05, 0.98].
- **Two outcomes, no block.** At or above `0.60`
  (`FEATURE_LADDER_CONFIDENCE_THRESHOLD`) the card proceeds. Below it the
  card is **resharded**: split into the number of smaller cards it names, by
  independent file sets, one concern each, with a fresh score per shard
  (once per feature, then proceed and record the number). For a command,
  "reshard" means run its preview or dry-run form first when the tool has
  one, then the real one. Everything is written next to the action in
  `FEATURE-REPORT.md`.
- **Reads the recall as the MCP server returns it.** The coordinator saves
  the recall tool result verbatim. `confidence.py` decodes MCP envelopes at
  any depth (a `result` JSON string, or JSON inside `content[].text`), so
  each memory is scored on its own with its tag intact rather than the whole
  response counting as one note. `tests/fixtures/mcp_recall_envelopes.json`
  holds both shapes.

`confidence.py card --action "<title>" --paths <files> --size S --memories recall.json`
is the call; `--live --query "<text>"` lets it recall on its own through the
same gate. `confidence.py score "<text>"` shows the lexicon's view of a line.

---

## Intake: ask once, up front

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
  own commit, stash them, or stop. There is no live-changes question: what
  the feature needs outside the repo runs and is logged for review.

`--auto` asks nothing and takes the scout's values and recommended options.
`/feature-ladder:scope` runs intake alone and saves it; the next
`/feature-ladder:feature` with the same request text reuses it without asking.

---

## Secrets from Google Secret Manager

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
command's environment and masks it in the output. The audit hook denies
anything that would print a value into the transcript; that is its only
denial. Deleting, disabling, re-sharing or creating a secret runs and is
logged `review: pending`. Values never reach a report, PR, commit, memory or
Codex prompt. The hook classifies the command after `--` exactly as if it
ran on its own, so a production step inside the wrapper is logged like any
other.

---

## Skill selection

A subagent normally starts with the description of every installed skill and
can load any of them. With hundreds of skills installed, that wastes tokens
and invites the wrong skill. feature-ladder makes skill choice explicit,
repeatable and auditable.

1. After routing, the coordinator runs
   `skills/skill-selector/scripts/select_skills.py` with the task's `role`
   (from the router), `kind`, `paths` (the card's scope), `text` (goal and
   description) and router `flags`.
2. The script scans the skills installed on that machine (personal skills,
   project `.claude/skills`, installed plugins, and in the Claude app the
   synced account and plugin skills) and applies the rules in
   `skill-rules.json`.
3. It returns at most `max_skills` skills (default 3) within a character
   budget of skill text: `max_chars_by_role` per router role (shipped
   defaults: mechanic 0, implementer 8,000, senior implementer and domain
   experts 30,000), else `max_chars` (default 20,000). Anything cut is
   reported, never silently included.
4. `--inject --out .feature-ladder/cards/<task>-skills.md` writes those
   skills' text to a file only the worker reads, and prints the exact skill
   names and files it used. The decision never changes between runs for the
   same input; `tests/fixtures/skill_ranking/cases.json` pins 15 rankings.

### Rules

Rules are merged in this order; a later file replaces a rule with the same
`id`, and `"disable": ["id"]` removes one:

1. `plugins/feature-ladder/skills/skill-selector/skill-rules.json`, the shipped defaults
2. `~/.claude/feature-ladder/skill-rules.json`, your personal rules (survive plugin updates)
3. `<repo>/.feature-ladder/skill-rules.json`, per-project rules

The shipped defaults cover debugging, TDD, planning, code review, library
docs (Context7), Figma, BigQuery/dbt/Dataform/Airflow/Spark, Python
dependencies, notebooks, ML practice, data-loss prevention, the AWS family,
Checkly, Railway, Cerebras, ComfyUI, front-end design, MCP servers and Office
files. Rules that name skills you don't have are simply skipped;
`--list` shows which ones those are.

```
/feature-ladder:skills Fix the crash when the S3 upload times out     # dry run
/feature-ladder:skills --list                                          # what it sees, broken rules
python3 select_skills.py '{"role":"implementer","kind":"code","paths":["src/Form.tsx"],"text":"Fix crash on empty submit"}'
```

---

## Hindsight memory, tightly controlled

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
- **Read twice, written once:** the coordinator's recall is saved to the run
  folder and feeds `confidence.py` and the GLM card's `Memory:` line, so the
  same memories that go on the card also size it.
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

---

## Reviews and memory, once per session

A different model family catches different mistakes, but paying for it on
every small feature is what burned the budget. `/feature-ladder:wrap` is the
end-of-session step: it flushes the memory queue, closes the session budget
and prints the ledger. Reviews run only there, once, over the whole session's
diff, and only when asked:

- **`--review`:** Codex, read-only sandbox, through the official Codex CLI and
  your own Codex sign-in. Structured findings (severity, file, line, why,
  fix, repro); the script drops `low` findings and duplicates with no model
  tokens, and a `git status` check flags anything touched outside the review
  folder (a background indexer writing its own database, for instance, is
  reported but is not Codex's doing).
- **`--adversarial`:** Codex writes attack tests only under
  `.feature-ladder/adversarial/<slug>/` and runs them.
- **`--second-opinion`:** one Fable answer to the single most consequential
  open point.
- **Report, don't loop:** findings are listed with their repro commands and a
  verification card; each confirmed bug becomes its own
  `/feature-ladder:feature` request. No fix rounds, no re-review.

Records are saved to `.feature-ladder/reviews/`. On demand, for any branch or
uncommitted work: `/feature-ladder:codex-review [--adversarial]`.

---

## Token ledger

Every run records what each rung cost, with no model tokens spent. When a
subagent stops, the `SubagentStop` hook (`hooks/scripts/ledger.py`) reads its
transcript, sums input, cache-read, cache-write and output tokens, parses the
rung, model, effort and task id from its Task Card, and appends one line to
`.feature-ladder/runs/<slug>/usage.jsonl`. `ledger.py report` prints a table
by rung and model. The coordinator pastes it into `FEATURE-REPORT.md` at
acceptance, and `/feature-ladder:feature` prints the complete table after the
coordinator has finished, so the coordinator's own cost is included. The
ledger is what the router bands, skill budgets and rung choices get tuned
against; without it those are guesses.

---

## Evidence

Everything that can be checked without a model is checked in and pinned:

| Evidence | Where | Catches |
|---|---|---|
| Routing goldens (23 cases over `route.py` and the budget sizer) | `tests/fixtures/routing/` | A rung or budget silently moving |
| Skill-ranking fixtures (15 cases) | `tests/fixtures/skill_ranking/` | A rule change reordering what a worker is given |
| `pr_checkpoint` cases and a real two-checkpoint stack test | `tests/fixtures/pr_checkpoint/`, `tests/test_pr_checkpoint.py` | A merge that misses the base branch |
| Codex triage corpus (12 cases) | `tests/fixtures/codex_triage/` | Findings dropped or kept wrongly |
| MCP recall envelopes | `tests/fixtures/mcp_recall_envelopes.json` | The recall parser regressing to one note |
| CycloneDX 1.5 SBOM and SARIF 2.1.0 scan | `evidence/` | Any script or version drift: CI runs `generate_evidence.py --check` |
| Synthetic CI failure logs and a classifier | `tests/fixtures/ci_logs/`, `scripts/evidence/ci_log_classifier.py`, [docs/ci-troubleshooting.md](docs/ci-troubleshooting.md) | A workflow failure nobody can read |
| Harness compatibility matrix and manifest test | [docs/harness-compat.md](docs/harness-compat.md), `tests/test_harness_manifest.py` | A broken `plugin.json`, `.mcp.json` or hook path |

The SBOM records each script's hash, so after changing a script or the
version run `python plugins/feature-ladder/scripts/evidence/generate_evidence.py`
and commit `evidence/`. See [docs/evidence.md](docs/evidence.md). Everything
here is stdlib Python; nothing calls a model.

---

## Claude Code vs Claude app

| | Claude Code plugin | Claude app skill |
|---|---|---|
| Deterministic routing (`route.py`) | ✅ | ✅ same script |
| Deterministic skill selection (`select_skills.py`) | ✅ | ✅ same script, same rules |
| Intake (ask once, up front) | ✅ scoper agent + question cards | ✅ same script; questions asked in chat |
| Confidence surface (recall -> sentiment -> proceed / reshard) | ✅ `confidence.py` | ✅ same script |
| Session subagent budget | ✅ enforced by a spawn hook | ✅ `session_budget.py charge` before each subagent |
| GLM tier through OpenRouter | ✅ | ✅ when the Claude CLI and a key are on that machine |
| Hindsight memory | ✅ plugin MCP server, per-repo bank via hooks | ✅ if a Hindsight connector is added; per-repo bank via the same resolver |
| Memory gate (scoped recall, gated writes) | ✅ enforced by the Hindsight hook | ✅ same script, run by the skill |
| Branches, checkpoint PRs, merge into the base with `review: pending` | ✅ plus audit hook | ✅ only when the work is a git repo with `gh` |
| Codex review at end of session | ✅ via your local Codex CLI | ✅ when the Codex CLI is signed in there |
| Token ledger per rung | ✅ `SubagentStop` hook, table in the report | — (no hooks in the app) |

---

## Repository layout

```
.claude-plugin/marketplace.json        marketplace: feature-ladder
.github/workflows/tests.yml            unittest on ubuntu + windows, then generate_evidence.py --check
evidence/                              sbom.cdx.json, findings.sarif (regenerated, checked in)
docs/                                  evidence.md, harness-compat.md, ci-troubleshooting.md
plugins/feature-ladder/
  .claude-plugin/plugin.json
  .mcp.json                            Hindsight HTTP MCP server (env-driven)
  agents/                              14 agents (13 rungs + scoper)
  commands/                            feature, wrap, scope, route, skills, recall, codex-review, workers
  skills/orchestration-ladder/         protocol + handoff contracts + pr_checkpoint.py, irreversible.py, confidence.py, with_secrets.py, verify.py, session_budget.py, external_worker.py
  skills/complexity-router/            route.py + intake.py
  skills/skill-selector/               select_skills.py + skill-rules.json
  skills/worker-contract/              the short contract every worker preloads instead of the full protocol
  skills/hindsight-memory/             memory rules + resolve_bank.py + memory_gate.py
  skills/cross-model-review/           codex_review.py (Codex review + adversarial tests)
  scripts/evidence/                    generate_evidence.py (SBOM + SARIF), ci_log_classifier.py
  tests/                               unittest suite; fixtures/ holds the golden corpora
  hooks/                               bank resolution and memory gate, session budget and spawn rules, retain gate, token ledger, outside-repo audit log
  hindsight/                           self-host env, start script, round-trip test
claude-app/model-ladder-router/        the Claude app skill
```

Run the whole suite with
`python -m unittest discover -s plugins/feature-ladder/tests`.

---

## Known limits

- The `fable` model must be available on your account for the Fable rungs. If
  it isn't, change those agents' `model:` lines.
- Claude Code documents that preloaded agent skills are separate from the
  `Skill` tool, so the protocol skills should still load with `Skill`
  disabled. Confirm this on your first run by asking an agent whether its
  orchestration protocol is in context.
- The audit hook matches command patterns; it is a log, not a sandbox. A
  production write hidden inside a script whose name doesn't look like a
  migration isn't labelled by it, and a step on a target it can't recognise
  is labelled as production. The `review: pending` list is only as complete
  as those patterns.
- `finish` merges every stack entry into your base branch and retargets
  stacked PRs on the way. The GitHub retarget path is covered by a test
  against a mocked `gh`, not a live repository. If an earlier PR is only
  queued and GitHub then refuses a later one (checks still pending, branch
  protection, a conflict), the report says which and the rest stays open;
  merge or fix it yourself.
- Subagents have a fixed startup cost. Acceptance checks therefore run through
  `verify.py` rather than a mechanic; for other very small jobs, running them
  directly can be cheaper than delegating.
- The Codex second opinion requires the Codex CLI (`npm install -g @openai/codex`)
  signed in with `codex login`. A review takes a few minutes; it is bounded by
  `FEATURE_LADDER_CODEX_TIMEOUT` (default 570 seconds, just under Claude Code's
  10-minute command limit). Set `FEATURE_LADDER_CODEX_MODEL` /
  `FEATURE_LADDER_CODEX_EFFORT` to override Codex's model or reasoning effort.
- Only Claude Code and the Claude app are supported harnesses. Codex,
  OpenCode, Zed, dmux and generic agent surfaces are listed as untested in
  [docs/harness-compat.md](docs/harness-compat.md).
- Gemini is deliberately not integrated through a consumer subscription:
  Google's Antigravity FAQ says wrapping its CLI in third-party coding agents
  isn't supported on consumer plans. Use a paid Gemini API key for that.
- On Hindsight Cloud, Vectorize chooses the model for Hindsight's own
  extraction. The per-operation model pinning applies to self-hosted servers.

---

## Releases

| Version | What changed |
|---|---|
| **v1.13.0** | `finish` merges every stack entry into the original base and retargets stacked PRs; the integrator's diff includes uncommitted, untracked and earlier-checkpoint work; `confidence.py` decodes MCP recall envelopes so each memory keeps its tag; README rewritten around requirements and evidence |
| v1.12.0 | OpenRouter cards carry `Memory:`, `Bounds:` and the output contract; deterministic evidence corpora, SBOM/SARIF with a CI check, CI failure logs, harness matrix; OpenRouter documented as a requirement |
| v1.11.0 | Ungated runs, the confidence surface, Fable only as integrator |
| v1.10.0 | Atomic feature runs, session subagent budget, GLM by default, gated memory |
| v1.9.0 | GLM 5.3 Flash worker rung through OpenRouter |

Full notes on each [GitHub release](https://github.com/jakemctigue/feature-ladder/releases).

## License

[MIT](LICENSE) © 2026 Jake McTigue
