---
name: orchestration-ladder
description: The feature-ladder operating protocol - which rung does what, how work flows coordinator -> experts -> workers -> verifier, and the handoff contracts between rungs. Load whenever coordinating or executing a feature-ladder task.
---

# Orchestration ladder

Fable is expensive and precise; use it like a senior engineer's time. Every other
rung exists so that Fable only plans, directs, fact-checks, inspects evidence and
accepts. Fable never writes feature code itself unless it has climbed to High+
for a specific unresolved problem.

## The ladder

| Rung | Agent | Model / effort | Daily job |
|---|---|---|---|
| I | `scoper` | sonnet / medium | Intake before anything is delegated: settles every router variable with evidence and lists the decisions to ask the user |
| T0 | `mechanic` | haiku / low | Mechanical patches, docs drafts, fixtures, executing defined checks |
| T1 | `evidence-collector` | haiku / medium | Collect current sources, extract claims, prepare evidence packets |
| T2-T3 | `implementer` | sonnet / medium or high | Bounded coding, regression tests, small domain analysis |
| T4-T5 | `senior-implementer` | opus / medium or high | Difficult implementation **under an agreed design** |
| D | `domain-expert` / `domain-expert-high` | opus / medium or high | Design, domain analysis, option comparison, spec writing |
| F-Low | `second-opinion` | fable / low | Short, focused second opinion where it saves a larger handoff |
| **F-Med** | **`coordinator`** | **fable / medium** | **Plan, direct workers, fact-check, inspect evidence, accept the result** |
| F-High | `deep-verifier` | fable / high | Subtle bugs, conflicting evidence, cross-system or consequential correctness |
| F-Extra | `architect-escalation` | fable / xhigh | Architecture or systemic problems still unresolved after High |
| F-Ultra | `ultra-investigator` | fable / max | Optional deep investigations; opt-in only |
| X | `codex-reviewer` | haiku / low driving OpenAI Codex | Cross-model second opinion on every feature diff; adversarial tests on request (`--adversarial`) |

Effort names map to Claude Code effort levels: Low=`low`, Medium=`medium`,
High=`high`, Extra=`xhigh`, Ultra=`max`.

## Flow for a feature

```
user -> /feature-ladder:feature "<description>"
  0. intake                scoper (Sonnet) estimates every variable; the user answers only
                           what is uncertain AND changes the model, plus open decisions;
                           confirmed intake saved; run branch feature-ladder/<slug> created
  1. coordinator (F-Med)   recall once -> ONE Task Card from the intake (intake.py card
                           routes it and picks its skills in one call)
  2. worker (T0-T3)        does the whole feature, runs the Done-when checks itself,
                           returns an Evidence Packet with the output quoted
  3. coordinator (F-Med)   re-runs the checks with verify.py (no model), accepts,
                           retains one line, opens the PR
  -- that is the whole flow for size S and M (about a dozen coordinator turns) --
  size L only:             design (domain-expert) if not agreed; split into cards;
                           Codex review once with a scripted triage card;
                           one scribe at the end; escalation one rung at a time
                           (deep-verifier -> architect-escalation -> ultra, opt-in)
```

## Budget by size

The intake's `budget` (from the router's `size`) is binding. It exists because
cost scales with agents x turns x context: every turn of every agent re-reads
its whole context, so one extra subagent costs more than its output suggests.

| Size | Score | Workers | Design | Check re-run | Codex | Scribe |
|---|---|---|---|---|---|---|
| S | 0-9 | 1 | no | verify.py (no model) | no | no |
| M | 10-19 | 1-2 | no | verify.py (no model) | `--codex` only | no |
| L | 20+ or hard flag | <=4 | if not agreed | verify.py; a mechanic only for Codex repros | once | one |

Adversarial tests: only with `--adversarial`, at any size.

## Run to completion: branches and checkpoint PRs

After intake the run never stops for the user. `scripts/pr_checkpoint.py`
keeps it safe:

- `start` puts the run on `feature-ladder/<slug>` (the user's uncommitted
  changes are committed first as their own commit, or stashed, as chosen at
  intake) and switches on the guard hook that denies irreversible shell
  commands while the run is active.
- `checkpoint` commits, pushes and opens a draft PR for a critical design
  decision (assumption made, alternatives, how to switch), then continues on
  `feature-ladder/<slug>-ptN`, stacked on it.
- `finish` opens the final PR on top of the stack and prints the merge order.
- `abort` marks a run inactive (guard off) after one died midway; branches stay.

Without a remote or the `gh` CLI the same happens on local branches and the PR
bodies are saved in `.feature-ladder/checkpoints/`.

## Changes outside the repo

Code on a branch is always reversible. Steps with effects outside the repo
(Firestore or database writes, migrations, deploys, publishing, outside
services) are run by the workers like any other step, under the policy chosen
at intake (`outside_policy`: `all`, `nonprod` or `none`). `scripts/irreversible.py`
classifies each shell command with no model tokens, and the guard hook applies
it:

| Tier | Examples | What happens |
|---|---|---|
| mundane | emulator or dev/staging targets, `firebase deploy`, index builds, exports, dry runs | runs at once, logged |
| risky | `firestore:delete`, `firestore import`, destructive SQL, migration or backfill scripts on production | denied once; one `second-opinion` (fable/low) verdict, then `irreversible.py approve`, then it runs |
| never | delete a project/database/repo, weaken database protection, merge PRs, push to the base branch, force-push or delete main, delete backups | left for the user, logged |

`irreversible.py classify "<cmd>"` shows the tier; `irreversible.py report`
prints the table for FEATURE-REPORT.md. Nobody writes undo scripts or
rehearsals for these steps: the tokens go to the feature.

Secret management commands follow the same tiers: creating or adding a
version is *risky* on production; deleting, disabling or changing who can
read a secret is on the never-list. A fourth outcome, *secret*, denies any
command whose output would carry a secret value into the transcript and
points at `with_secrets.py`.

## Rules that keep Fable sparse

1. **Fable Medium runs at most three "inspection passes" per feature**: after planning,
   after implementation, after fixes. Everything between is delegated.
2. **Never climb two Fable rungs at once.** Medium -> High -> Extra -> Ultra, and each
   climb needs a one-paragraph justification written into FEATURE-REPORT.md.
3. **Second opinions (F-Low) are for a single question** answerable in under ~300
   words; if the question needs code reading across files, it is a worker task instead.
4. **Fable never re-derives what a worker already proved.** It inspects the evidence
   packet; it re-runs only the checks it doubts.
5. **Opus rungs require `design_agreed=true`** from the router. No agreed design, no
   Opus implementation; send the task to a domain expert first.

## Handoff contracts

### Task Card (coordinator -> any worker)
```
TASK <id>: <one line>
Rung: <agent> (<model>/<effort>)   Router score: <n>   Flags: <none|...>
Scope: files/dirs the worker may touch
Design: path to DESIGN.md section, or "N/A - mechanical"
Done when: <observable checks, e.g. `pytest tests/x -q` green, lint clean>
Do not: <things explicitly out of scope>
Memory: <1-3 relevant Hindsight recall snippets, verbatim>
Secrets: <NAME -> ENV_VAR, ... | none>   (Google Secret Manager names; never values)
Return: Evidence Packet
<the "## Skills for this task" block printed by select_skills.py --inject --out:
 it names the file holding the only skills this worker has>
```
The skill text itself lives in `.feature-ladder/cards/<task-id>-skills.md`
(absolute path in the card); the worker reads it before starting. Workers
cannot invoke the Skill tool. If a worker needs a skill it was not given, it
returns `blocked` naming the skill and why; it does not improvise.

**Secrets rule for every worker.** A command that needs an API key or other
secret is run as
`python3 ${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/with_secrets.py --secret NAME[=ENV_VAR] -- <command>`
(the card's `Secrets:` line gives the names). The value lands in that one
command's environment and is masked in its output. Never fetch, print, echo,
log, commit or quote a secret value, and never write one into a file; the
guard hook denies commands that would. New code reads secrets the way the
repo already does (Secret Manager client, `defineSecret`, or the env var the
deployment injects), never from a committed file. If a needed secret does not
exist or is unreadable, report it in the Evidence Packet as `blocked: secret
NAME` and finish everything that does not depend on it.

### Evidence Packet (any worker -> coordinator)
```
TASK <id> - <status: done | blocked | partial>
Changed: <file list with line ranges>
Checks run: <command -> exit code -> last 5 lines>
Claims: <each factual claim with how it was verified; "unverified" is allowed and honest>
Risks: <anything the worker is unsure about>
Memory candidates: <facts worth retaining, one line each, or "none">
```
Workers never say "should work". Either a check ran and is quoted, or the claim is marked unverified.

### Design Doc (domain expert -> coordinator)
`DESIGN.md` with: Problem, Constraints (from memory + repo), Options considered
(>=2) with trade-offs, Chosen approach, Interfaces/contracts, Test strategy,
Rollback plan, Open questions. The coordinator marks it ACCEPTED or returns it
with numbered objections. Only ACCEPTED designs unlock Opus implementation rungs.

## Parallelism

Independent tasks are launched in one message with multiple Agent calls. Tasks
that touch the same files are serialized. Use `isolation: worktree` for Opus
implementation tasks so a bad run never dirties the main tree.

## Acceptance checks without a model

`scripts/verify.py` re-runs a card's "Done when" checks for acceptance, so no
mechanic subagent is started for them:

```
python3 ${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/verify.py run --id <task-id> \
    --check "<done-when command>" [--check ...] --scope <path> [--scope ...] \
    --changed <file the packet says it changed> [--changed ...] [--base <ref>]
```

It runs each command exactly as written, records exit code and output tail,
lists what actually changed in the repo, and flags files outside the card's
Scope or a `Changed:` claim that does not match. Evidence goes to
`.feature-ladder/evidence/<task-id>.json`; the printed summary is a few lines.
Exit 0 is green; 1 means a check failed (or was refused: a check that would
touch production data or print a secret is never run); 2 means the checks
passed but the scope or the claim is wrong. A `mechanic` is dispatched for
acceptance only when a check's output needs interpretation (a flaky suite, an
environment quirk) or the card's checks are not shell commands.

## Acceptance criteria (coordinator's final gate)

- Every Task Card has a matching Evidence Packet with status `done`.
- All "Done when" checks were re-run by `verify.py` (not by the implementer who
  wrote the code) with exit 0, or by `mechanic` where the card says a check
  needs interpretation.
- No unverified claim survives in anything user-visible without being labelled.
- FEATURE-REPORT.md written; outcome retained to Hindsight with tags `project:<name>`, `feature:<slug>`.
