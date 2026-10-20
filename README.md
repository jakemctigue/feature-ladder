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
  and runs it end to end in your repo: recall memory, scope, decompose into
  task cards, design where needed, implement, verify, accept, remember.
- **Asks first, then finishes.** Before anything is delegated, a Sonnet scout
  works out every routing variable from your code, with evidence and a
  confidence level. You're asked only about the ones that are uncertain *and*
  would change which model does the work, plus the design choices a worker
  would otherwise guess. After that the run goes to completion without
  stopping.
- **Works on its own branch and finishes the job.** A critical design decision
  that comes up mid-run becomes a draft PR with the assumption, and work
  continues on a stacked branch. Changes outside the repo (Firestore or
  database writes, migrations, deploys) are run too, under a policy you pick
  once at intake: routine ones at once, production data overwrites after one
  Fable verdict, and a short never-list left for you.
- Decides **which model and effort level** handles each task with a fixed
  scoring function (`route.py`), from Haiku low for mechanical edits up to
  Opus high for hard, designed work.
- Decides **which skills** each worker gets with a fixed rule engine
  (`select_skills.py`), and in Claude Code physically prevents workers from
  loading any others.
- Keeps the expensive model (Fable 5.1) on a short leash: at most three
  inspection passes per feature, and one-step escalations with a written
  justification.
- Requires evidence: every worker returns real command output for its checks,
  and acceptance checks are re-run by a separate cheap worker, not by the author.
- Records durable lessons (decisions, pitfalls, interfaces, test gates) in
  Hindsight after each task and each feature, using the cheapest model that
  can summarize correctly.

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
| T0 | `mechanic` | Haiku / low | Mechanical patches, docs drafts, fixtures, executing defined checks |
| T1 | `evidence-collector` | Haiku / medium | Collect current sources, extract claims, prepare evidence |
| T2–T3 | `implementer` | Sonnet 5.5 / medium–high | Bounded coding, regression tests, domain analysis |
| T4–T5 | `senior-implementer` | Opus 5.5 / medium–high | Difficult implementation under an agreed design |
| D | `domain-expert`, `domain-expert-high` | Opus 5.5 / medium–high | Design: options, trade-offs, interfaces, test plan |
| M | `memory-scribe` | Haiku / low | Writes Hindsight memories for the expensive rungs |
| F-Low | `second-opinion` | Fable 5.1 / low | Short, focused second opinions that save a larger handoff |
| **F-Med** | **`coordinator`** | **Fable 5.1 / medium** | **Plan, direct workers, fact-check, inspect evidence, accept** |
| F-High | `deep-verifier` | Fable 5.1 / high | Subtle bugs, conflicting evidence, cross-system correctness |
| F-Extra | `architect-escalation` | Fable 5.1 / xhigh | Architecture or systemic problems unresolved after High |
| F-Ultra | `ultra-investigator` | Fable 5.1 / max | Optional deep investigations (opt-in with `--ultra`) |
| X | `codex-reviewer` | Haiku / low driving **OpenAI Codex** | Cross-model second opinion on every feature; adversarial tests with `--adversarial` |

### How a feature run flows

0. **Intake.** The scoper scans the repo and reads the code the feature will
   touch, and proposes a value, confidence and evidence for every routing
   variable. `intake.py decide` picks the questions: only uncertain variables
   whose answer changes the model, plus open design decisions. You answer
   once; the confirmed intake is binding. The run then moves to its own
   branch, `feature-ladder/<slug>`.
1. **Recall.** The coordinator asks Hindsight for this project's conventions,
   past decisions and pitfalls in the area.
2. **Scope.** It reads only what it needs, and writes `FEATURE-REPORT.md`
   with the goal, acceptance criteria and out-of-scope items.
3. **Decompose and route.** Each task card is scored by `route.py`. The score
   picks the rung. Hard flags (ambiguous, cross-system plus security,
   whole-app blast radius) force escalation instead.
4. **Select skills.** `select_skills.py` picks the card's skills and their
   full text is pasted into the card.
5. **Design if needed.** Ambiguous or novel work goes to a domain expert
   first. Opus implementation is refused until a design is accepted.
6. **Execute.** Workers run in parallel where independent, each returning an
   *Evidence Packet*: files changed, checks run with real output, claims and
   how they were verified.
7. **Verify.** A separate mechanic re-runs the "done when" checks. The
   coordinator spot-checks claims against the diff.
8. **Get a second opinion from another model.** OpenAI Codex reviews the
   whole feature diff (read-only). With `--adversarial` it also writes tests
   that try to break the change and runs them. Real problems go back through
   the router as fix tasks; disputed findings are settled by a short Fable
   second opinion.
9. **Escalate only when warranted.** One rung at a time, each climb justified
   in the report.
10. **Accept and remember.** The coordinator finishes the report, the
   durable facts go to Hindsight, and the final PR opens on top of any
   checkpoint PRs.

Throughout, nothing stops for you. Non-critical choices are decided and
listed under Assumptions. A **critical decision** (costly to undo, could go
either way) is decided with the recommended option and parked in a draft PR
that explains how to switch, and work continues on the next stacked branch.
Steps with effects outside the repo run under the intake policy (next
section); none of them stops the run.

---

## Features

### Deterministic model routing

`skills/complexity-router/scripts/route.py` is a pure function: task facts
in, rung out. Points come from files, lines changed, modules touched, new
dependencies, public API changes, persistence, security, concurrency,
cross-system work, missing tests, ambiguity, novelty and blast radius. Score
bands map to Haiku low (0–2), Sonnet medium (3–6), Sonnet high (7–9), Opus
medium (10–13), Opus high (14–17), and escalate (18+). Run
`route.py --table` for the full table. Workers never choose their own model.

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

Those are the only switches: `--auto`, `--adversarial` (attack tests; on by
itself when the intake says the feature touches security) and `--ultra`.
The branch, PR availability and review scope are detected.

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

### Changes outside the repo

Code on a branch is always reversible, so `irreversible` only means effects
outside the repo (in routing it is a +3 risk weight, not a stop). Those steps
are run, not parked. `skills/orchestration-ladder/scripts/irreversible.py`
classifies every shell command during a run, with no model tokens, and the
`PreToolUse` guard applies the result:

| Tier | Examples | What happens |
|---|---|---|
| **mundane** | emulator, dev or staging targets; `firebase deploy`; index builds; exports; dry runs; anything that doesn't destroy production data | runs at once, logged |
| **risky** | `firestore:delete`, `firestore import`, destructive SQL, migration/backfill scripts against production | denied once; the coordinator gets one `second-opinion` (Fable low) verdict on the exact command, approves it with `irreversible.py approve`, and it runs |
| **never** | deleting a project, database, repo or backups; weakening database protection; merging PRs; pushing to your base branch; force-pushing or deleting `main` | left for you, logged |

The intake policy narrows this: *non-production only* leaves every production
action for you; *nothing outside the repo* leaves all of them. Production is
recognised from `.firebaserc` aliases, `--project` flags and words like
`prod`/`staging` in the command; an unknown target on a real cloud tool is
treated as production. Only the executable part of a command is classified
(quoted strings, heredocs and comments are stripped), so a commit message or a
`grep` that mentions `DROP TABLE` is not a command. Every action, approved,
rejected or left for you, is listed in a table in `FEATURE-REPORT.md`.

### Deterministic skill selection

See [Skill selection](#skill-selection) below.

### Hindsight memory

- **Recall before work:** the coordinator and every worker query project memory first.
- **Retain when each subagent finishes:** every rung ends its report with a
  `Retained:` line. A `SubagentStop` hook sends a worker back once if that
  line is missing.
- **Cheapest model writes memories:** Haiku and Sonnet workers write their own
  1–3 facts. Fable and Opus rungs hand a raw list to `memory-scribe`
  (Haiku low) instead of spending their own tokens. A sample self-hosted
  Hindsight config pins Hindsight's own extraction and consolidation to Haiku.
- **Tagged and scoped:** memories are tagged `project:<name>`,
  `feature:<slug>` and a kind (`decision`, `pitfall`, `interface`,
  `test-strategy`, `convention`, `outcome`). Secrets and personal data are
  never stored.
- **One bank per repository, found or created deterministically:** at session
  start, `resolve_bank.py` identifies the repo (main worktree folder name plus
  the `origin` owner/repo). It uses a bank pinned in
  `.feature-ladder/hindsight.json` if there is one. Otherwise it reuses the first
  bank that already exists among `coding-agent::<project>` (the naming used by
  Hindsight's own coding-agents integration), `<owner>-<repo>` and `<project>`.
  Only if none exists does it create `coding-agent::<project>`. The choice is
  cached per repo, and a `PreToolUse` hook adds that `bank_id` to every
  Hindsight call, so agents never pick a bank. Folders that aren't git repos
  (and your home folder) use the default bank.
- **Health check:** the same session-start step reports the bank in use, or
  that Hindsight is unreachable. If memory is down, work continues without it.

### Cross-model second opinion (OpenAI Codex)

A different model family catches different mistakes, so Claude never grades
its own work alone. Before acceptance, every feature's full diff is reviewed by
OpenAI Codex through the official Codex CLI, using your own Codex sign-in
(ChatGPT plan or API key; usage counts toward your Codex limits). The plugin
never touches credentials.

- **Review (always):** Codex runs in a read-only sandbox and returns structured
  findings (severity, file, line, why, fix). Reasoning effort is chosen
  deterministically, the same way for review and adversarial runs: medium for
  routine work, high when any task routed to
  Opus or escalated, or touched security, persistence, concurrency,
  cross-system or public-API code.
- **Adversarial tests (`--adversarial`):** Codex writes tests that attack the
  change (edge cases, hostile input, error paths, concurrency) only under
  `.feature-ladder/adversarial/<slug>/`, runs them with your test runner, and
  marks which failures are real bugs. A before-and-after `git status` check
  flags anything it touched outside that folder.
- **Triage:** critical and high findings must be fixed (routed like any task)
  and re-reviewed; medium are fixed or justified in the report; disputes go to
  a one-question Fable second opinion; at most two rounds, then `deep-verifier`.
  Adversarial tests that exposed real bugs are moved into your test suite once
  fixed. Confirmed findings and false alarms are both saved to Hindsight.
- **Cheap to drive:** a Haiku-low agent runs Codex and hands back its findings
  verbatim, so Fable never spends tokens reading raw review output.
- **Optional:** if Codex isn't installed or signed in, the run says so and continues.

Records are saved to `.feature-ladder/reviews/`. That folder and
`.feature-ladder/adversarial/` are git-ignored automatically. On demand, for
any branch or uncommitted work: `/feature-ladder:codex-review [--adversarial]`.

### Evidence and escalation discipline

- Task cards, evidence packets and design docs follow fixed formats
  (`skills/orchestration-ladder/SKILL.md`).
- Failed or blocked tasks move up **one** band at a time.
- Escalation means "fix the task" (decide, design, or split), never "the
  coordinator writes the code".

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
3. It returns at most `max_skills` skills (default 3) within `max_chars` of
   skill text (default 60,000). Anything cut is reported, never silently
   included.
4. `--inject` prints those skills' full instructions. The coordinator pastes
   them into the task card and logs the choice in `FEATURE-REPORT.md`.
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
the start of a conversation it downloads `route.py`, `select_skills.py`,
`skill-rules.json` and `resolve_bank.py` from this repo, pinned to a release tag (currently `v1.2.0`), so the app and
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
| `/feature-ladder:feature <request> [--auto] [--adversarial] [--ultra]` | Intake, then run a feature to completion on its own branch and open PRs |
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
| Named ladder agents (13 rungs + scoper) | ✅ | General-purpose subagents with role briefs |
| Hindsight memory | ✅ plugin MCP server, per-repo bank via hooks | ✅ if a Hindsight connector is added; per-repo bank via the same resolver |
| Retain gate and startup hooks | ✅ | — (no hooks in the app) |
| Codex second opinion and adversarial tests | ✅ via your local Codex CLI | — (needs your local Codex sign-in) |
| Intake (ask once, up front) | ✅ scoper agent + question cards | ✅ same script; questions asked in chat |
| Branches and checkpoint PRs | ✅ plus guard hook | ✅ only when the work is a git repo with `gh` |

## Repository layout

```
.claude-plugin/marketplace.json        marketplace: feature-ladder
plugins/feature-ladder/
  .claude-plugin/plugin.json
  .mcp.json                            Hindsight HTTP MCP server (env-driven)
  agents/                              14 agents (13 rungs + scoper)
  commands/                            feature, scope, route, skills, recall, codex-review
  skills/orchestration-ladder/         protocol + handoff contracts + pr_checkpoint.py
  skills/complexity-router/            route.py + intake.py
  skills/skill-selector/               select_skills.py + skill-rules.json
  skills/hindsight-memory/             recall / retain protocol + resolve_bank.py
  skills/cross-model-review/           codex_review.py (Codex review + adversarial tests)
  hooks/                               bank resolution, per-call bank routing, retain gate, irreversible-command guard
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
- Subagents have a fixed startup cost. For very small checks, running them
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

## License

[MIT](LICENSE) © 2026 Jake McTigue
