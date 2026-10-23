---
name: orchestration-ladder
description: The feature-ladder operating protocol - atomic feature runs (one cheap worker, verify.py, a merged PR labelled review: pending), the session subagent budget, which rung does what, the GLM tier, the confidence surface that replaced the gates, the outside-repo audit log and the handoff contracts. Reviews, second opinions and memory happen once per session at /feature-ladder:wrap. Reference; no agent preloads it.
---

# Orchestration ladder

A feature run implements the requested code change and its tests, nothing
else, with the fewest and cheapest agents that can do it, and it does not
wait for anyone: the work is merged and deployed when the feature calls for
it, labelled `review: pending`, and the user reviews it afterwards. Fable is
reached on the run's own initiative only to integrate the work of two or
more different coding agents; every other Fable rung needs a switch from the
user. The rules below are enforced by scripts and hooks, not by the
coordinator's judgment.

## The ladder

| Rung | Agent | Model / effort | Job |
|---|---|---|---|
| I | `scoper` | sonnet / medium | Intake before anything is delegated: every router variable with evidence, the decisions to ask |
| T0 | `mechanic` | GLM 5.3 Flash (OpenRouter), else haiku / low | Mechanical patches, docs drafts, fixtures; at most one per feature |
| T1 | `evidence-collector` | haiku / medium | Collect sources, extract claims |
| T2 | `implementer` | GLM 5.3 Flash for score <= 6, else **sonnet / medium** | Bounded coding and its tests: the default rung |
| T3 | `implementer` | sonnet / high | Bounded coding, harder |
| T4-T5 | `senior-implementer` | opus / medium or high | Difficult implementation under an agreed design |
| D | `domain-expert(-high)` | opus / medium or high | One design step, only on a hard flag |
| C | `coordinator` | opus / medium | Confidence, card, dispatch, verify.py, accept, merge: 12-24 turns |
| FI | `integrator` | fable / medium | Once per feature, only after two or more different coding agents wrote code; no coding tools, superpowers skills only |
| F-Low/High/Extra/Ultra | `second-opinion`, `deep-verifier`, `architect-escalation`, `ultra-investigator` | fable | Only with `--second-opinion`, `--escalate`, `--ultra` |
| X | `codex-reviewer` | haiku / low driving OpenAI Codex | Only at `/feature-ladder:wrap --review` |

## Flow

```
/feature-ladder:feature "<request>"
  0. intake      scoper (Sonnet) settles the fields; ONE round of questions:
                 the session's subagent budget (first feature only), what is
                 uncertain and changes the model, open decisions
  1. coordinator one recall (saved) -> confidence.py card: proceed, or
                 reshard into smaller cards -> one Task Card each
                 (intake.py card: route + skills)
  2. worker      GLM or Sonnet medium does the change and its tests
  3. integrator  Fable, ONLY if >= 2 different coding agents wrote code:
                 finds the seams, returns one integration card or "coherent"
  4. coordinator verify.py re-runs the checks (no model), queues memory
                 lines, pr_checkpoint.py finish: PR, label review: pending,
                 merge the stack
/feature-ladder:wrap [--review] [--adversarial] [--second-opinion]
                 once at the end of the session: memory written by a script,
                 and the reviews the user scheduled, over the whole session
```

## Budgets (binding, enforced)

**Session**: asked once per coding session, stored in
`.feature-ladder/session.json`: Small 2 subagents, Medium 4, Large 8. The
scout and the coordinator are not counted; every other subagent and every
external GLM worker is. A new Claude Code session asks again.

**Feature** (`budget` in the confirmed intake, from the router's size):

| Size | Score | Workers | Coordinator turns |
|---|---|---|---|
| S | 0-9 | 1 | ~12 |
| M | 10-19 | 1, or 2 for two independent file sets | ~20 |
| L | 20+ | up to 4 | ~24 (the coordinator's maxTurns at every size) |

Every size: at most one mechanic, a design step only when `budget.design`
(a hard flag), no Codex, no scribe, no check-runner, and no Fable rung but
the integrator (once, after two coding agents; it does not count as a
worker).

`scripts/session_budget.py` is a PreToolUse hook on Agent/Task. It denies a
spawn past the session budget, past the feature's workers (+1 retry, +1
design), a second mechanic or design step, the integrator before two coding
agents have returned (or a second integrator), and inside a run any
`codex-reviewer`, `memory-scribe` or other Fable rung the user did not
enable. External workers check the same budget (`external_worker.py` exit 4).

## The confidence surface (what the gates became)

Before each Task Card, and as advice before a risky command, the coordinator
runs `scripts/confidence.py`. It turns the feature's one Hindsight recall into
an emotionally textured surface: every remembered fact is scored for
sentiment (a pitfall reads as a warning, a convention or a test strategy as
steady ground; a lexicon by default, Jev through OpenRouter with
`--scorer jev`), weighted by how closely it touches the files and commands at
hand, and folded with the card's size (or the command's tier) and the
evidence so far into one number in [0, 1], with a mood: `unknown`, `wary`,
`steady`, `confident`.

| Confidence | Card | Command |
|---|---|---|
| >= 0.60 | proceed | run it |
| < 0.60 | **reshard**: split into smaller cards by independent file sets, recall again per shard; once per feature | run the preview or dry-run form first when the tool has one, then the real command |

It never blocks anything. The number, the decision and the mood go on the
card's `Confidence:` line and into FEATURE-REPORT.md. The threshold is
`FEATURE_LADDER_CONFIDENCE_THRESHOLD` (default 0.60).

## The GLM tier (OpenRouter)

Cards whose route has `provider: glm` (every mechanic card; implementer at
medium effort with score <= 6) run on GLM 5.3 Flash
(`z-ai/glm-5.3-flash:nitro`) through `scripts/external_worker.py`: a child
`claude -p` pointed at OpenRouter, the worker contract as its system prompt,
the same tools and audit hook, billed to OpenRouter credits instead of the
Claude plan. It is on by default (`workers: auto`) whenever the Claude CLI and
an OpenRouter key (env `OPENROUTER_API_KEY`, or the Secret Manager secret of
that name through gcloud) are present; otherwise `intake.py card` prints a
subagent runner and says why. `/feature-ladder:workers jev` switches the
cheap tier to `typesafe/jev-router`; `claude` turns it off. Configuration:
`~/.claude/feature-ladder/providers.json`.

## Changes outside the repo: an audit log, not a gate

`scripts/irreversible.py` classifies each shell command during a run with no
model, and the PreToolUse hook records the result in `run.json`:

| Tier | Examples | What happens |
|---|---|---|
| mundane | emulator or dev/staging targets, deploys, index builds, dry runs | runs, logged |
| risky | `firestore:delete`, imports, destructive SQL, backfills on production | runs, logged `review: pending` |
| never | the former never-list: merge PRs, push or force-push the base branch, delete a project/database/repo/backups, weaken protection | runs, logged `review: pending`, named first in the report |
| secret | a command whose output would print a secret value | **denied** - the one denial left; use `with_secrets.py` |

There is no intake policy and no approve step. `irreversible.py pending`
prints the `review: pending` rows as a checklist; `pr_checkpoint.py finish`
puts the same list in the PR body. No undo scripts, no rehearsals.

## Handoff contracts

### Task Card (coordinator -> worker)
```
TASK <id>: <one line>
Rung: <agent> (<model>/<effort>)   Router score: <n>   Flags: <none|...>
Scope: <files the worker may touch>
Design: <DESIGN.md section | "N/A - the request is the design">
Confidence: <0.00-1.00> <proceed|reshard> (<mood>) - <one line of texture>
Done when: <exact shell commands>
Do not: <out of scope>
Memory: <recall snippets that bear on this card, verbatim, or none>
Secrets: <NAME -> ENV_VAR, ... | none>
Return: Evidence Packet
<the "## Skills for this task" block from intake.py card>
```
The skill text lives in `.feature-ladder/cards/<task-id>-skills.md`; the
worker reads it, nobody else does. Workers cannot invoke the Skill tool.

**Secrets.** A command that needs a secret runs as
`python3 ${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/with_secrets.py --secret NAME[=ENV_VAR] -- <command>`.
Never fetch, print, log, commit or quote a value.

### Evidence Packet (worker -> coordinator)
```
TASK <id> - <status: done | blocked | partial>
Changed: <file list with line ranges>
Checks run: <command -> exit code -> last 5 lines>
Claims: <each claim with how it was verified, or "unverified">
Risks: <anything the worker is unsure about>
Memory candidates: <"[kind] fact" lines a future task could act on, or none>
Retained: deferred - <n> candidates
```

### Integration verdict (integrator -> coordinator, two or more coding agents only)
```
INTEGRATION: coherent | needs-card
Seams: <file:line on both sides, what disagrees, the keeper - or none>
Checks: <the build/type/lint/test commands that prove the parts are one program>
<for needs-card: exactly one Task Card the coordinator routes unchanged>
Memory candidates: ...
Retained: deferred - <n> candidates
```

### Design Doc (domain expert -> coordinator, hard flag only)
`DESIGN.md`: Problem, Constraints, Options (>= 2), Chosen approach,
Interfaces, Test strategy, Rollback, Open questions. One pass; the coordinator
accepts it or decides the open points itself.

## Acceptance without a model

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/verify.py run --id <task-id> \
    --check "<done-when command>" [--check ...] --scope <path> [--scope ...] \
    --changed <file the packet says it changed> [--changed ...] [--base <ref>]
```

It runs each command as written, records exit code and output tail, and flags
files outside Scope or a wrong `Changed:` claim (exit 0 green, 1 a check
failed or would print a secret, 2 scope or claim mismatch). Exit 1:
re-dispatch once, then one band up once, then `partial`.

## Memory

One scoped `recall` per feature (a hook pins it to this repo's bank and
`project:<name>`, at most 1000 tokens), saved to
`.feature-ladder/runs/<slug>/recall.json` so `confidence.py` can read it. No
`retain` during a run: the coordinator queues `[kind] fact` lines with
`session_budget.py remember`, the memory gate refuses summaries and vague
lines, and `/feature-ladder:wrap` writes the queue with `memory_gate.py
flush` (no model). Details: hindsight-memory skill.

## Branches, checkpoint PRs and the merge

`scripts/pr_checkpoint.py`: `start` puts the run on `feature-ladder/<slug>`
(uncommitted changes committed first or stashed, as chosen) and writes
`run.json` (budget, switches, environments) which arms the hooks;
`checkpoint` opens a draft PR for a critical decision and continues stacked
on it; `finish` opens the final PR, labels the stack `review: pending`,
merges it in order (checkpoints first) and prints each result; `--no-merge`
leaves it open; `abort` disarms a run that died midway. A merge GitHub
refuses is reported, never forced. Without a remote or `gh`, the same
happens on local branches and the stack is merged into the base branch
locally.
