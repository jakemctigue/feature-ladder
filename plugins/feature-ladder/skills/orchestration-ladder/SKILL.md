---
name: orchestration-ladder
description: The feature-ladder operating protocol - atomic feature runs (one cheap worker, verify.py, a PR), the session subagent budget, which rung does what, the GLM tier, live-change tiers and the handoff contracts. Reviews, second opinions and memory happen once per session at /feature-ladder:wrap. Reference; no agent preloads it.
---

# Orchestration ladder

A feature run implements the requested code change and its tests, nothing
else, with the fewest and cheapest agents that can do it. Fable is reached
only when the user asks for it. The rules below are enforced by scripts and
hooks, not by the coordinator's judgment.

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
| F-Med | `coordinator` | fable / medium | Card, dispatch, verify.py, accept: 12-24 turns |
| F-Low/High/Extra/Ultra | `second-opinion`, `deep-verifier`, `architect-escalation`, `ultra-investigator` | fable | Only with `--second-opinion`, `--escalate`, `--ultra` |
| X | `codex-reviewer` | haiku / low driving OpenAI Codex | Only at `/feature-ladder:wrap --review` |

## Flow

```
/feature-ladder:feature "<request>"
  0. intake      scoper (Sonnet) settles the fields; ONE round of questions:
                 the session's subagent budget (first feature only), what is
                 uncertain and changes the model, open decisions, live changes
  1. coordinator one recall -> one Task Card (intake.py card: route + skills)
  2. worker      GLM or Sonnet medium does the change and its tests
  3. coordinator verify.py re-runs the checks (no model), queues memory lines,
                 opens the PR
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
(a hard flag), no Codex, no scribe, no Fable rung, no check-runner.

`scripts/session_budget.py` is a PreToolUse hook on Agent/Task. It denies a
spawn past the session budget, past the feature's workers (+1 retry, +1
design), a second mechanic or design step, and inside a run any
`codex-reviewer`, `memory-scribe` or Fable rung the user did not enable.
External workers check the same budget (`external_worker.py` exit 4).

## The GLM tier (OpenRouter)

Cards whose route has `provider: glm` (every mechanic card; implementer at
medium effort with score <= 6) run on GLM 5.3 Flash
(`z-ai/glm-5.3-flash:nitro`) through `scripts/external_worker.py`: a child
`claude -p` pointed at OpenRouter, the worker contract as its system prompt,
the same tools and guard hook, billed to OpenRouter credits instead of the
Claude plan. It is on by default (`workers: auto`) whenever the Claude CLI and
an OpenRouter key (env `OPENROUTER_API_KEY`, or the Secret Manager secret of
that name through gcloud) are present; otherwise `intake.py card` prints a
subagent runner and says why. `/feature-ladder:workers jev` switches the
cheap tier to `typesafe/jev-router`; `claude` turns it off. Configuration:
`~/.claude/feature-ladder/providers.json`.

## Changes outside the repo

The intake's `outside_policy` (`none`, the recommended default; `nonprod`;
`all`) decides what a run may do outside the repo. `scripts/irreversible.py`
classifies each shell command with no model and the guard hook applies it:

| Tier | Examples | What happens |
|---|---|---|
| mundane | emulator or dev/staging targets, deploys, index builds, dry runs | runs if the policy allows, logged |
| risky | `firestore:delete`, imports, destructive SQL, backfills on production | left for the user with the exact command; with `--second-opinion`, one Fable verdict then `irreversible.py approve` |
| never | delete a project/database/repo, weaken protection, merge PRs, push or force-push the base branch | left for the user |

A fourth outcome, *secret*, denies a command that would print a secret value
and points at `with_secrets.py`. No undo scripts, no rehearsals.

## Handoff contracts

### Task Card (coordinator -> worker)
```
TASK <id>: <one line>
Rung: <agent> (<model>/<effort>)   Router score: <n>   Flags: <none|...>
Scope: <files the worker may touch>
Design: <DESIGN.md section | "N/A - the request is the design">
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
failed or was refused, 2 scope or claim mismatch). Exit 1: re-dispatch once,
then one band up once, then `partial`.

## Memory

One scoped `recall` per feature (a hook pins it to this repo's bank and
`project:<name>`, at most 1000 tokens). No `retain` during a run: the
coordinator queues `[kind] fact` lines with `session_budget.py remember`, the
memory gate refuses summaries and vague lines, and `/feature-ladder:wrap`
writes the queue with `memory_gate.py flush` (no model). Details:
hindsight-memory skill.

## Branches and checkpoint PRs

`scripts/pr_checkpoint.py`: `start` puts the run on `feature-ladder/<slug>`
(uncommitted changes committed first or stashed, as chosen) and writes
`run.json` (policy, budget, switches) which arms the hooks; `checkpoint`
opens a draft PR for a critical decision and continues stacked on it;
`finish` opens the final PR and prints the merge order; `abort` disarms a run
that died midway. Without a remote or `gh`, the same happens on local
branches.
