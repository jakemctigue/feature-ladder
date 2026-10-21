---
description: End a coding session - write the session's queued memories to Hindsight once (a script, no model), and only if you ask for them, run ONE review over everything the session built (Codex, adversarial tests, a Fable second opinion). Run it once after many features, never per feature. Usage - /feature-ladder:wrap [--review] [--adversarial] [--second-opinion] [--base <ref>]
argument-hint: [--review] [--adversarial] [--second-opinion] [--base <ref>]
---

Wrap up this coding session with arguments: $ARGUMENTS

Do nothing that is not listed here. Use `python` if `python3` is missing.

## 1. Where things stand

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/session_budget.py" status
python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/session_budget.py" memory
```

If a ladder run is still active (`.feature-ladder/run.json` with `"active": true`),
stop and say so: wrap runs after the session's features, not during one.

## 2. Review (only with `--review`, `--adversarial` or `--second-opinion`)

The base is `--base` if given, otherwise the branch the session's first run
started from (`base` in `.feature-ladder/run.json`), so the review covers
every feature of the session in one pass.

- `--review`: dispatch `feature-ladder:codex-reviewer` (model haiku, effort
  low) once with the command
  `python3 "${CLAUDE_PLUGIN_ROOT}/skills/cross-model-review/scripts/codex_review.py" review --slug wrap-<date> --base <base>`.
  One attempt; a timeout or exit 3 is reported as "Codex review skipped:
  <reason>".
- `--adversarial`: afterwards, once more with `adversarial` in place of
  `review` (implies `--review`).
- `--second-opinion`: send `feature-ladder:second-opinion` (fable/low) ONE
  question: the most consequential open point from the review (or, without a
  review, the riskiest decision in the session's FEATURE-REPORT files), with
  the excerpt it needs. Never more than one.

Show the findings grouped by severity with the verification card path. Do not
fix anything here: each confirmed bug becomes its own
`/feature-ladder:feature` request, which the user starts. Each spawn above
counts against the session's subagent budget; a denied spawn is final, so
report what was skipped.

## 3. Memory, once, with no model

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/hindsight-memory/scripts/memory_gate.py" flush
```

It re-checks every queued `[kind] fact` line against the memory gate (one
concrete, repo-anchored fact; no summaries, narration or duplicates), writes
the survivors straight to this repository's Hindsight bank and lists what it
wrote and what it rejected. Nothing is summarised or reworded. Exit 3 means
Hindsight was unreachable from the script: run it again with `--dry-run` and
make one `retain` call per `would_write` line exactly as printed (the hook
applies the same gate); if that is not possible either, stop here and leave
the queue for the next wrap. Once the queue is written (or empty):

```
python3 "${CLAUDE_PLUGIN_ROOT}/skills/orchestration-ladder/scripts/session_budget.py" close
```

## 4. Report

One short block: features in the session (from the run ledgers), subagents
used of the budget, review outcome (or "no review scheduled"), memories
written, and the token table from
`python3 "${CLAUDE_PLUGIN_ROOT}/hooks/scripts/ledger.py" report`
for the last run.
